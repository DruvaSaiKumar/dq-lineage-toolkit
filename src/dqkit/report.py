"""Markdown rendering for quality results, impact analysis and tag propagation."""

from __future__ import annotations

from collections import Counter

from .lineage import DIRECT, ColumnRef, Impact, LineageGraph
from .quality import PASS, CheckResult, DatasetResult


def _cell(value: object) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]


def _samples(result: CheckResult) -> list[str]:
    header = list(result.samples[0])
    return _table(header, [[row.get(h) for h in header] for row in result.samples])


def render_quality(results: list[DatasetResult]) -> str:
    lines = ["# Data quality report", ""]
    overall = Counter()
    for r in results:
        overall.update(r.counts)
    lines += [
        *_table(
            ["Dataset", "Status", "PASS", "FAIL", "WARN", "ERROR"],
            [
                [d.dataset, d.status, *(d.counts[s] for s in ("PASS", "FAIL", "WARN", "ERROR"))]
                for d in results
            ],
        ),
        "",
    ]
    for d in results:
        lines += [f"## {d.dataset} (`{d.table}`): {d.status}", ""]
        findings = [c for c in d.results if c.status != PASS or (c.failing and c.detail)]
        if findings:
            findings.sort(key=lambda c: (c.status == PASS, c.status, c.name))
            rows = [
                [c.status, c.severity, c.name, c.failing, c.total, c.failure_pct, c.detail] for c in findings
            ]
            lines += _table(["Status", "Severity", "Check", "Failing", "Of", "%", "Detail"], rows) + [""]
        else:
            lines += ["All checks passed.", ""]
        for c in (c for c in d.results if c.samples):
            lines += [f"**Sample violations: {c.name}**", "", *_samples(c), ""]
        passed = sum(c.status == PASS for c in d.results)
        lines += [f"{passed} of {len(d.results)} checks passed.", ""]
    return "\n".join(lines)


def render_impact(graph: LineageGraph, ref: ColumnRef, hits: list[Impact]) -> str:
    if not hits:
        return f"Nothing is built from {ref}."
    models = sorted({h.column.table for h in hits})
    lines = [f"Changing {ref} can affect {len(hits)} column(s) in {len(models)} model(s):", ""]
    lines += _table(
        ["Depth", "Column", "Relationship"],
        [[h.depth, h.column, "copy" if h.kind == DIRECT else "computed from it"] for h in hits],
    )
    return "\n".join(lines)


def render_tags(graph: LineageGraph) -> str:
    inherited = graph.inherited_tags()
    if not graph.tags:
        return "No tags declared in the sources file."
    lines: list[str] = []
    for tag in sorted({t for tags in graph.tags.values() for t in tags}):
        origins = sorted(c for c, tags in graph.tags.items() if tag in tags)
        lines += [f"## Tag `{tag}`", "", "Declared on: " + ", ".join(f"`{o}`" for o in origins), ""]
        reached = sorted((c, kinds[tag]) for c, kinds in inherited.items() if tag in kinds)
        rows = [[c, "verbatim copy path" if kind == DIRECT else "transformed"] for c, kind in reached]
        lines += _table(["Column that carries it", "How it arrives"], rows) if rows else ["Not propagated."]
        lines += [
            "",
            "A transformation is not assumed to anonymise the value; review each `transformed` "
            "column that leaves the trusted zone.",
            "",
        ]
    return "\n".join(lines)
