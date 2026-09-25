import json
from pathlib import Path

import pytest

from dqkit.lineage import (
    DERIVED,
    DIRECT,
    ColumnRef,
    LineageError,
    build_graph,
    render,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def graph():
    return build_graph(EXAMPLES / "models", EXAMPLES / "sources.yml")


def models(tmp_path, **sql):
    for name, text in sql.items():
        (tmp_path / f"{name}.sql").write_text(text)
    return tmp_path


def sources(tmp_path, body):
    path = tmp_path / "sources.yml"
    path.write_text(body)
    return path


def test_render_ref_source_config():
    sql = (
        "{{ config(materialized='table') }}{# note #} "
        "select 1 from {{ ref('a') }} join {{ source('raw', 'b') }}"
    )
    assert render(sql, "m") == "select 1 from a join raw_b"


def test_render_refuses_other_jinja():
    with pytest.raises(LineageError, match="unsupported Jinja"):
        render("select {{ var('x') }} from t", "m")
    with pytest.raises(LineageError, match="unsupported Jinja"):
        render("{% if x %}select 1{% endif %}", "m")


def test_models_run_in_dependency_order(graph):
    pos = {m: i for i, m in enumerate(graph.order)}
    for up, down in graph.table_edges:
        if up in pos:
            assert pos[up] < pos[down]


def test_star_through_a_cte_is_expanded(graph):
    assert graph.model_columns["fct_orders"] == [
        "order_id", "customer_id", "country", "amount", "discount", "net_amount", "order_ts",
    ]  # fmt: skip


def test_copy_versus_computed_columns(graph):
    kinds = {(str(e.source), str(e.target)): e.kind for e in graph.column_edges}
    assert kinds[("stg_orders.amount", "fct_orders.amount")] == DIRECT
    assert kinds[("stg_orders.amount", "fct_orders.net_amount")] == DERIVED
    assert kinds[("stg_orders.discount", "fct_orders.net_amount")] == DERIVED
    assert kinds[("dim_customer.country", "fct_orders.country")] == DIRECT  # through join + CTE
    assert kinds[("raw_orders.discount", "stg_orders.discount")] == DERIVED  # coalesce()


def test_filter_only_columns_are_not_lineage(graph):
    """status decides which rows survive but does not feed any output value."""
    assert not [
        e
        for e in graph.column_edges
        if str(e.source) == "stg_orders.status" and e.target.table == "fct_orders"
    ]


def test_downstream_impact_is_transitive_and_ordered(graph):
    hits = graph.downstream(ColumnRef("raw_orders", "amount"))
    assert [(h.depth, str(h.column), h.kind) for h in hits] == [
        (1, "stg_orders.amount", DIRECT),
        (2, "fct_orders.amount", DIRECT),
        (2, "fct_orders.net_amount", DERIVED),
        (3, "mart_country_revenue.revenue", DERIVED),
    ]


def test_upstream_reaches_root_sources(graph):
    roots = graph.root_sources(ColumnRef("mart_country_revenue", "revenue"))
    assert [str(r) for r in roots] == ["raw_orders.amount", "raw_orders.discount"]


def test_downstream_tables(graph):
    assert graph.downstream_tables("stg_customers") == [
        "customer_contact_list", "dim_customer", "fct_orders", "mart_country_revenue",
    ]  # fmt: skip


def test_pii_tag_propagation(graph):
    inherited = graph.inherited_tags()
    carrying = {str(c) for c, tags in inherited.items() if "pii" in tags}
    assert carrying == {
        "stg_customers.name", "stg_customers.email", "dim_customer.name", "dim_customer.email",
        "customer_contact_list.email", "customer_contact_list.name_upper",
    }  # fmt: skip
    # lower(trim(email)) happens first, so the tag arrives as "derived" even where later steps copy it
    assert inherited[ColumnRef("customer_contact_list", "email")]["pii"] == DERIVED


def test_untagged_columns_do_not_get_tags(graph):
    inherited = graph.inherited_tags()
    assert ColumnRef("fct_orders", "net_amount") not in inherited
    assert ColumnRef("mart_country_revenue", "revenue") not in inherited


def test_mermaid_and_json_outputs(graph):
    mermaid = graph.to_mermaid()
    assert mermaid.startswith("flowchart LR")
    assert "raw_orders --> stg_orders" in mermaid and "fct_orders --> mart_country_revenue" in mermaid
    data = json.loads(json.dumps(graph.to_dict()))
    assert {"from": "stg_orders.amount", "to": "fct_orders.amount", "kind": "direct"} in data["column_edges"]
    assert data["inherited_tags"]["dim_customer.email"] == {"pii": "derived"}


def test_cte_computed_column_is_not_a_copy(tmp_path):
    (tmp_path / "m").mkdir()
    src = sources(tmp_path, "sources:\n  t:\n    columns:\n      a: int\n")
    m = models(tmp_path / "m", x="with c as (select a * 2 as b from t) select b from c")
    graph = build_graph(m, src)
    (edge,) = graph.column_edges
    assert (str(edge.source), str(edge.target), edge.kind) == ("t.a", "x.b", DERIVED)


def test_pure_passthrough_keeps_a_tag_as_direct(tmp_path):
    (tmp_path / "m").mkdir()
    src = sources(tmp_path, "sources:\n  t:\n    columns:\n      email: {type: text, tags: [pii]}\n")
    m = models(tmp_path / "m", x="select email from t", y="select email as contact from x")
    graph = build_graph(m, src)
    assert graph.inherited_tags()[ColumnRef("y", "contact")] == {"pii": DIRECT}


def test_circular_models_are_reported(tmp_path):
    (tmp_path / "m").mkdir()
    src = sources(tmp_path, "sources:\n  t:\n    columns:\n      a: int\n")
    with pytest.raises(LineageError, match="circular dependency"):
        build_graph(models(tmp_path / "m", a="select a from b", b="select a from a"), src)


def test_undeclared_source_is_a_warning_not_a_crash(tmp_path):
    (tmp_path / "m").mkdir()
    graph = build_graph(models(tmp_path / "m", x="select 1 as one from mystery"))
    assert any("mystery" in w for w in graph.warnings)


def test_ambiguous_column_without_schema_is_an_error(tmp_path):
    (tmp_path / "m").mkdir()
    with pytest.raises(LineageError, match="cannot resolve columns"):
        build_graph(models(tmp_path / "m", x="select id from a join b on a.k = b.k"))


def test_unparseable_sql_and_empty_folder_and_duplicates(tmp_path):
    (tmp_path / "m").mkdir()
    with pytest.raises(LineageError, match="no .sql files"):
        build_graph(tmp_path / "m")
    with pytest.raises(LineageError, match="cannot parse"):
        build_graph(models(tmp_path / "m", x="select from where"))
    (tmp_path / "m" / "sub").mkdir()
    (tmp_path / "m" / "sub" / "x.sql").write_text("select 1 as a")
    with pytest.raises(LineageError, match="duplicate model name"):
        build_graph(tmp_path / "m")


def test_columnref_parse():
    assert ColumnRef.parse("a.b") == ColumnRef("a", "b")
    with pytest.raises(LineageError):
        ColumnRef.parse("nodot")
