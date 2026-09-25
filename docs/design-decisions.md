# Design decisions

## Why quality checks and lineage live in one tool

Each half is more useful with the other. Lineage tells you which quality checks matter after a
change (`impact`), and the coverage report joins the two: the model graph says which columns exist,
the check configs say which are guarded. The demo makes this concrete by building the tables from
the same SQL the lineage was parsed from, so a defect injected into a raw table shows up as a failed
check several models downstream, and `impact` says which columns it travelled through.

## A check counts violations; it does not just say pass/fail

Every row-level check returns *how many* rows violate it and *out of how many*. That makes the two
knobs people actually need cheap to offer: `max_failure_pct` (tolerate 1% of orders without a
customer, but say so) and `severity: warn`. A tolerated failure still appears in the report, marked
as within tolerance, so it does not silently disappear.

## Checks that cannot run are ERROR, not FAIL

A typo in a column name is a problem with the check, not evidence about the data. It reports `ERROR`
(exit code 2, distinct from 1), and the other checks in the dataset still run. Otherwise one bad
line in a YAML file hides every real finding.

## Freshness is measured against an explicit `now`

`--now` defaults to the current time, but every test and the demo pass a fixed value. That keeps the
demo reproducible and lets a job check "was the data fresh as of the batch time", not "as of when
this script happened to start".

## Volume drift needs history, so runs are recorded

`row_count_change` compares with the previous run of the same dataset. The `check` command appends
each run's metrics to a JSONL file (`--history`). The first run has no baseline and passes with a
note saying so, rather than failing or pretending to have compared something.

## Lineage from SQL, one level at a time

For each model, sqlglot traces every output column to the columns of the tables it directly reads
(CTEs and subqueries are looked through). Edges between models are chained by the graph. Tracing
each step separately, instead of asking sqlglot to expand all the way to raw sources, is what makes
impact analysis on *intermediate* models possible.

### Copy versus computed

An edge is `copy` only if every select item on the path from output to source is a bare column
reference. Checking only the outermost select item would classify
`WITH x AS (SELECT a * 2 AS b FROM t) SELECT b FROM x` as a copy. A test covers this case.

### Tag propagation does not assume anything is anonymised

`lower(trim(email))` is still an email. Tags follow every edge, and the report says whether the tagged
value arrives verbatim or transformed, leaving the judgement about whether a transformation
anonymises it to a person. Declaring a hash function "safe" would need a policy the tool does not
have.

### What lineage leaves out

Filters and join keys shape which rows exist but do not produce output values, so they are not edges.
This is the usual definition of column lineage and is asserted in a test, but it means "does anything
depend on `status`?" gets the answer no for `fct_orders` even though it filters on it.

## Jinja: render three things, refuse the rest

Real dbt projects use much more Jinja than `ref()`, `source()` and `config()`. Silently ignoring an
unknown macro would produce wrong lineage, so anything else raises an error naming the model. That is
less convenient than best-effort parsing and more honest.

## What is not verified

- Only DuckDB executes checks. Nothing here has been run against BigQuery, Snowflake or Databricks.
- Lineage has been tested on the bundled models and on small purpose-built cases, not on a large
  real-world dbt project.
