"""The demo injects a known number of defects; every check must find exactly that many."""

import json
from pathlib import Path

import duckdb
import pytest

from dqkit import coverage as cov
from dqkit.cli import main, run_checks
from dqkit.demo import DEFECTS, NOW
from dqkit.lineage import build_graph
from dqkit.quality import FAIL, PASS, WARN, load_baseline, load_dataset_config, run_dataset

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CONFIGS = sorted(str(p) for p in (EXAMPLES / "quality").glob("*.yaml"))


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    work = tmp_path_factory.mktemp("demo")
    assert main(["demo", "--workdir", str(work)]) == 1
    return work


def results_for(work, db="warehouse.duckdb", history=None):
    return {
        r.dataset: {c.name: c for c in r.results} for r in run_checks(CONFIGS, str(work / db), history, NOW)
    }


def test_stg_orders_findings_match_injected_defects(demo):
    c = results_for(demo)["stg_orders"]
    assert c["unique:order_id"].failing == DEFECTS["duplicate_orders"]
    assert c["accepted_values:status"].failing == DEFECTS["bad_status"]
    assert c["range:amount"].failing == DEFECTS["negative_amount"]
    assert c["relationships:customer_id"].failing == DEFECTS["orphan_customer"]  # NULLs are not orphans
    assert c["discount_not_above_amount"].failing == DEFECTS["discount_above_amount"]
    assert c["freshness:order_ts"].status == FAIL  # the feed stopped two days early
    assert c["not_null:order_id"].status == PASS and c["row_count"].status == PASS


def test_null_customers_are_tolerated_but_still_reported(demo):
    r = results_for(demo)["stg_orders"]["not_null:customer_id"]
    assert (r.status, r.failing) == (PASS, DEFECTS["null_customer"])
    assert "within the 1% tolerance" in r.detail


def test_customer_findings(demo):
    c = results_for(demo)["stg_customers"]
    assert c["accepted_values:country"].failing == DEFECTS["bad_country"]
    assert c["regex:email"].failing == DEFECTS["bad_email"]
    assert c["unique:customer_id"].status == PASS
    assert c["schema_contract"].status == PASS


def test_messy_values_cleaned_in_staging(demo):
    """Padded/upper-cased text was injected into 'clean' rows; staging must absorb it."""
    c = results_for(demo)
    assert c["stg_orders"]["accepted_values:status"].failing == DEFECTS["bad_status"]  # not +30 padded
    assert c["stg_customers"]["regex:email"].failing == DEFECTS["bad_email"]  # not +10 upper-case


def test_defects_propagate_to_the_fact_and_the_mart(demo):
    r = results_for(demo)
    fct = r["fct_orders"]
    assert fct["unique:order_id"].failing == DEFECTS["duplicate_orders"]
    assert fct["range:net_amount"].failing == DEFECTS["discount_above_amount"]
    assert (fct["not_null:country"].status, fct["not_null:country"].failing) == (
        WARN, DEFECTS["null_customer"] + DEFECTS["orphan_customer"]
    )  # fmt: skip
    assert r["mart_country_revenue"]["not_null:country"].failing == 1  # the NULL-country group


def test_row_count_change_baseline(demo):
    """The demo checks a clean load first and records it, then checks the defective load against it."""
    history = [json.loads(line) for line in (demo / "history.jsonl").read_text().splitlines()]
    stg = [h["metrics"]["row_count_change"] for h in history if h["dataset"] == "stg_orders"]
    assert stg == [2000.0, 2005.0]  # clean baseline, then baseline + 5 duplicate orders

    baseline = load_baseline(demo / "history.jsonl", "stg_orders")  # latest recorded run
    cfg = load_dataset_config(EXAMPLES / "quality" / "stg_orders.yaml")
    con = duckdb.connect(str(demo / "warehouse.duckdb"), read_only=True)
    try:
        result = run_dataset(con, cfg, NOW, {"row_count_change": 2000.0})
    finally:
        con.close()
    change = next(c for c in result.results if c.name == "row_count_change")
    assert change.status == PASS and "2005 rows vs 2000 before (+0.2%)" in change.detail
    assert baseline == {"row_count_change": 2005.0}


