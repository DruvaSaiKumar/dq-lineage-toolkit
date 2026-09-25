"""Synthetic demo warehouse for the example models, with a known set of injected defects.

Nothing here comes from any real company or dataset. The raw tables are generated with a seeded
RNG, the example models are then executed in dependency order, and the quality checks run on the
resulting tables, so a defect in raw data is caught at the layer where it becomes visible.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from .lineage import LineageGraph

N_CUSTOMERS, N_ORDERS = 200, 2000
STATUSES = ["PAID", "SHIPPED", "DELIVERED", "PENDING", "CANCELLED"]
COUNTRIES = ["US", "GB", "IN", "DE"]

# Injected into the raw tables (disjoint row sets, so each count is exact).
DEFECTS = {
    "null_customer": 12,
    "orphan_customer": 6,
    "negative_amount": 8,
    "discount_above_amount": 10,
    "bad_status": 4,
    "duplicate_orders": 5,
    "bad_country": 3,
    "bad_email": 4,
}
DAY0 = datetime(2024, 6, 1)
NOW = datetime(2024, 6, 7)  # the demo's "current time", fixed so freshness is reproducible


def generate(con, clean: bool = False, seed: int = 3) -> dict[str, int]:
    """Create raw_customers and raw_orders in `con`. With clean=True nothing is wrong and the
    newest order is from the last day; otherwise the feed also stopped two days early."""
    rng = random.Random(seed)
    last_day = 5 if clean else 3  # freshness defect: the feed stopped after June 3

    customers = [
        [
            i,
            f"Customer {i}",
            f"customer{i}@example.com",
            rng.choice(COUNTRIES),
            DAY0 - timedelta(days=rng.randint(30, 900)),
        ]
        for i in range(1, N_CUSTOMERS + 1)
    ]
    for row in customers[:10]:  # messy but valid: staging trims and lower-cases these
        row[1], row[2] = f"  {row[1]} ", row[2].upper()

    orders = []
    for i in range(1, N_ORDERS + 1):
        amount = round(rng.uniform(5, 500), 2)
        orders.append(
            [
                i,
                rng.randint(1, N_CUSTOMERS),
                amount,
                round(rng.uniform(0, min(50, amount * 0.3)), 2) if rng.random() < 0.6 else None,
                rng.choice(STATUSES),
                DAY0 + timedelta(days=rng.randrange(last_day), seconds=rng.randrange(86_400)),
            ]
        )
    for row in orders[10:40]:  # messy but valid status casing/whitespace
        row[4] = f" {row[4].lower()} "

    injected = {k: 0 for k in DEFECTS}
    if not clean:
        pool = [r for r in orders if r[4].strip().upper() != "CANCELLED" and r[3] is not None]
        picks = rng.sample(pool, 12 + 6 + 8 + 10 + 4 + 5)
        it = iter(picks)
        for _ in range(DEFECTS["null_customer"]):
            next(it)[1] = None
        for n in range(DEFECTS["orphan_customer"]):
            next(it)[1] = 900 + n
        for _ in range(DEFECTS["negative_amount"]):
            row = next(it)
            row[2] = -abs(row[2])
        for _ in range(DEFECTS["discount_above_amount"]):
            row = next(it)
            row[3] = row[2] + 5  # discount larger than the amount
        for _ in range(DEFECTS["bad_status"]):
            next(it)[4] = "REFUNDED"
        orders += [list(next(it)) for _ in range(DEFECTS["duplicate_orders"])]
        for row in rng.sample(customers[10:], 3):
            row[3] = "XX"
        for row in rng.sample([c for c in customers[10:] if c[3] != "XX"], 4):
            row[2] = "not-an-email"
        injected = dict(DEFECTS)

    con.execute("DROP TABLE IF EXISTS raw_customers")
    con.execute("DROP TABLE IF EXISTS raw_orders")
    con.execute(
        "CREATE TABLE raw_customers (customer_id INTEGER, name VARCHAR, email VARCHAR, "
        "country VARCHAR, signup_ts TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE raw_orders (order_id INTEGER, customer_id INTEGER, amount DOUBLE, "
        "discount DOUBLE, status VARCHAR, order_ts TIMESTAMP)"
    )
    _bulk_insert(con, "raw_customers", customers)
    _bulk_insert(con, "raw_orders", orders)
    return injected


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _bulk_insert(con, table: str, rows: list[list], chunk: int = 500) -> None:
    """Multi-row INSERTs with inlined literals. The values are generated here, never user input;
    parameter binding and executemany were both far slower (seconds for ~2,000 rows)."""
    for i in range(0, len(rows), chunk):
        values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows[i : i + chunk])
        con.execute(f"INSERT INTO {table} VALUES {values}")


def build_models(con, graph: LineageGraph) -> None:
    """Materialise every model as a table, upstream first."""
    for name in graph.order:
        con.execute(f"DROP TABLE IF EXISTS {name}")
        con.execute(f"CREATE TABLE {name} AS {graph.models[name]}")


def build_warehouse(db_path: str | Path, graph: LineageGraph, clean: bool = False) -> dict[str, int]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        injected = generate(con, clean=clean)
        build_models(con, graph)
    finally:
        con.close()
    return injected
