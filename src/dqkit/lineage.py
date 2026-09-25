"""Column-level lineage from a folder of SQL models, using sqlglot.

Models are dbt-style: one SELECT per file, the file name is the model name, and `ref()`, `source()`
and `config()` Jinja calls are supported (nothing else is rendered). The graph records, for every
model column, which upstream columns it is built from and whether it is a plain copy ("direct") or
computed ("derived"), then answers impact questions and propagates tags such as `pii`.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify

DIRECT, DERIVED = "direct", "derived"

_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_CONFIG = re.compile(r"\{\{\s*config\([^)]*\)\s*\}\}")
_REF = re.compile(r"\{\{\s*ref\(\s*['\"](\w+)['\"]\s*\)\s*\}\}")
_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)\s*\}\}")


class LineageError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class ColumnRef:
    table: str
    column: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"

    @classmethod
    def parse(cls, text: str) -> ColumnRef:
        table, sep, column = text.strip().rpartition(".")
        if not sep or not table or not column:
            raise LineageError(f"expected table.column, got {text!r}")
        return cls(table, column)


@dataclass(frozen=True, order=True)
class ColumnEdge:
    source: ColumnRef
    target: ColumnRef
    kind: str  # DIRECT: plain copy of the source column; DERIVED: computed from it


@dataclass(frozen=True)
class Impact:
    column: ColumnRef
    depth: int
    kind: str  # DIRECT only if a path of pure copies reaches it


@dataclass
class LineageGraph:
    models: dict[str, str] = field(default_factory=dict)  # model -> rendered SQL
    order: list[str] = field(default_factory=list)  # models, upstream first
    model_columns: dict[str, list[str]] = field(default_factory=dict)
    source_columns: dict[str, list[str]] = field(default_factory=dict)
    table_edges: set[tuple[str, str]] = field(default_factory=set)  # (upstream, downstream)
    column_edges: set[ColumnEdge] = field(default_factory=set)
    tags: dict[ColumnRef, set[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # ---- traversal -------------------------------------------------------------------------
    def _walk(self, start: ColumnRef, forward: bool) -> list[Impact]:
        """Breadth-first over column edges. Tracks whether a pure-copy path exists to each column."""
        step: dict[ColumnRef, list[ColumnEdge]] = {}
        for edge in self.column_edges:
            step.setdefault(edge.source if forward else edge.target, []).append(edge)
        best: dict[ColumnRef, tuple[int, bool]] = {}
        seen: set[tuple[ColumnRef, bool]] = {(start, True)}
        queue = deque([(start, 0, True)])
        while queue:
            node, depth, all_direct = queue.popleft()
            for edge in step.get(node, []):
                nxt = edge.target if forward else edge.source
                direct = all_direct and edge.kind == DIRECT
                if (nxt, direct) in seen:
                    continue
                seen.add((nxt, direct))
                prev = best.get(nxt)
                best[nxt] = (
                    min(prev[0], depth + 1) if prev else depth + 1,
                    direct or (prev[1] if prev else False),
                )
                queue.append((nxt, depth + 1, direct))
        best.pop(start, None)
        return sorted(
            (Impact(c, d, DIRECT if direct else DERIVED) for c, (d, direct) in best.items()),
            key=lambda i: (i.depth, i.column),
        )

    def downstream(self, ref: ColumnRef) -> list[Impact]:
        """Every column that is (transitively) built from `ref`."""
        return self._walk(ref, forward=True)

    def upstream(self, ref: ColumnRef) -> list[Impact]:
        """Every column that `ref` is (transitively) built from."""
        return self._walk(ref, forward=False)

    def root_sources(self, ref: ColumnRef) -> list[ColumnRef]:
        """Upstream columns that live in source tables, not in models."""
        return sorted(i.column for i in self.upstream(ref) if i.column.table in self.source_columns)

    def downstream_tables(self, table: str) -> list[str]:
        out, queue = set(), deque([table])
        while queue:
            current = queue.popleft()
            for up, down in self.table_edges:
                if up == current and down not in out:
                    out.add(down)
                    queue.append(down)
        return sorted(out)

    # ---- tags --------------------------------------------------------------------------------
    def inherited_tags(self) -> dict[ColumnRef, dict[str, str]]:
        """Tags that reach each downstream column: {column: {tag: direct|derived}}."""
        result: dict[ColumnRef, dict[str, str]] = {}
        for origin, tags in self.tags.items():
            for hit in self.downstream(origin):
                slot = result.setdefault(hit.column, {})
                for tag in tags:
                    # a pure copy anywhere outranks a derived path for the same tag
                    if slot.get(tag) != DIRECT:
                        slot[tag] = hit.kind
        return result

    # ---- output ------------------------------------------------------------------------------
    def to_mermaid(self) -> str:
        lines = ["flowchart LR"]
        for name in sorted(self.source_columns):
            lines.append(f"    {name}[({name})]:::source")
        for name in sorted(self.models):
            lines.append(f"    {name}[{name}]")
        lines += [f"    {up} --> {down}" for up, down in sorted(self.table_edges)]
        lines.append("    classDef source fill:#eef,stroke:#88a")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        inherited = self.inherited_tags()
        return {
            "sources": {k: sorted(v) for k, v in sorted(self.source_columns.items())},
            "models": {k: v for k, v in sorted(self.model_columns.items())},
            "table_edges": [list(e) for e in sorted(self.table_edges)],
            "column_edges": [
                {"from": str(e.source), "to": str(e.target), "kind": e.kind}
                for e in sorted(self.column_edges)
            ],
            "tags": {str(c): sorted(t) for c, t in sorted(self.tags.items())},
            "inherited_tags": {str(c): t for c, t in sorted(inherited.items())},
            "warnings": self.warnings,
        }


# ---- building the graph ---------------------------------------------------------------------


def render(sql: str, model: str) -> str:
    """Replace ref()/source() with plain table names; refuse any other Jinja."""
    sql = _CONFIG.sub("", _COMMENT.sub("", sql))
    sql = _SOURCE.sub(lambda m: f"{m.group(1)}_{m.group(2)}", _REF.sub(r"\1", sql))
    if "{{" in sql or "{%" in sql:
        raise LineageError(f"{model}: unsupported Jinja; only ref(), source() and config() can be rendered")
    return sql.strip().rstrip(";")


def load_models(models_dir: str | Path) -> dict[str, str]:
    root = Path(models_dir)
    if not root.is_dir():
        raise LineageError(f"models directory not found: {root}")
    models: dict[str, str] = {}
    for path in sorted(root.rglob("*.sql")):
        if path.stem in models:
            raise LineageError(f"duplicate model name {path.stem!r}")
        models[path.stem] = render(path.read_text(encoding="utf-8"), path.stem)
    if not models:
        raise LineageError(f"no .sql files under {root}")
    return models


def load_sources(path: str | Path | None) -> tuple[dict[str, dict[str, str]], dict[ColumnRef, set[str]]]:
    """Read source schemas: {table: {column: type}} plus tags. Columns may be `name: type` or
    `name: {type: ..., tags: [pii]}`."""
    if path is None:
        return {}, {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    schemas: dict[str, dict[str, str]] = {}
    tags: dict[ColumnRef, set[str]] = {}
    for table, spec in (raw.get("sources") or {}).items():
        schemas[table] = {}
        for column, col_spec in (spec.get("columns") or {}).items():
            if isinstance(col_spec, dict):
                schemas[table][column] = str(col_spec.get("type", "text"))
                if col_spec.get("tags"):
                    tags[ColumnRef(table, column)] = set(col_spec["tags"])
            else:
                schemas[table][column] = str(col_spec)
    return schemas, tags


def _referenced_tables(expression: exp.Expression) -> set[str]:
    ctes = {cte.alias for cte in expression.find_all(exp.CTE)}
    return {t.name for t in expression.find_all(exp.Table) if t.name and t.name not in ctes}


def _topological(deps: dict[str, set[str]]) -> list[str]:
    remaining = {m: set(d) for m, d in deps.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(m for m, d in remaining.items() if not d)
        if not ready:
            raise LineageError(f"circular dependency among models: {sorted(remaining)}")
        order += ready
        for m in ready:
            del remaining[m]
        for d in remaining.values():
            d.difference_update(ready)
    return order


def _is_copy(expression: exp.Expression | None) -> bool:
    """True if a select item is just a column reference (optionally re-aliased)."""
    inner = expression.this if isinstance(expression, exp.Alias) else expression
    return isinstance(inner, exp.Column)


def _leaf_paths(node, derived: bool = False):
    """Yield (leaf_node, derived) for every source leaf below `node`.

    `derived` becomes true if any select item on the way down computes something, including inside
    CTEs and subqueries, so `WITH x AS (SELECT a * 2 AS b ...) SELECT b` is not mistaken for a copy.
    """
    if not isinstance(node.expression, exp.Table):
        derived = derived or not _is_copy(node.expression)
    if not node.downstream:
        yield node, derived
    for child in node.downstream:
        yield from _leaf_paths(child, derived)


def build_graph(
    models_dir: str | Path, sources_file: str | Path | None = None, dialect: str = "duckdb"
) -> LineageGraph:
    models = load_models(models_dir)
    source_schemas, tags = load_sources(sources_file)
    graph = LineageGraph(models=models, tags=tags)
    graph.source_columns = {t: list(cols) for t, cols in source_schemas.items()}

    parsed: dict[str, exp.Expression] = {}
    for name, sql in models.items():
        try:
            parsed[name] = parse_one(sql, dialect=dialect)
        except SqlglotError as exc:
            raise LineageError(f"{name}: cannot parse SQL: {exc}") from exc

    deps = {name: _referenced_tables(tree) for name, tree in parsed.items()}
    known = set(models) | set(source_schemas)
    for name, tables in deps.items():
        for table in sorted(tables - known):
            graph.warnings.append(
                f"{name}: table {table!r} is neither a model nor a declared source; "
                "its columns cannot be resolved"
            )
            graph.source_columns.setdefault(table, [])
        graph.table_edges.update((t, name) for t in tables)

    schema: dict[str, dict[str, str]] = {t: dict(c) for t, c in source_schemas.items()}
    graph.order = _topological({m: d & set(models) for m, d in deps.items()})
    for name in graph.order:
        try:
            qualified = qualify(
                parsed[name].copy(), schema=schema, dialect=dialect, validate_qualify_columns=True
            )
        except SqlglotError as exc:
            raise LineageError(f"{name}: cannot resolve columns: {exc}") from exc
        columns = list(qualified.named_selects)
        if "*" in columns:
            graph.warnings.append(f"{name}: SELECT * over an undeclared table; skipped")
            columns = [c for c in columns if c != "*"]
        graph.model_columns[name] = columns
        schema[name] = {c: "text" for c in columns}

        for column in columns:
            try:
                root = lineage(column, models[name], schema=schema, dialect=dialect)
            except SqlglotError as exc:
                raise LineageError(f"{name}.{column}: cannot trace lineage: {exc}") from exc
            for leaf, derived in _leaf_paths(root):
                if not isinstance(leaf.source, exp.Table):
                    continue
                source = ColumnRef(leaf.source.name, leaf.name.rsplit(".", 1)[-1])
                kind = DERIVED if derived else DIRECT
                graph.column_edges.add(ColumnEdge(source, ColumnRef(name, column), kind))
    return graph
