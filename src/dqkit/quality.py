"""Declarative data-quality checks compiled to SQL and run on DuckDB.

A dataset config lists checks; each check counts the rows (or values) that violate it. A check
passes when the failing share is within `max_failure_pct` (default 0). Failures of severity `warn`
are reported but do not fail the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PASS, FAIL, WARN, ERROR = "PASS", "FAIL", "WARN", "ERROR"
_TABLE_RE = re.compile(r"^[A-Za-z0-9_.]+$")

# check type -> required keys
CHECK_TYPES: dict[str, tuple[str, ...]] = {
    "not_null": ("column",),
    "unique": ("columns",),
    "accepted_values": ("column", "values"),
    "range": ("column",),
    "regex": ("column", "pattern"),
    "relationships": ("column", "to_table", "to_column"),
    "row_count": (),
    "row_count_change": ("max_pct",),
    "freshness": ("column", "max_age_hours"),
    "schema_contract": ("columns",),
    "custom_sql": ("name", "sql"),
}


class ConfigError(ValueError):
    pass


@dataclass
class DatasetConfig:
    dataset: str
    table: str
    database: str | None
    checks: list[dict[str, Any]]
    sample_size: int = 5


@dataclass
class CheckResult:
    name: str
    type: str
    status: str
    severity: str = "error"
    failing: int | None = None
    total: int | None = None
    value: float | None = None  # metric a later run can compare against (row counts)
    detail: str = ""
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failure_pct(self) -> float | None:
        if self.failing is None or not self.total:
            return None
        return round(100.0 * self.failing / self.total, 4)


@dataclass
class DatasetResult:
    dataset: str
    table: str
    started_at: datetime
    results: list[CheckResult]

    @property
    def status(self) -> str:
        statuses = {r.status for r in self.results}
        return ERROR if ERROR in statuses else FAIL if FAIL in statuses else PASS

    @property
    def counts(self) -> dict[str, int]:
        return {s: sum(r.status == s for r in self.results) for s in (PASS, FAIL, WARN, ERROR)}


# ---- configuration ----------------------------------------------------------------------------


def parse_dataset_config(raw: Any) -> DatasetConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    table = raw.get("table")
    if not isinstance(table, str) or not _TABLE_RE.match(table):
        raise ConfigError(f"table must match {_TABLE_RE.pattern}, got {table!r}")
    checks = raw.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ConfigError("checks must be a non-empty list")
    for i, check in enumerate(checks):
        if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
            raise ConfigError(f"checks[{i}]: type must be one of {sorted(CHECK_TYPES)}")
        for key in CHECK_TYPES[check["type"]]:
            if key not in check:
                raise ConfigError(f"checks[{i}] ({check['type']}): missing required key {key!r}")
        if check.get("severity", "error") not in ("error", "warn"):
            raise ConfigError(f"checks[{i}]: severity must be error or warn")
        if check["type"] == "relationships" and not _TABLE_RE.match(str(check["to_table"])):
            raise ConfigError(f"checks[{i}]: invalid to_table {check['to_table']!r}")
        if check["type"] == "unique" and not (isinstance(check["columns"], list) and check["columns"]):
            raise ConfigError(f"checks[{i}]: unique.columns must be a non-empty list")
    return DatasetConfig(
        dataset=str(raw.get("dataset", table)),
        table=table,
        database=raw.get("database"),
        checks=checks,
        sample_size=int(raw.get("sample_size", 5)),
    )


def load_dataset_config(path: str | Path) -> DatasetConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_dataset_config(raw)


def columns_covered(check: dict[str, Any]) -> set[str]:
    """Lower-cased column names a check makes a statement about (used by the coverage report)."""
    kind = check["type"]
    if kind == "unique":
        return {c.lower() for c in check["columns"]}
    if kind == "schema_contract":
        return {c.lower() for c in check["columns"]}
    if "column" in check:
        return {str(check["column"]).lower()}
    return set()


# ---- SQL helpers ------------------------------------------------------------------------------


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _family(duck_type: str) -> str:
    t = duck_type.upper()
    if t.startswith(("DECIMAL", "NUMERIC")):
        return "decimal"
    if t.startswith(("DOUBLE", "FLOAT", "REAL")):
        return "float"
    if "INT" in t and "INTERVAL" not in t:
        return "integer"
    if t.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return "string"
    if t.startswith("BOOL"):
        return "boolean"
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP"):
        return "timestamp"
    return "other"


def check_name(check: dict[str, Any]) -> str:
    if check.get("name"):
        return str(check["name"])
    kind = check["type"]
    target = check.get("column") or ",".join(check.get("columns") or []) if kind != "schema_contract" else ""
    return f"{kind}:{target}" if target else kind


# ---- engine -----------------------------------------------------------------------------------


class Runner:
    def __init__(self, con, cfg: DatasetConfig, now: datetime, baseline: dict[str, float]):
        self.con, self.cfg, self.now, self.baseline = con, cfg, now, baseline
        self.table = cfg.table
        self.columns = {r[0].lower(): r[0] for r in con.execute(f"DESCRIBE {self.table}").fetchall()}
        self.types = {r[0].lower(): r[1] for r in con.execute(f"DESCRIBE {self.table}").fetchall()}

    def scalar(self, sql: str):
        return self.con.execute(sql).fetchone()[0]

    def rows(self, sql: str) -> list[dict[str, Any]]:
        cur = self.con.execute(sql)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def col(self, name: str) -> str:
        actual = self.columns.get(str(name).lower())
        if actual is None:
            raise KeyError(f"column {name!r} not found in {self.table}")
        return _q(actual)

    def total(self) -> int:
        return int(self.scalar(f"SELECT COUNT(*) FROM {self.table}"))

    # -- row-level checks: a predicate that is true for violating rows --
    def _predicate(self, check: dict[str, Any]) -> str | None:
        kind = check["type"]
        if kind == "not_null":
            return f"{self.col(check['column'])} IS NULL"
        if kind == "accepted_values":
            values = ", ".join(_lit(v) for v in check["values"])
            c = self.col(check["column"])
            return f"{c} IS NOT NULL AND {c} NOT IN ({values})"
        if kind == "range":
            c, parts = self.col(check["column"]), []
            if check.get("min") is not None:
                parts.append(f"{c} < {_lit(check['min'])}")
            if check.get("max") is not None:
                parts.append(f"{c} > {_lit(check['max'])}")
            if not parts:
                raise ValueError("range needs min and/or max")
            return f"{c} IS NOT NULL AND ({' OR '.join(parts)})"
        if kind == "regex":
            c = self.col(check["column"])
            return f"{c} IS NOT NULL AND NOT regexp_matches(CAST({c} AS VARCHAR), {_lit(check['pattern'])})"
        return None

    def _finish(self, check, failing: int, total: int, samples=None, detail="") -> CheckResult:
        tolerance = float(check.get("max_failure_pct", 0))
        pct = 100.0 * failing / total if total else 0.0
        severity = check.get("severity", "error")
        ok = failing == 0 or pct <= tolerance
        status = PASS if ok else (FAIL if severity == "error" else WARN)
        if not detail and failing:
            detail = (
                f"{failing} of {total} violate ({pct:.2f}% > {tolerance:g}% allowed)"
                if not ok
                else f"{failing} of {total} violate, within the {tolerance:g}% tolerance"
            )
        return CheckResult(
            check_name(check), check["type"], status, severity, failing, total,
            detail=detail, samples=samples or [],
        )  # fmt: skip

    def run(self, check: dict[str, Any]) -> CheckResult:
        kind, n = check["type"], self.cfg.sample_size
        predicate = self._predicate(check)
        if predicate is not None:
            failing = int(self.scalar(f"SELECT COUNT(*) FROM {self.table} WHERE {predicate}"))
            samples = self.rows(f"SELECT * FROM {self.table} WHERE {predicate} LIMIT {n}") if failing else []
            return self._finish(check, failing, self.total(), samples)

        if kind == "unique":
            cols = ", ".join(self.col(c) for c in check["columns"])
            groups = (
                f"SELECT {cols}, COUNT(*) AS occurrences FROM {self.table} "
                f"GROUP BY {cols} HAVING COUNT(*) > 1"
            )
            failing = int(self.scalar(f"SELECT COALESCE(SUM(occurrences - 1), 0) FROM ({groups})"))
            samples = self.rows(f"{groups} ORDER BY occurrences DESC, {cols} LIMIT {n}") if failing else []
            return self._finish(
                check, failing, self.total(), samples,
                detail=f"{failing} surplus row(s) share a key" if failing else "",
            )  # fmt: skip

        if kind == "relationships":
            c, tc = self.col(check["column"]), _q(check["to_column"])
            parent = f"SELECT {tc} FROM {check['to_table']} WHERE {tc} IS NOT NULL"
            where = f"{c} IS NOT NULL AND {c} NOT IN ({parent})"
            failing = int(self.scalar(f"SELECT COUNT(*) FROM {self.table} WHERE {where}"))
            total = int(self.scalar(f"SELECT COUNT(*) FROM {self.table} WHERE {c} IS NOT NULL"))
            samples = self.rows(f"SELECT * FROM {self.table} WHERE {where} LIMIT {n}") if failing else []
            return self._finish(check, failing, total, samples)

        if kind == "custom_sql":
            body = str(check["sql"]).strip().rstrip(";")
            failing = int(self.scalar(f"SELECT COUNT(*) FROM ({body})"))
            samples = self.rows(f"SELECT * FROM ({body}) LIMIT {n}") if failing else []
            return self._finish(check, failing, self.total(), samples)

        if kind == "row_count":
            count = self.total()
            lo, hi = check.get("min"), check.get("max")
            bad = (lo is not None and count < lo) or (hi is not None and count > hi)
            result = self._finish(check, 1 if bad else 0, 1)
            result.value, result.failing, result.total = float(count), None, None
            result.detail = f"{count} rows, expected between {lo} and {hi}" if bad else f"{count} rows"
            return result

        if kind == "row_count_change":
            count = self.total()
            result = CheckResult(
                check_name(check), kind, PASS, check.get("severity", "error"), value=float(count)
            )
            previous = self.baseline.get(result.name)
            if previous is None:
                result.detail = f"{count} rows; no baseline yet (first run)"
            else:
                change = 100.0 * (count - previous) / previous if previous else float("inf")
                result.detail = f"{count} rows vs {previous:g} before ({change:+.1f}%)"
                if abs(change) > float(check["max_pct"]):
                    result.status = FAIL if result.severity == "error" else WARN
                    result.detail += f", beyond +/-{float(check['max_pct']):g}%"
            return result

        if kind == "freshness":
            latest = self.scalar(f"SELECT MAX({self.col(check['column'])}) FROM {self.table}")
            severity = check.get("severity", "error")
            if latest is None:
                return CheckResult(
                    check_name(check),
                    kind,
                    FAIL if severity == "error" else WARN,
                    severity,
                    detail="no data: column is entirely NULL or the table is empty",
                )
            if isinstance(latest, datetime):
                latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
            else:  # DATE
                latest_dt = datetime(latest.year, latest.month, latest.day, tzinfo=timezone.utc)
            age_hours = (self.now - latest_dt).total_seconds() / 3600
            limit = float(check["max_age_hours"])
            ok = age_hours <= limit
            return CheckResult(
                check_name(check), kind, PASS if ok else (FAIL if severity == "error" else WARN), severity,
                value=round(age_hours, 2),
                detail=f"latest {latest_dt:%Y-%m-%d %H:%M}, {age_hours:.1f}h old"
                + ("" if ok else f" (limit {limit:g}h)"),
            )  # fmt: skip

        if kind == "schema_contract":
            expected = {str(k).lower(): str(v).lower() for k, v in check["columns"].items()}
            problems = []
            for name, family in expected.items():
                if name not in self.types:
                    problems.append(f"missing column {name}")
                elif _family(self.types[name]) != family:
                    problems.append(f"{name} is {_family(self.types[name])}, contract says {family}")
            if not check.get("allow_extra", False):
                problems += [f"unexpected column {c}" for c in self.types if c not in expected]
            severity = check.get("severity", "error")
            return CheckResult(
                check_name(check), kind, PASS if not problems else (FAIL if severity == "error" else WARN),
                severity, failing=len(problems), total=len(expected),
                detail="; ".join(problems),
            )  # fmt: skip

        raise ValueError(f"unhandled check type {kind}")


def run_dataset(
    con, cfg: DatasetConfig, now: datetime | None = None, baseline: dict[str, float] | None = None
) -> DatasetResult:
    """Run every check. A check that cannot run reports ERROR; the others still run."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    started = datetime.now(timezone.utc)
    try:
        runner = Runner(con, cfg, now, baseline or {})
    except Exception as exc:
        failed = CheckResult("table", "table", ERROR, detail=f"{type(exc).__name__}: {exc}")
        return DatasetResult(cfg.dataset, cfg.table, started, [failed])
    results = []
    for check in cfg.checks:
        try:
            results.append(runner.run(check))
        except Exception as exc:
            results.append(
                CheckResult(check_name(check), check["type"], ERROR, check.get("severity", "error"),
                            detail=f"{type(exc).__name__}: {exc}")
            )  # fmt: skip
    return DatasetResult(cfg.dataset, cfg.table, started, results)


# ---- history (for row_count_change) -----------------------------------------------------------


def load_baseline(history_path: str | Path | None, dataset: str) -> dict[str, float]:
    """Metrics from the most recent recorded run of `dataset`."""
    if not history_path or not Path(history_path).exists():
        return {}
    baseline: dict[str, float] = {}
    for line in Path(history_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("dataset") == dataset:
            baseline = entry.get("metrics", {})
    return baseline


def append_history(history_path: str | Path, result: DatasetResult) -> None:
    metrics = {
        r.name: r.value for r in result.results if r.value is not None and r.type == "row_count_change"
    }
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"dataset": result.dataset, "run_at": result.started_at.isoformat(), "metrics": metrics}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
