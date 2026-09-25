# Design notes

**Checks and lineage in one tool.** Each is more useful with the other. Lineage says which checks matter after a change, and the coverage report joins the two: the model graph says which columns exist, the check configs say which are guarded. The demo builds its tables from the same SQL the lineage is parsed from, so a defect injected into a raw table fails a check several models downstream, and `impact` shows the columns it travelled through.

**Checks count violations.** Each row-level check returns how many rows break it and out of how many. That makes `max_failure_pct` and `severity: warn` cheap to offer. A tolerated failure still shows up in the report, marked as within tolerance, so it doesn't quietly disappear.

**A check that can't run is `ERROR`, not `FAIL`.** A typo in a column name is a problem with the check, not evidence about the data. It gets exit code 2 (failed checks are 1) and the other checks still run, so one bad line in a YAML file doesn't hide real findings.

**Freshness uses an explicit `--now`.** It defaults to the current time, but tests and the demo pass a fixed value. That keeps the demo reproducible and lets a job ask whether the data was fresh as of the batch time, not as of when the script started.

**Volume drift needs history.** `row_count_change` compares against the previous run of the same dataset, so `check` appends each run's metrics to a JSONL file (`--history`). The first run has nothing to compare with, so it passes with a note saying so.

**Lineage one level at a time.** For each model, sqlglot traces every output column to the columns of the tables it reads directly, looking through CTEs and subqueries. The graph chains those edges together. Tracing each step separately, rather than expanding all the way to raw sources, is what makes impact analysis on intermediate models possible.

**Copy versus computed.** An edge is a copy only if every select item on the path is a bare column reference. Checking only the outermost item would call `WITH x AS (SELECT a * 2 AS b FROM t) SELECT b FROM x` a copy. A test covers this.

**Tags don't assume anonymization.** `lower(trim(email))` is still an email. Tags follow every edge, and the report says whether the value arrives as is or transformed. Deciding that a hash counts as anonymized would need a policy the tool doesn't have.

**Filters and join keys aren't lineage.** They decide which rows exist but don't produce output values, which is the usual definition of column lineage. The catch is that "does anything depend on `status`?" gets "no" for `fct_orders` even though it filters on it. A test asserts this.

**Limited Jinja.** Real dbt projects use much more than `ref()`, `source()` and `config()`. Silently ignoring an unknown macro would give wrong lineage, so anything else raises an error naming the model. That's less convenient than best-effort parsing, but it doesn't guess.
