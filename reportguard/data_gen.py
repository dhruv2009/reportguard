"""Builds the synthetic e-commerce warehouse (SQLite).

Seeded, so every run produces the same numbers. Orders skew towards US evening hours
(early morning UTC) and there's a promo spike at the start of Sep 1 UTC, which makes
the local-time vs UTC month boundary bug show up in the totals.
"""

from __future__ import annotations

import bisect
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    country       TEXT NOT NULL,
    signup_ts_utc TEXT NOT NULL            -- 'YYYY-MM-DD HH:MM:SS', UTC
);
CREATE TABLE products (
    product_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    unit_price  REAL NOT NULL
);
CREATE TABLE orders (
    order_id     INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
    order_ts_utc TEXT NOT NULL,            -- UTC
    status       TEXT NOT NULL CHECK (status IN ('completed', 'cancelled', 'pending')),
    channel      TEXT NOT NULL
);
CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL            -- price at time of sale
);
CREATE TABLE refunds (
    refund_id     INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    refund_ts_utc TEXT NOT NULL,           -- UTC; refunds count in the month they are issued
    amount        REAL NOT NULL
);
CREATE INDEX idx_orders_ts ON orders(order_ts_utc);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_refunds_ts ON refunds(refund_ts_utc);
CREATE INDEX idx_customers_signup ON customers(signup_ts_utc);
"""

CATEGORIES = {
    "Electronics": (60, 900),
    "Home": (15, 250),
    "Apparel": (12, 140),
    "Beauty": (8, 80),
    "Sports": (20, 320),
}
FIRST = ["Ava", "Liam", "Noah", "Mia", "Zara", "Kai", "Ivy", "Leo", "Nia", "Omar", "Ruby", "Sam", "Tara", "Yusuf"]
LAST = ["Patel", "Kim", "Garcia", "Chen", "Singh", "Brown", "Lopez", "Ali", "Novak", "Silva", "Ito", "Khan"]
COUNTRIES = ["US"] * 8 + ["CA", "GB"]
CHANNELS = ["web", "web", "app", "app", "marketplace"]

DATA_START = datetime(2026, 6, 1)
DATA_END = datetime(2026, 9, 10, 23, 59, 59)
FMT = "%Y-%m-%d %H:%M:%S"
# UTC hour weights: heavier 22:00-04:00 UTC (US evening)
HOUR_WEIGHTS = [9, 9, 8, 7, 4, 2, 1, 1, 1, 2, 3, 4, 5, 5, 6, 6, 6, 6, 6, 7, 7, 8, 9, 9]


def _ts(dt: datetime) -> str:
    return dt.strftime(FMT)


def build_warehouse(db_path: str | Path, seed: int = 7) -> dict:
    rng = random.Random(seed)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    # products
    products = []
    pid = 1
    for category, (lo, hi) in CATEGORIES.items():
        for i in range(8):
            price = round(rng.uniform(lo, hi), 2)
            products.append((pid, f"{category} item {i + 1}", category, price))
            pid += 1
    conn.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

    # customers: long-tenured base plus steady new signups
    customers = []
    signup_start = datetime(2025, 1, 1)
    for cid in range(1, 2201):
        if cid <= 1400:
            signup = signup_start + timedelta(seconds=rng.uniform(0, (DATA_START - signup_start).total_seconds()))
        else:
            signup = DATA_START + timedelta(seconds=rng.uniform(0, (DATA_END - DATA_START).total_seconds()))
        first, last = rng.choice(FIRST), rng.choice(LAST)
        customers.append((cid, f"{first} {last}", f"{first.lower()}.{last.lower()}{cid}@example.com",
                          rng.choice(COUNTRIES), signup))
    customers.sort(key=lambda c: c[4])
    customers = [(i + 1, n, e, c, s) for i, (_, n, e, c, s) in enumerate(customers)]
    signup_times = [c[4] for c in customers]
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", [(*c[:4], _ts(c[4])) for c in customers])

    # order timestamps: ~62/day with weekly seasonality, plus a promo burst
    order_times = []
    day = DATA_START
    while day <= DATA_END:
        n = int(rng.gauss(62, 8) * (1.15 if day.weekday() >= 5 else 1.0))
        for _ in range(max(n, 20)):
            hour = rng.choices(range(24), HOUR_WEIGHTS)[0]
            order_times.append(day + timedelta(hours=hour, seconds=rng.randint(0, 3599)))
        day += timedelta(days=1)
    promo = datetime(2026, 9, 1)
    order_times += [promo + timedelta(seconds=rng.randint(0, 4 * 3600 - 1)) for _ in range(90)]
    order_times.sort()

    orders, items, refunds = [], [], []
    item_id = refund_id = 1
    price_by_pid = {p[0]: p[3] for p in products}
    for oid, ts in enumerate(order_times, start=1):
        eligible = bisect.bisect_right(signup_times, ts)
        if eligible == 0:
            continue
        cust = customers[rng.randrange(eligible)][0]
        age_days = (DATA_END - ts).days
        if age_days < 5:
            status = rng.choices(["completed", "pending", "cancelled"], [60, 32, 8])[0]
        else:
            status = rng.choices(["completed", "cancelled"], [91, 9])[0]
        orders.append((oid, cust, _ts(ts), status, rng.choice(CHANNELS)))
        total = 0.0
        for _ in range(rng.choices([1, 2, 3, 4], [50, 28, 15, 7])[0]):
            p = rng.choice(products)[0]
            qty = rng.choices([1, 2, 3], [80, 15, 5])[0]
            items.append((item_id, oid, p, qty, price_by_pid[p]))
            total += qty * price_by_pid[p]
            item_id += 1
        if status == "completed" and rng.random() < 0.07:
            rts = ts + timedelta(days=rng.randint(2, 25), seconds=rng.randint(0, 86399))
            if rts <= DATA_END:
                amount = round(total if rng.random() < 0.6 else total * rng.uniform(0.2, 0.6), 2)
                refunds.append((refund_id, oid, _ts(rts), amount))
                refund_id += 1

    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
    conn.executemany("INSERT INTO refunds VALUES (?,?,?,?)", refunds)
    conn.commit()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ["customers", "products", "orders", "order_items", "refunds"]}
    conn.close()
    return counts
