import json
from datetime import datetime, timezone

import duckdb
import pytest

from dqkit.quality import (
    ERROR,
    FAIL,
    PASS,
    WARN,
    ConfigError,
    append_history,
    load_baseline,
    parse_dataset_config,
    run_dataset,
)

NOW = datetime(2024, 6, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE people (id INTEGER, email VARCHAR, age INTEGER, status VARCHAR,
                             score DOUBLE, updated_at TIMESTAMP, dept_id INTEGER)
    """)
    c.execute("""
        INSERT INTO people VALUES
          (1, 'a@x.com', 30, 'ACTIVE',   1.5, '2024-06-10 10:00:00', 10),
          (2, 'b@x.com', 41, 'ACTIVE',   2.5, '2024-06-10 09:00:00', 10),
          (3, 'bad',     -5, 'PENDING',  NULL, '2024-06-09 09:00:00', 99),
          (3, NULL,      200, 'UNKNOWN', 4.0, '2024-06-08 09:00:00', NULL),
          (5, 'e@x.com', NULL, 'ACTIVE', 5.0, '2024-06-10 11:00:00', 20)
    """)
    c.execute("CREATE TABLE depts (dept_id INTEGER)")
    c.execute("INSERT INTO depts VALUES (10), (20)")
    yield c
    c.close()


def run(con, *checks, sample_size=5, now=NOW, baseline=None):
    cfg = parse_dataset_config({"table": "people", "checks": list(checks), "sample_size": sample_size})
    return run_dataset(con, cfg, now, baseline).results


def one(con, check, **kw):
    (result,) = run(con, check, **kw)
    return result


def test_not_null(con):
    r = one(con, {"type": "not_null", "column": "email"})
    assert (r.status, r.failing, r.total) == (FAIL, 1, 5)
    assert one(con, {"type": "not_null", "column": "id"}).status == PASS


def test_unique_counts_surplus_rows_and_samples_the_duplicates(con):
    r = one(con, {"type": "unique", "columns": ["id"]})
    assert (r.status, r.failing) == (FAIL, 1)
    assert r.samples == [{"id": 3, "occurrences": 2}]
    assert one(con, {"type": "unique", "columns": ["id", "status"]}).status == PASS  # composite key


def test_accepted_values_ignores_nulls_and_is_injection_safe(con):
    r = one(con, {"type": "accepted_values", "column": "status", "values": ["ACTIVE", "PENDING", "O'Brien"]})
    assert (r.status, r.failing) == (FAIL, 1)  # only 'UNKNOWN'


def test_range_min_max_and_null_skipped(con):
    r = one(con, {"type": "range", "column": "age", "min": 0, "max": 120})
    assert (r.status, r.failing) == (FAIL, 2)  # -5 and 200; the NULL is not a range violation
    assert one(con, {"type": "range", "column": "age", "min": -10}).failing == 0  # no max given
    assert one(con, {"type": "range", "column": "age", "max": 100}).failing == 1  # only 200


def test_regex(con):
    r = one(con, {"type": "regex", "column": "email", "pattern": r"^[^@]+@[^@]+\.[a-z]+$"})
    assert (r.status, r.failing) == (FAIL, 1)


def test_relationships_skips_nulls_and_counts_orphans(con):
    r = one(con, {"type": "relationships", "column": "dept_id", "to_table": "depts", "to_column": "dept_id"})
    assert (r.status, r.failing, r.total) == (FAIL, 1, 4)  # dept 99; the NULL is excluded from the total


def test_tolerance_lets_a_small_failure_share_pass_but_reports_it(con):
    r = one(con, {"type": "not_null", "column": "email", "max_failure_pct": 25})
    assert (r.status, r.failing) == (PASS, 1)
    assert "within the 25% tolerance" in r.detail


def test_warn_severity_does_not_fail_the_dataset(con):
    cfg = parse_dataset_config(
        {"table": "people", "checks": [{"type": "not_null", "column": "email", "severity": "warn"}]}
    )
    result = run_dataset(con, cfg, NOW)
    assert result.results[0].status == WARN and result.status == PASS


def test_row_count_bounds_report_the_value(con):
    r = one(con, {"type": "row_count", "min": 10})
    assert (r.status, r.value) == (FAIL, 5.0)
    assert one(con, {"type": "row_count", "min": 1, "max": 5}).status == PASS


def test_row_count_change_uses_history(con, tmp_path):
    check = {"type": "row_count_change", "max_pct": 10}
    assert "no baseline" in one(con, check).detail and one(con, check).status == PASS
    assert one(con, check, baseline={"row_count_change": 5}).status == PASS
    r = one(con, check, baseline={"row_count_change": 10})  # 5 vs 10 = -50%
    assert r.status == FAIL and "-50.0%" in r.detail

    result = run_dataset(con, parse_dataset_config({"table": "people", "checks": [check]}), NOW)
    log = tmp_path / "h" / "history.jsonl"
    append_history(log, result)
    result.dataset = "other"
    append_history(log, result)
    assert load_baseline(log, "people") == {"row_count_change": 5.0}
    assert load_baseline(log, "unseen") == {} and load_baseline(tmp_path / "missing", "people") == {}


def test_freshness_is_measured_against_the_supplied_now(con):
    check = {"type": "freshness", "column": "updated_at", "max_age_hours": 2}
    r = one(con, check)  # newest row is 11:00, now is 12:00
    assert (r.status, r.value) == (PASS, 1.0)
    late = one(con, check, now=datetime(2024, 6, 10, 20, 0))  # naive `now` is treated as UTC
    assert late.status == FAIL and "limit 2h" in late.detail


def test_freshness_on_an_empty_table_fails(con):
    con.execute("DELETE FROM people")
    assert one(con, {"type": "freshness", "column": "updated_at", "max_age_hours": 1}).status == FAIL


def test_schema_contract(con):
    ok = {"id": "integer", "email": "string", "age": "integer", "status": "string",
          "score": "float", "updated_at": "timestamp", "dept_id": "integer"}  # fmt: skip
    assert one(con, {"type": "schema_contract", "columns": ok}).status == PASS

    broken = {**ok, "age": "string", "ghost": "integer"}
    del broken["dept_id"]
    r = one(con, {"type": "schema_contract", "columns": broken})
    assert r.status == FAIL
    assert "missing column ghost" in r.detail and "age is integer" in r.detail
    assert "unexpected column dept_id" in r.detail
    assert (
        one(con, {"type": "schema_contract", "columns": {"id": "integer"}, "allow_extra": True}).status
        == PASS
    )


def test_custom_sql_counts_returned_rows(con):
    r = one(con, {"type": "custom_sql", "name": "old_and_active",
                  "sql": "SELECT id FROM people WHERE status = 'ACTIVE' AND age > 40;"})  # fmt: skip
    assert (r.name, r.status, r.failing) == ("old_and_active", FAIL, 1)


def test_a_broken_check_is_an_error_and_the_others_still_run(con):
    results = run(
        con,
        {"type": "not_null", "column": "nope"},
        {"type": "not_null", "column": "id"},
        {"type": "custom_sql", "name": "bad", "sql": "SELECT * FROM missing_table"},
    )
    assert [r.status for r in results] == [ERROR, PASS, ERROR]
    assert "nope" in results[0].detail


def test_missing_table_is_a_single_error(con):
    cfg = parse_dataset_config({"table": "no_such_table", "checks": [{"type": "not_null", "column": "id"}]})
    result = run_dataset(con, cfg, NOW)
    assert result.status == ERROR and len(result.results) == 1


def test_sample_size_zero_returns_no_samples(con):
    assert one(con, {"type": "not_null", "column": "email"}, sample_size=0).samples == []


@pytest.mark.parametrize(
    "raw",
    [
        {"checks": [{"type": "not_null", "column": "a"}]},  # no table
        {"table": "t; DROP TABLE x", "checks": [{"type": "not_null", "column": "a"}]},
        {"table": "t", "checks": []},
        {"table": "t", "checks": [{"type": "nope"}]},
        {"table": "t", "checks": [{"type": "not_null"}]},  # missing column
        {"table": "t", "checks": [{"type": "not_null", "column": "a", "severity": "loud"}]},
        {"table": "t", "checks": [{"type": "unique", "columns": "a"}]},
        {
            "table": "t",
            "checks": [{"type": "relationships", "column": "a", "to_table": "x y", "to_column": "b"}],
        },
    ],
)
def test_config_validation(raw):
    with pytest.raises(ConfigError):
        parse_dataset_config(raw)


def test_results_are_json_serialisable(con):
    results = run(
        con,
        {"type": "unique", "columns": ["id"]},
        {"type": "freshness", "column": "updated_at", "max_age_hours": 1},
    )
    json.dumps([r.__dict__ for r in results], default=str)
