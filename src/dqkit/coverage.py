"""Which model columns have at least one data-quality check?"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lineage import LineageGraph
from .quality import DatasetConfig, columns_covered


@dataclass
class ModelCoverage:
    model: str
    columns: list[str]
    covered: list[str] = field(default_factory=list)
    has_config: bool = False

    @property
    def uncovered(self) -> list[str]:
        return [c for c in self.columns if c not in self.covered]

    @property
    def pct(self) -> float:
        return round(100.0 * len(self.covered) / len(self.columns), 1) if self.columns else 100.0


def coverage(graph: LineageGraph, configs: list[DatasetConfig]) -> list[ModelCoverage]:
    """Match each config to the model named like its table (schema prefix ignored)."""
    checked: dict[str, set[str]] = {}
    for cfg in configs:
        table = cfg.table.split(".")[-1]
        cols = checked.setdefault(table, set())
        for check in cfg.checks:
            cols |= columns_covered(check)
    out = []
    for model in graph.order:
        columns = graph.model_columns.get(model, [])
        cov = ModelCoverage(model, columns, has_config=model in checked)
        cov.covered = [c for c in columns if c.lower() in checked.get(model, set())]
        out.append(cov)
    return out


def overall_pct(items: list[ModelCoverage]) -> float:
    total = sum(len(m.columns) for m in items)
    return round(100.0 * sum(len(m.covered) for m in items) / total, 1) if total else 100.0


def render(items: list[ModelCoverage]) -> str:
    lines = [
        f"Column coverage by data-quality checks: {overall_pct(items)}%",
        "",
        "| Model | Coverage | Columns without a check |",
        "|---|---|---|",
    ]
    for m in items:
        note = "no checks defined" if not m.has_config else ", ".join(m.uncovered) or "none"
        lines.append(f"| {m.model} | {m.pct}% ({len(m.covered)}/{len(m.columns)}) | {note} |")
    return "\n".join(lines)
