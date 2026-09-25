# dq-lineage-toolkit

[![CI](https://github.com/DruvaSaiKumar/dq-lineage-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/DruvaSaiKumar/dq-lineage-toolkit/actions/workflows/ci.yml)

`dqkit` is a small toolkit for two governance questions data teams keep answering by hand:

1. **Is the data any good?** Declarative YAML checks (nulls, duplicates, ranges, freshness, referential integrity, schema contracts, volume drift, custom SQL) that run on DuckDB, with severities, tolerances, sample violations and a run history.
2. **What depends on what?** Column-level lineage parsed from dbt-style SQL models, with impact analysis ("what breaks if I change this column?"), tag propagation (where does `pii` end up?) and a coverage report (which columns have no quality check at all?).

This is a personal portfolio project on synthetic data. It reflects patterns from my data engineering work (validation, reconciliation, audit logging, lineage, governance) but contains no employer or client code or data.

## Try it

```bash
pip install -e .
dqkit demo
```

`demo` generates a synthetic warehouse, runs the example models in dependency order, injects a known number of defects, runs every quality check, and writes a report, a Mermaid lineage diagram, a JSON lineage graph and a coverage report to `demo_output/`. It exits `1` because the checks find the defects. `dqkit demo --clean` exits `0`.

## Data-quality checks

```yaml
dataset: stg_orders
table: stg_orders
checks:
  - { type: not_null, column: order_id }
  - { type: unique, columns: [order_id] }
  - { type: not_null, column: customer_id, severity: warn, max_failure_pct: 1 }
  - { type: accepted_values, column: status, values: [PENDING, PAID, SHIPPED, DELIVERED, CANCELLED] }
  - { type: range, column: amount, min: 0, max: 100000 }
  - { type: relationships, column: customer_id, to_table: stg_customers, to_column: customer_id }
  - { type: freshness, column: order_ts, max_age_hours: 48 }
  - { type: row_count_change, max_pct: 25 }      # compared with the previous recorded run
  - type: custom_sql
    name: discount_not_above_amount
    sql: "SELECT order_id FROM stg_orders WHERE amount >= 0 AND discount > amount"
```

| Check | Fails when |
|---|---|
| `not_null`, `accepted_values`, `range`, `regex` | a non-conforming row exists (NULLs are only a violation for `not_null`) |
| `unique` | surplus rows share a key (composite keys supported) |
| `relationships` | a non-NULL value has no match in the parent table |
| `freshness` | the newest value is older than `max_age_hours` at `--now` |
| `row_count` / `row_count_change` | count is out of bounds / moved more than `max_pct` since the last run |
| `schema_contract` | a column is missing, has the wrong type family, or (unless `allow_extra`) is unexpected |
| `custom_sql` | the query returns rows |

Every check accepts `severity: error|warn` (warnings are reported but do not fail the run), `max_failure_pct` (a tolerated share of failing rows, reported rather than hidden) and returns up to `sample_size` violating rows.

A check that cannot run (missing column, bad SQL) reports `ERROR` and the remaining checks still run. Exit codes: `0` clean, `1` failed checks, `2` could not run.

### What the demo finds

The demo injects a known number of defects; `tests/test_demo_cli.py` asserts every count exactly.

| Injected defect | Injected | Reported by |
|---|---|---|
| Duplicate order rows | 5 | `unique:order_id` on `stg_orders` **and** `fct_orders` |
| Status outside the allowed list | 4 | `accepted_values:status` |
| Negative amounts | 8 | `range:amount` |
| Orders for unknown customers | 6 | `relationships:customer_id` |
| Orders with no customer | 12 | tolerated by `max_failure_pct: 1` (reported, passes); with the orphans above, 18 rows have a NULL country in `fct_orders` (warning) |
| Discount larger than amount | 10 | custom SQL on `stg_orders`, and 10 negative `net_amount` rows in `fct_orders` |
| Invalid country / email | 3 / 4 | `accepted_values`, `regex` on `stg_customers` |
| Feed stopped two days early | n/a | `freshness:order_ts` |

Padded, mixed-case text was also injected into *valid* rows to show that staging absorbs it and the checks do not report it.

## Lineage, impact and tags

```bash
dqkit lineage examples/models --sources examples/sources.yml --mermaid lineage.md --json lineage.json
dqkit impact  examples/models raw_orders.amount --sources examples/sources.yml
dqkit tags    examples/models --sources examples/sources.yml
```

Real output of `impact`:

```
Changing raw_orders.amount can affect 4 column(s) in 3 model(s):

| Depth | Column | Relationship |
|---|---|---|
| 1 | stg_orders.amount | copy |
| 2 | fct_orders.amount | copy |
| 2 | fct_orders.net_amount | computed from it |
| 3 | mart_country_revenue.revenue | computed from it |
```

```mermaid
flowchart LR
    raw_orders[(raw_orders)] --> stg_orders
    raw_customers[(raw_customers)] --> stg_customers
    stg_customers --> dim_customer
    dim_customer --> customer_contact_list
    dim_customer --> fct_orders
    stg_orders --> fct_orders
    fct_orders --> mart_country_revenue
```

- **Column-level, through CTEs, joins and `SELECT *`.** sqlglot does the parsing; each model column is traced to the upstream columns it is built from.
- **Copy vs computed.** An edge is a `copy` only if every step on the way is a plain column reference. `WITH x AS (SELECT a * 2 AS b ...) SELECT b FROM x` is correctly `computed`, not a copy (tested).
- **Tags.** Declare `tags: [pii]` on source columns; `dqkit tags` lists every downstream column that carries it and whether it arrives verbatim or transformed. A transformation is never assumed to anonymise a value, so `lower(trim(email))` still counts.
- **dbt-style models.** `ref()`, `source()` and `config()` are rendered; any other Jinja is refused with a clear error rather than guessed at.

### Coverage

`dqkit coverage` matches quality configs to models and lists columns nobody checks:

```
Column coverage by data-quality checks: 51.7%

| Model | Coverage | Columns without a check |
|---|---|---|
| stg_customers | 100.0% (5/5) | none |
| stg_orders | 83.3% (5/6) | discount |
| dim_customer | 0.0% (0/5) | no checks defined |
| customer_contact_list | 0.0% (0/3) | no checks defined |
| fct_orders | 42.9% (3/7) | customer_id, amount, discount, order_ts |
| mart_country_revenue | 66.7% (2/3) | orders |
```

`--fail-under N` makes it a CI gate. Note that `custom_sql` checks are not attributed to any column, which is why `discount` shows as unchecked even though a custom check reads it.

## Commands

| Command | Purpose |
|---|---|
| `dqkit check CONFIG... [--database F] [--history H] [--now T] [--report R] [--json J]` | run checks (wildcards are expanded by the tool, so it works on Windows shells) |
| `dqkit lineage MODELS [--sources S] [--mermaid F] [--json F]` | build the graph, print/write a diagram |
| `dqkit impact MODELS table.column` | everything built from a column |
| `dqkit tags MODELS --sources S` | where tagged columns end up |
| `dqkit coverage MODELS CONFIG... [--fail-under N]` | columns without a quality check |
| `dqkit demo [--clean]` | synthetic warehouse, defects, checks and reports |

## Limitations

- **Lineage covers values, not filters.** A column used only in a `WHERE` or `JOIN` (like `status` in `fct_orders`) decides which rows survive but is not a lineage edge. This is stated and tested.
- **Column lineage needs schemas** for source tables (`sources.yml`). Ambiguous columns without one are reported as errors; undeclared tables produce a warning.
- **DuckDB only** for check execution. The SQL is standard, but other engines would need an adapter.
- `custom_sql` runs whatever SQL is in the config. Treat config files as trusted code. Table names are validated against a strict pattern and identifiers/values are quoted, but that is not a sandbox.
- Freshness needs a timestamp column; the age is computed in Python against `--now`, not in SQL.

## Development

```bash
pip install -e .[dev]
pytest          # 59 tests
ruff check . && ruff format --check .
```

Design notes: [docs/design-decisions.md](docs/design-decisions.md).

## License

MIT
