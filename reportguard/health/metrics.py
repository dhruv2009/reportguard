"""Governed metric definitions for the population health warehouse.

Same contract as the retail metrics: one reviewed SQL statement per metric, a unit,
and a tolerance. Periods are calendar months in UTC.
"""

from __future__ import annotations

from ..metrics import MetricDef

_MEMBERS = "(SELECT COUNT(*) FROM member_months WHERE month = :month)"
_COST = ("(SELECT COALESCE(SUM(c.amount), 0) FROM claims c "
         "WHERE c.service_ts_utc >= :start AND c.service_ts_utc < :end)")
_ED = ("(SELECT COUNT(*) FROM encounters e WHERE e.encounter_type = 'ed' "
       "AND e.admit_ts_utc >= :start AND e.admit_ts_utc < :end)")
_ADMITS = ("(SELECT COUNT(*) FROM encounters e WHERE e.encounter_type = 'inpatient' "
           "AND e.admit_ts_utc >= :start AND e.admit_ts_utc < :end)")
_INDEX_DISCHARGES = ("SELECT e.encounter_id, e.patient_id, e.discharge_ts_utc FROM encounters e "
                     "WHERE e.encounter_type = 'inpatient' AND e.discharge_ts_utc >= :start "
                     "AND e.discharge_ts_utc < :end AND e.disposition NOT IN ('expired', 'transfer')")

