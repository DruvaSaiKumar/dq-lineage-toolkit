"""Command line interface.

Exit codes: 0 all good, 1 findings (failed checks / coverage below threshold), 2 could not run.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from . import __version__
from . import coverage as cov
from .demo import NOW, build_warehouse
from .lineage import ColumnRef, LineageError, build_graph
from .quality import (
    ERROR,
    FAIL,
    ConfigError,
    DatasetResult,
    append_history,
    load_baseline,
    load_dataset_config,
    run_dataset,
)
from .report import render_impact, render_quality, render_tags

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _expand(patterns: list[str]) -> list[str]:
    """Expand wildcards ourselves: Windows shells pass `*.yaml` through unexpanded."""
    out: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
        if not matches:
            raise ConfigError(f"no files match {pattern!r}")
        out += matches
    return out


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")


def _exit_code(results: list[DatasetResult]) -> int:
    statuses = {r.status for r in results}
    return 2 if ERROR in statuses else 1 if FAIL in statuses else 0


def _parse_now(text: str | None) -> datetime:
    if not text:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"--now must be an ISO timestamp, got {text!r}") from exc


def run_checks(
    config_paths: list[str],
    database: str | None = None,
    history: str | None = None,
    now: datetime | None = None,
) -> list[DatasetResult]:
    results = []
    for path in config_paths:
        cfg = load_dataset_config(path)
        db = database or cfg.database
        if not db:
            raise ConfigError(f"{path}: no database (set `database:` or pass --database)")
        con = duckdb.connect(str(db), read_only=True)
        try:
            result = run_dataset(con, cfg, now, load_baseline(history, cfg.dataset))
        finally:
            con.close()
        if history:
            append_history(history, result)
        results.append(result)
    return results


def _graph(args):
    return build_graph(args.models, args.sources, args.dialect)


def _cmd_check(args) -> int:
    results = run_checks(_expand(args.configs), args.database, args.history, _parse_now(args.now))
    text = render_quality(results)
    _write(args.report, text)
    if args.json:
        payload = [
            {"dataset": r.dataset, "table": r.table, "status": r.status, "counts": r.counts,
             "results": [c.__dict__ | {"failure_pct": c.failure_pct} for c in r.results]}
            for r in results
        ]  # fmt: skip
        _write(args.json, json.dumps(payload, indent=2, default=str))
    print(text)
    return _exit_code(results)


def _cmd_lineage(args) -> int:
    graph = _graph(args)
    _write(args.mermaid, "```mermaid\n" + graph.to_mermaid() + "\n```\n")
    _write(args.json, json.dumps(graph.to_dict(), indent=2))
    print(f"{len(graph.models)} models, {len(graph.source_columns)} sources, "
          f"{len(graph.table_edges)} table edges, {len(graph.column_edges)} column edges")  # fmt: skip
    for warning in graph.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print("\n```mermaid\n" + graph.to_mermaid() + "\n```")
    return 0


def _cmd_impact(args) -> int:
    graph = _graph(args)
    ref = ColumnRef.parse(args.column)
    known = set(graph.model_columns.get(ref.table, [])) | set(graph.source_columns.get(ref.table, []))
    if ref.column not in known:
        raise LineageError(f"unknown column {ref}")
    print(render_impact(graph, ref, graph.downstream(ref)))
    return 0


def _cmd_tags(args) -> int:
    print(render_tags(_graph(args)))
    return 0


def _cmd_coverage(args) -> int:
    graph = _graph(args)
    items = cov.coverage(graph, [load_dataset_config(p) for p in _expand(args.configs)])
    print(cov.render(items))
    return 1 if args.fail_under is not None and cov.overall_pct(items) < args.fail_under else 0


def _cmd_demo(args) -> int:
    ex, work = Path(args.examples), Path(args.workdir)
    graph = build_graph(ex / "models", ex / "sources.yml")
    configs = sorted(str(p) for p in (ex / "quality").glob("*.yaml"))
    history = work / "history.jsonl"
    if history.exists():
        history.unlink()

    # First a clean load, so volume checks have a baseline to compare against.
    build_warehouse(work / "baseline.duckdb", graph, clean=True)
    run_checks(configs, str(work / "baseline.duckdb"), str(history), NOW)

    injected = build_warehouse(work / "warehouse.duckdb", graph, clean=args.clean)
    results = run_checks(configs, str(work / "warehouse.duckdb"), str(history), NOW)
    report = render_quality(results)
    _write(str(work / "quality_report.md"), report)
    _write(str(work / "lineage.md"), "```mermaid\n" + graph.to_mermaid() + "\n```\n")
    _write(str(work / "lineage.json"), json.dumps(graph.to_dict(), indent=2))
    coverage_text = cov.render(cov.coverage(graph, [load_dataset_config(p) for p in configs]))
    _write(str(work / "coverage.md"), coverage_text)

    print(f"Injected defects: {injected}\n")
    print(report)
    print(coverage_text)
    return _exit_code(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dqkit", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_models(p, extra_sources=True):
        p.add_argument("models", help="directory of .sql models")
        p.add_argument("--sources", help="YAML file declaring source table schemas and tags")
        p.add_argument("--dialect", default="duckdb")

    p = sub.add_parser("check", help="run data-quality checks from YAML configs")
    p.add_argument("configs", nargs="+")
    p.add_argument("--database", help="DuckDB file (overrides `database:` in the configs)")
    p.add_argument("--history", help="JSONL file that stores metrics between runs")
    p.add_argument("--now", help="ISO timestamp to measure freshness against (default: now)")
    p.add_argument("--report", help="write the Markdown report here")
    p.add_argument("--json", help="write results as JSON here")
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("lineage", help="build the lineage graph and print a Mermaid diagram")
    with_models(p)
    p.add_argument("--mermaid", help="write the diagram to this file")
    p.add_argument("--json", help="write the full column-level graph as JSON")
    p.set_defaults(func=_cmd_lineage)

    p = sub.add_parser("impact", help="list everything built from a column (table.column)")
    with_models(p)
    p.add_argument("column")
    p.set_defaults(func=_cmd_impact)

    p = sub.add_parser("tags", help="show where tagged columns (e.g. pii) end up")
    with_models(p)
    p.set_defaults(func=_cmd_tags)

    p = sub.add_parser("coverage", help="which model columns have a data-quality check")
    with_models(p)
    p.add_argument("configs", nargs="+", help="quality YAML configs")
    p.add_argument("--fail-under", type=float, help="exit 1 if overall coverage %% is below this")
    p.set_defaults(func=_cmd_coverage)

    p = sub.add_parser("demo", help="synthetic warehouse with injected defects, then check it")
    p.add_argument("--workdir", default="demo_output")
    p.add_argument("--examples", default=str(EXAMPLES))
    p.add_argument("--clean", action="store_true", help="no defects (should pass)")
    p.set_defaults(func=_cmd_demo)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, LineageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