def test_report_and_artifacts_are_written(demo):
    assert "# Data quality report" in (demo / "quality_report.md").read_text()
    assert (demo / "lineage.md").read_text().startswith("```mermaid")
    data = json.loads((demo / "lineage.json").read_text())
    assert data["inherited_tags"]["customer_contact_list.email"]["pii"] == "derived"
    assert "Column coverage" in (demo / "coverage.md").read_text()


def test_clean_demo_has_no_findings_at_all(tmp_path):
    assert main(["demo", "--workdir", str(tmp_path), "--clean"]) == 0
    for datasets in results_for(tmp_path).values():
        assert {c.status for c in datasets.values()} == {PASS}
        assert all(not c.failing for c in datasets.values() if c.failing is not None)


def test_coverage_finds_untested_columns_and_models():
    graph = build_graph(EXAMPLES / "models", EXAMPLES / "sources.yml")
    items = {m.model: m for m in cov.coverage(graph, [load_dataset_config(p) for p in CONFIGS])}
    assert items["stg_orders"].uncovered == ["discount"]  # custom_sql names no column
    assert items["dim_customer"].has_config is False and items["dim_customer"].pct == 0.0
    assert items["stg_customers"].uncovered == []
    assert 0 < cov.overall_pct(list(items.values())) < 100


def test_cli_coverage_threshold(capsys):
    base = ["coverage", str(EXAMPLES / "models"), "--sources", str(EXAMPLES / "sources.yml"), *CONFIGS]
    assert main([*base, "--fail-under", "10"]) == 0
    assert main([*base, "--fail-under", "99"]) == 1
    assert "Column coverage" in capsys.readouterr().out


def test_cli_expands_wildcards_itself_and_reports_no_match(capsys):
    args = ["coverage", str(EXAMPLES / "models"), "--sources", str(EXAMPLES / "sources.yml")]
    assert main([*args, str(EXAMPLES / "quality" / "*.yaml")]) == 0
    assert "stg_orders" in capsys.readouterr().out
    assert main([*args, str(EXAMPLES / "quality" / "*.nope")]) == 2
    assert "no files match" in capsys.readouterr().err


def test_cli_impact_and_tags(capsys):
    args = [str(EXAMPLES / "models"), "--sources", str(EXAMPLES / "sources.yml")]
    assert main(["impact", *args, "raw_orders.amount"]) == 0
    out = capsys.readouterr().out
    assert "mart_country_revenue.revenue" in out and "4 column(s) in 3 model(s)" in out
    assert main(["tags", *args]) == 0
    assert "customer_contact_list.email" in capsys.readouterr().out
    assert main(["impact", *args, "raw_orders.nope"]) == 2


def test_cli_lineage_writes_files(tmp_path, capsys):
    args = ["lineage", str(EXAMPLES / "models"), "--sources", str(EXAMPLES / "sources.yml")]
    assert main([*args, "--mermaid", str(tmp_path / "l.md"), "--json", str(tmp_path / "l.json")]) == 0
    assert "raw_orders --> stg_orders" in (tmp_path / "l.md").read_text()
    assert json.loads((tmp_path / "l.json").read_text())["column_edges"]
    capsys.readouterr()


def test_cli_check_json_and_bad_inputs(demo, tmp_path, capsys):
    out = tmp_path / "r.json"
    code = main(["check", CONFIGS[0], "--database", str(demo / "warehouse.duckdb"),
                 "--now", "2024-06-07T00:00:00", "--json", str(out)])  # fmt: skip
    assert code == 1 and json.loads(out.read_text())[0]["status"] == "FAIL"
    assert main(["check", CONFIGS[0]]) == 2  # no database given
    assert (
        main(["check", CONFIGS[0], "--database", str(demo / "warehouse.duckdb"), "--now", "yesterday-ish"])
        == 2
    )
    err = capsys.readouterr().err
    assert "no database" in err and "--now must be an ISO timestamp" in err
