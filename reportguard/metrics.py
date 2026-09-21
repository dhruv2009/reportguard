"""Metric definitions and check_metric.

check_metric recomputes a metric from its SQL, scales the reported value by its unit
label ($, $K, $M, %) and compares within tolerance.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

from . import config

_COMPLETED_IN_PERIOD = "o.status = 'completed' AND o.order_ts_utc >= :start AND o.order_ts_utc < :end"


@dataclass(frozen=True)
class MetricDef:
    id: str
    name: str
    unit: str                 # usd | count | percent
    description: str
    sql: str
    abs_tolerance: float
    dimension: str | None = None
    version: str = "2026.1"
    dimension_values_sql: str | None = None       # where valid dimension values come from
    dimension_aliases: tuple = ()                  # (("emergency", "ed"),): display label -> stored value


RETAIL_METRICS: dict[str, MetricDef] = {m.id: m for m in [
    MetricDef(
        "GROSS_REVENUE", "Gross revenue", "usd",
        "Sum of quantity x unit_price for COMPLETED orders placed in the period. Period boundaries are UTC.",
        f"SELECT ROUND(COALESCE(SUM(oi.quantity * oi.unit_price), 0), 2) FROM orders o "
        f"JOIN order_items oi ON oi.order_id = o.order_id WHERE {_COMPLETED_IN_PERIOD}",
        abs_tolerance=1.0),
    MetricDef(
        "REFUNDS", "Refunds issued", "usd",
        "Sum of refund amounts ISSUED in the period (by refund_ts_utc, UTC), regardless of order date.",
        "SELECT ROUND(COALESCE(SUM(r.amount), 0), 2) FROM refunds r "
        "WHERE r.refund_ts_utc >= :start AND r.refund_ts_utc < :end",
        abs_tolerance=1.0),
    MetricDef(
        "NET_REVENUE", "Net revenue", "usd",
        "GROSS_REVENUE minus REFUNDS for the same period.",
        f"SELECT ROUND((SELECT COALESCE(SUM(oi.quantity * oi.unit_price), 0) FROM orders o "
        f"JOIN order_items oi ON oi.order_id = o.order_id WHERE {_COMPLETED_IN_PERIOD}) - "
        f"(SELECT COALESCE(SUM(r.amount), 0) FROM refunds r "
        f"WHERE r.refund_ts_utc >= :start AND r.refund_ts_utc < :end), 2)",
        abs_tolerance=1.0),
    MetricDef(
        "ORDERS", "Completed orders", "count",
        "Number of distinct COMPLETED orders placed in the period (UTC). One row per order, not per item.",
        f"SELECT COUNT(*) FROM orders o WHERE {_COMPLETED_IN_PERIOD}",
        abs_tolerance=0),
    MetricDef(
        "AOV", "Average order value", "usd",
        "GROSS_REVENUE divided by ORDERS for the same period.",
        f"SELECT ROUND(SUM(oi.quantity * oi.unit_price) / COUNT(DISTINCT o.order_id), 2) FROM orders o "
        f"JOIN order_items oi ON oi.order_id = o.order_id WHERE {_COMPLETED_IN_PERIOD}",
        abs_tolerance=0.01),
    MetricDef(
        "ACTIVE_CUSTOMERS", "Active customers", "count",
        "Distinct customers with at least one COMPLETED order placed in the period (UTC).",
        f"SELECT COUNT(DISTINCT o.customer_id) FROM orders o WHERE {_COMPLETED_IN_PERIOD}",
        abs_tolerance=0),
    MetricDef(
        "NEW_CUSTOMERS", "New customers", "count",
        "Customers whose signup_ts_utc falls in the period (UTC).",
        "SELECT COUNT(*) FROM customers c WHERE c.signup_ts_utc >= :start AND c.signup_ts_utc < :end",
        abs_tolerance=0),
    MetricDef(
        "REFUND_RATE", "Refund rate", "percent",
        "100 x REFUNDS / GROSS_REVENUE for the same period, in percent.",
        f"SELECT ROUND(100.0 * (SELECT COALESCE(SUM(r.amount), 0) FROM refunds r "
        f"WHERE r.refund_ts_utc >= :start AND r.refund_ts_utc < :end) / "
        f"(SELECT SUM(oi.quantity * oi.unit_price) FROM orders o "
        f"JOIN order_items oi ON oi.order_id = o.order_id WHERE {_COMPLETED_IN_PERIOD}), 2)",
        abs_tolerance=0.05),
    MetricDef(
        "CATEGORY_REVENUE", "Gross revenue by product category", "usd",
        "GROSS_REVENUE restricted to one product category (products.category = :dimension).",
        f"SELECT ROUND(COALESCE(SUM(oi.quantity * oi.unit_price), 0), 2) FROM orders o "
        f"JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id "
        f"WHERE {_COMPLETED_IN_PERIOD} AND p.category = :dimension",
        abs_tolerance=1.0, dimension="category",
        dimension_values_sql="SELECT DISTINCT category FROM products"),
]}

METRICS: dict[str, MetricDef] = RETAIL_METRICS
if config.DOMAIN == "health":
    from .health.metrics import METRICS as METRICS  # noqa: F811  (domain switch, RG_DOMAIN=health)

UNIT_SCALES = {
    "usd": {"": 1, "$": 1, "usd": 1, "$k": 1e3, "k": 1e3, "usd k": 1e3, "thousands": 1e3,
            "$m": 1e6, "m": 1e6, "millions": 1e6},
    "count": {"": 1, "#": 1, "count": 1, "k": 1e3, "thousands": 1e3, "m": 1e6},
    "percent": {"%": 1, "percent": 1, "pct": 1, "": 1, "ratio": 100},
    "rate": {"": 1, "per 1000": 1, "per 1,000": 1, "/1000": 1, "days": 1, "pmpm": 1},
}


def period_bounds(period: str) -> tuple[str, str]:
    """'2026-08' -> ('2026-08-01 00:00:00', '2026-09-01 00:00:00'), UTC."""
    try:
        year, month = (int(x) for x in period.split("-"))
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"period must look like YYYY-MM, got {period!r}") from exc
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01 00:00:00", f"{nxt[0]:04d}-{nxt[1]:02d}-01 00:00:00"


def resolve_dimension(conn: sqlite3.Connection, m: MetricDef, value: str) -> str:
    """Match a displayed dimension label to a stored value, ignoring case. An unknown value is an
    error, not a silent zero."""
    if not m.dimension_values_sql:
        return value
    stored = [r[0] for r in conn.execute(m.dimension_values_sql).fetchall()]
    wanted = value.strip().lower()
    wanted = dict(m.dimension_aliases).get(wanted, wanted)
    for v in stored:
        if str(v).lower() == wanted:
            return v
    raise ValueError(f"Unknown {m.dimension} {value!r} for {m.id}. Valid values: {sorted(stored)}")


def compute_metric(conn: sqlite3.Connection, metric_id: str, period: str, dimension_value: str | None = None) -> float:
    m = METRICS.get(metric_id)
    if m is None:
        raise ValueError(f"Unknown metric_id {metric_id!r}. Known: {sorted(METRICS)}")
    if m.dimension and not dimension_value:
        raise ValueError(f"{metric_id} needs dimension_value (a {m.dimension})")
    if m.dimension:
        dimension_value = resolve_dimension(conn, m, dimension_value)
    start, end = period_bounds(period)
    params = {"start": start, "end": end, "dimension": dimension_value, "month": period}
    value = conn.execute(m.sql, params).fetchone()[0]
    return float(value or 0)


def unit_scale(metric: MetricDef, unit_label: str) -> float:
    key = (unit_label or "").strip().lower().replace("(", "").replace(")", "").replace("in ", "")
    scales = UNIT_SCALES[metric.unit]
    if key not in scales:
        raise ValueError(f"Unit label {unit_label!r} not understood for a {metric.unit} metric. "
                         f"Use one of: {sorted(k for k in scales if k)}")
    return scales[key]


def check_metric(conn: sqlite3.Connection, metric_id: str, reported_value: float, unit_label: str,
                 period: str, dimension_value: str | None = None, display_decimals: int = 0) -> dict:
    m = METRICS[metric_id] if metric_id in METRICS else None
    if m is None:
        raise ValueError(f"Unknown metric_id {metric_id!r}. Known: {sorted(METRICS)}")
    scale = unit_scale(m, unit_label)
    expected = compute_metric(conn, metric_id, period, dimension_value)
    reported = float(reported_value) * scale
    rounding_tol = 0.5 * (10 ** -int(display_decimals)) * scale
    tolerance = max(m.abs_tolerance, rounding_tol) + 1e-9
    delta = reported - expected
    return {
        "status": "PASS" if abs(delta) <= tolerance else "FAIL",
        "metric_id": metric_id,
        "period": period,
        "dimension_value": dimension_value,
        "reported_normalized": round(reported, 4),
        "expected": round(expected, 4),
        "delta": round(delta, 4),
        "delta_pct": round(100 * delta / expected, 2) if expected else None,
        "ratio_reported_to_expected": round(reported / expected, 4) if expected else None,
        "tolerance": round(tolerance, 4),
        "unit": m.unit,
        "unit_scale_applied": scale,
        "definition_version": m.version,
    }


def metric_catalog() -> list[dict]:
    return [{"metric_id": m.id, "name": m.name, "unit": m.unit, "dimension": m.dimension,
             "description": m.description} for m in METRICS.values()]


def metric_definition(metric_id: str) -> dict:
    if metric_id not in METRICS:
        raise ValueError(f"Unknown metric_id {metric_id!r}. Known: {sorted(METRICS)}")
    return asdict(METRICS[metric_id])