METRICS: dict[str, MetricDef] = {m.id: m for m in [
    MetricDef(
        "ATTRIBUTED_MEMBERS", "Attributed members", "count",
        "Members attributed to the contract in the reporting month (one row per member month).",
        f"SELECT {_MEMBERS}", abs_tolerance=0),
    MetricDef(
        "HIGH_RISK_MEMBERS", "High-risk members", "count",
        "Attributed members with a risk score of 2.0 or higher.",
        "SELECT COUNT(*) FROM member_months mm JOIN patients p ON p.patient_id = mm.patient_id "
        "WHERE mm.month = :month AND p.risk_score >= 2.0", abs_tolerance=0),
    MetricDef(
        "TOTAL_COST", "Total cost of care", "usd",
        "Sum of all claim amounts with a service date in the period (UTC).",
        f"SELECT ROUND({_COST}, 2)", abs_tolerance=1.0),
    MetricDef(
        "COST_PMPM", "Cost per member per month", "usd",
        "Total cost of care divided by attributed members for the same month.",
        f"SELECT ROUND(1.0 * {_COST} / {_MEMBERS}, 2)", abs_tolerance=0.01),
    MetricDef(
        "ED_VISITS", "ED visits", "count",
        "Emergency department encounters with an admit time in the period (UTC).",
        f"SELECT {_ED}", abs_tolerance=0),
    MetricDef(
        "ED_VISITS_PER_1000", "ED visits per 1,000 members", "rate",
        "ED visits divided by attributed members, annualized and expressed per 1,000 members "
        "(visits / members x 12,000).",
        f"SELECT ROUND(12000.0 * {_ED} / {_MEMBERS}, 1)", abs_tolerance=0.05),
    MetricDef(
        "INPATIENT_ADMITS", "Inpatient admissions", "count",
        "Inpatient encounters with an admit time in the period (UTC).",
        f"SELECT {_ADMITS}", abs_tolerance=0),
    MetricDef(
        "ADMITS_PER_1000", "Admissions per 1,000 members", "rate",
        "Inpatient admissions divided by attributed members, annualized per 1,000 members.",
        f"SELECT ROUND(12000.0 * {_ADMITS} / {_MEMBERS}, 1)", abs_tolerance=0.05),
    MetricDef(
        "AVG_LENGTH_OF_STAY", "Average length of stay", "rate",
        "Mean days between admit and discharge for inpatient stays discharged in the period.",
        "SELECT ROUND(AVG((julianday(e.discharge_ts_utc) - julianday(e.admit_ts_utc))), 2) FROM encounters e "
        "WHERE e.encounter_type = 'inpatient' AND e.discharge_ts_utc >= :start AND e.discharge_ts_utc < :end",
        abs_tolerance=0.01),
    MetricDef(
        "READMISSION_RATE", "30-day readmission rate", "percent",
        "Of inpatient discharges in the period (excluding deaths and transfers), the share followed by "
        "another inpatient admission within 30 days.",
        f"SELECT ROUND(100.0 * (SELECT COUNT(*) FROM ({_INDEX_DISCHARGES}) d WHERE EXISTS "
        f"(SELECT 1 FROM encounters r WHERE r.patient_id = d.patient_id AND r.encounter_type = 'inpatient' "
        f"AND r.admit_ts_utc > d.discharge_ts_utc "
        f"AND julianday(r.admit_ts_utc) - julianday(d.discharge_ts_utc) <= 30)) / "
        f"(SELECT COUNT(*) FROM ({_INDEX_DISCHARGES}) d2), 2)", abs_tolerance=0.05),
    MetricDef(
        "HBA1C_SCREENING_RATE", "HbA1c screening rate", "percent",
        "Of attributed members with diabetes, the share with at least one HbA1c result in the period. "
        "The numerator counts distinct members, not lab results.",
        "SELECT ROUND(100.0 * (SELECT COUNT(DISTINCT l.patient_id) FROM labs l JOIN member_months mm "
        "ON mm.patient_id = l.patient_id AND mm.month = :month JOIN patients p ON p.patient_id = l.patient_id "
        "WHERE p.has_diabetes = 1 AND l.code = 'HBA1C' AND l.taken_ts_utc >= :start AND l.taken_ts_utc < :end) / "
        "(SELECT COUNT(*) FROM member_months mm2 JOIN patients p2 ON p2.patient_id = mm2.patient_id "
        "WHERE mm2.month = :month AND p2.has_diabetes = 1), 2)", abs_tolerance=0.05),
    MetricDef(
        "BP_CONTROL_RATE", "Blood pressure control rate", "percent",
        "Of attributed members with hypertension and a systolic reading in the period, the share whose most "
        "recent reading in the period is under 140.",
        "SELECT ROUND(100.0 * SUM(CASE WHEN latest < 140 THEN 1 ELSE 0 END) / COUNT(*), 2) FROM ("
        "SELECT l.patient_id, (SELECT l2.value FROM labs l2 WHERE l2.patient_id = l.patient_id "
        "AND l2.code = 'BP_SYSTOLIC' AND l2.taken_ts_utc >= :start AND l2.taken_ts_utc < :end "
        "ORDER BY l2.taken_ts_utc DESC LIMIT 1) AS latest FROM labs l "
        "JOIN member_months mm ON mm.patient_id = l.patient_id AND mm.month = :month "
        "JOIN patients p ON p.patient_id = l.patient_id AND p.has_hypertension = 1 "
        "WHERE l.code = 'BP_SYSTOLIC' AND l.taken_ts_utc >= :start AND l.taken_ts_utc < :end "
        "GROUP BY l.patient_id)", abs_tolerance=0.05),
    MetricDef(
        "PCP_VISIT_RATE", "PCP visit rate", "percent",
        "Share of attributed members with at least one office encounter in the period.",
        "SELECT ROUND(100.0 * (SELECT COUNT(DISTINCT e.patient_id) FROM encounters e "
        "JOIN member_months mm ON mm.patient_id = e.patient_id AND mm.month = :month "
        "WHERE e.encounter_type = 'office' AND e.admit_ts_utc >= :start AND e.admit_ts_utc < :end) / "
        f"{_MEMBERS}, 2)", abs_tolerance=0.05),
    MetricDef(
        "OPEN_CARE_GAPS", "Open care gaps", "count",
        "Care gaps opened on or before the period end and still open at the period end "
        "(closed gaps are excluded).",
        "SELECT COUNT(*) FROM care_gaps g WHERE g.opened_ts_utc < :end "
        "AND (g.closed_ts_utc IS NULL OR g.closed_ts_utc >= :end)", abs_tolerance=0),
    MetricDef(
        "COST_BY_CATEGORY", "Cost of care by claim category", "usd",
        "Total cost of care restricted to one claim category (claims.category = :dimension).",
        "SELECT ROUND(COALESCE(SUM(c.amount), 0), 2) FROM claims c WHERE c.category = :dimension "
        "AND c.service_ts_utc >= :start AND c.service_ts_utc < :end",
        abs_tolerance=1.0, dimension="category",
        dimension_values_sql="SELECT DISTINCT category FROM claims",
        dimension_aliases=(("emergency", "ed"), ("emergency department", "ed"))),
    MetricDef(
        "REGION_MEMBERS", "Attributed members by region", "count",
        "Attributed members in the month, restricted to one region (patients.region = :dimension).",
        "SELECT COUNT(*) FROM member_months mm JOIN patients p ON p.patient_id = mm.patient_id "
        "WHERE mm.month = :month AND p.region = :dimension", abs_tolerance=0, dimension="region",
        dimension_values_sql="SELECT DISTINCT region FROM patients"),
    MetricDef(
        "REGION_COST_PMPM", "Cost per member per month by region", "usd",
        "Regional cost of care divided by regional attributed members for the same month.",
        "SELECT ROUND(1.0 * (SELECT COALESCE(SUM(c.amount), 0) FROM claims c "
        "JOIN patients p ON p.patient_id = c.patient_id WHERE p.region = :dimension "
        "AND c.service_ts_utc >= :start AND c.service_ts_utc < :end) / "
        "(SELECT COUNT(*) FROM member_months mm JOIN patients p2 ON p2.patient_id = mm.patient_id "
        "WHERE mm.month = :month AND p2.region = :dimension), 2)",
        abs_tolerance=0.01, dimension="region",
        dimension_values_sql="SELECT DISTINCT region FROM patients"),
    MetricDef(
        "REGION_ED_PER_1000", "ED visits per 1,000 members by region", "rate",
        "Regional ED visits divided by regional attributed members, annualized per 1,000 members.",
        "SELECT ROUND(12000.0 * (SELECT COUNT(*) FROM encounters e JOIN patients p "
        "ON p.patient_id = e.patient_id WHERE p.region = :dimension AND e.encounter_type = 'ed' "
        "AND e.admit_ts_utc >= :start AND e.admit_ts_utc < :end) / "
        "(SELECT COUNT(*) FROM member_months mm JOIN patients p2 ON p2.patient_id = mm.patient_id "
        "WHERE mm.month = :month AND p2.region = :dimension), 1)",
        abs_tolerance=0.05, dimension="region",
        dimension_values_sql="SELECT DISTINCT region FROM patients"),
]}
