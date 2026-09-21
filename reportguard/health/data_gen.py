"""Synthetic population health warehouse (SQLite), seeded so numbers are reproducible.

Members are attributed to a payer contract for stretches of time (member_months),
which is what per-member-per-month and per-1000 metrics are built on.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE patients (
    patient_id      INTEGER PRIMARY KEY,
    birth_date      TEXT NOT NULL,
    sex             TEXT NOT NULL,
    region          TEXT NOT NULL,
    payer           TEXT NOT NULL,
    risk_score      REAL NOT NULL,
    has_diabetes    INTEGER NOT NULL,
    has_hypertension INTEGER NOT NULL
);
CREATE TABLE member_months (
    patient_id  INTEGER NOT NULL REFERENCES patients(patient_id),
    month       TEXT NOT NULL,              -- 'YYYY-MM'
    PRIMARY KEY (patient_id, month)
);
CREATE TABLE encounters (
    encounter_id     INTEGER PRIMARY KEY,
    patient_id       INTEGER NOT NULL REFERENCES patients(patient_id),
    admit_ts_utc     TEXT NOT NULL,
    discharge_ts_utc TEXT,
    encounter_type   TEXT NOT NULL CHECK (encounter_type IN ('office', 'ed', 'inpatient')),
    disposition      TEXT                    -- inpatient only: home | snf | transfer | expired
);
CREATE TABLE claims (
    claim_id      INTEGER PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(patient_id),
    encounter_id  INTEGER REFERENCES encounters(encounter_id),
    service_ts_utc TEXT NOT NULL,
    category      TEXT NOT NULL CHECK (category IN ('inpatient', 'outpatient', 'ed', 'professional', 'pharmacy')),
    amount        REAL NOT NULL
);
CREATE TABLE labs (
    lab_id     INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(patient_id),
    taken_ts_utc TEXT NOT NULL,
    code       TEXT NOT NULL,               -- HBA1C | BP_SYSTOLIC
    value      REAL NOT NULL
);
CREATE TABLE care_gaps (
    gap_id      INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(patient_id),
    measure_id  TEXT NOT NULL,              -- HBA1C_SCREEN | BP_CONTROL | WELLNESS_VISIT
    opened_ts_utc TEXT NOT NULL,
    closed_ts_utc TEXT
);
CREATE INDEX idx_enc_admit ON encounters(admit_ts_utc);
CREATE INDEX idx_claims_ts ON claims(service_ts_utc);
CREATE INDEX idx_labs_ts ON labs(taken_ts_utc);
CREATE INDEX idx_mm_month ON member_months(month);
"""

REGIONS = ["Northeast", "Midwest", "South", "West"]
PAYERS = ["Medicare Advantage", "Commercial", "Medicaid"]
CATEGORY_MIX = [("inpatient", 0.06, 4200, 2600), ("outpatient", 0.22, 620, 380),
                ("ed", 0.08, 1450, 700), ("professional", 0.34, 210, 120), ("pharmacy", 0.30, 165, 140)]
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
FMT = "%Y-%m-%d %H:%M:%S"


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return start, end


def build_warehouse(db_path: str | Path, seed: int = 11) -> dict:
    rng = random.Random(seed)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    patients, member_months = [], []
    for pid in range(1, 4001):
        region = rng.choices(REGIONS, [0.26, 0.24, 0.3, 0.2])[0]
        payer = rng.choices(PAYERS, [0.45, 0.4, 0.15])[0]
        age = rng.randint(19, 88) if payer != "Medicare Advantage" else rng.randint(65, 92)
        birth = datetime(2026, 1, 1) - timedelta(days=age * 365 + rng.randint(0, 364))
        risk = round(max(0.2, rng.gauss(1.5 if payer == "Medicare Advantage" else 0.9, 0.7)), 2)
        diabetes = 1 if rng.random() < (0.28 if payer == "Medicare Advantage" else 0.12) else 0
        htn = 1 if rng.random() < (0.55 if payer == "Medicare Advantage" else 0.24) else 0
        patients.append((pid, birth.strftime("%Y-%m-%d"), rng.choice(["F", "M"]), region, payer, risk, diabetes, htn))
        # attribution: most members enrolled all period, some join or leave mid-stream
        start_i = 0 if rng.random() < 0.88 else rng.randint(1, 3)
        end_i = len(MONTHS) - 1 if rng.random() < 0.93 else rng.randint(start_i, len(MONTHS) - 1)
        member_months += [(pid, MONTHS[i]) for i in range(start_i, end_i + 1)]
    conn.executemany("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?)", patients)
    conn.executemany("INSERT INTO member_months VALUES (?,?)", member_months)

    enrolled: dict[str, list[int]] = {m: [] for m in MONTHS}
    for pid, month in member_months:
        enrolled[month].append(pid)
    risk_of = {p[0]: p[5] for p in patients}

    encounters, claims, labs, gaps = [], [], [], []
    eid = cid = lid = gid = 1
    for month in MONTHS:
        start, end = _month_bounds(month)
        span = (end - start).total_seconds()
        members = enrolled[month]
        for pid in members:
            risk = risk_of[pid]
            for _ in range(rng.choices([0, 1, 2, 3], [0.55, 0.3, 0.1, 0.05])[0]):
                ts = start + timedelta(seconds=rng.uniform(0, span))
                encounters.append((eid, pid, ts.strftime(FMT), None, "office", None))
                claims.append((cid, pid, eid, ts.strftime(FMT), "professional", round(rng.gauss(210, 60), 2)))
                eid, cid = eid + 1, cid + 1
            if rng.random() < 0.035 * min(risk, 3.0):
                ts = start + timedelta(seconds=rng.uniform(0, span))
                encounters.append((eid, pid, ts.strftime(FMT), None, "ed", None))
                claims.append((cid, pid, eid, ts.strftime(FMT), "ed", round(rng.gauss(1450, 500), 2)))
                eid, cid = eid + 1, cid + 1
            if rng.random() < 0.012 * min(risk, 3.0):
                ts = start + timedelta(seconds=rng.uniform(0, span * 0.92))
                los = rng.choices([1, 2, 3, 4, 5, 7, 10], [0.2, 0.26, 0.2, 0.13, 0.1, 0.07, 0.04])[0]
                disp = rng.choices(["home", "snf", "transfer", "expired"], [0.78, 0.15, 0.04, 0.03])[0]
                encounters.append((eid, pid, ts.strftime(FMT), (ts + timedelta(days=los)).strftime(FMT),
                                   "inpatient", disp))
                claims.append((cid, pid, eid, ts.strftime(FMT), "inpatient", round(rng.gauss(4200, 1500) * los / 2, 2)))
                eid, cid = eid + 1, cid + 1
                # readmission within 30 days for some discharges
                if disp in ("home", "snf") and rng.random() < 0.14:
                    r_ts = ts + timedelta(days=los + rng.randint(2, 28), seconds=rng.randint(0, 86399))
                    r_los = rng.choices([1, 2, 3, 5], [0.3, 0.3, 0.25, 0.15])[0]
                    encounters.append((eid, pid, r_ts.strftime(FMT), (r_ts + timedelta(days=r_los)).strftime(FMT),
                                       "inpatient", "home"))
                    claims.append((cid, pid, eid, r_ts.strftime(FMT), "inpatient", round(rng.gauss(3900, 1200), 2)))
                    eid, cid = eid + 1, cid + 1
            for _ in range(rng.choices([0, 1, 2], [0.35, 0.45, 0.2])[0]):
                ts = start + timedelta(seconds=rng.uniform(0, span))
                claims.append((cid, pid, None, ts.strftime(FMT), "pharmacy", round(rng.gauss(165, 90), 2)))
                cid += 1
            if rng.random() < 0.18:
                ts = start + timedelta(seconds=rng.uniform(0, span))
                claims.append((cid, pid, None, ts.strftime(FMT), "outpatient", round(rng.gauss(620, 300), 2)))
                cid += 1

    by_id = {p[0]: p for p in patients}
    for pid, p in by_id.items():
        if p[6]:  # diabetes: HbA1c labs
            for month in MONTHS:
                if rng.random() < 0.22:
                    start, end = _month_bounds(month)
                    for _ in range(1 + (rng.random() < 0.28)):   # repeat tests happen: same member, two results
                        ts = start + timedelta(seconds=rng.uniform(0, (end - start).total_seconds()))
                        labs.append((lid, pid, ts.strftime(FMT), "HBA1C", round(rng.gauss(7.4, 1.3), 1)))
                        lid += 1
        if p[7]:  # hypertension: BP readings
            for month in MONTHS:
                if rng.random() < 0.38:
                    start, end = _month_bounds(month)
                    ts = start + timedelta(seconds=rng.uniform(0, (end - start).total_seconds()))
                    labs.append((lid, pid, ts.strftime(FMT), "BP_SYSTOLIC", round(rng.gauss(133, 15), 0)))
                    lid += 1
        for measure, chance in (("HBA1C_SCREEN", 0.30 if p[6] else 0.0), ("BP_CONTROL", 0.26 if p[7] else 0.0),
                                ("WELLNESS_VISIT", 0.2)):
            if rng.random() < chance:
                opened = datetime(2026, 4, 1) + timedelta(days=rng.randint(0, 120))
                closed = opened + timedelta(days=rng.randint(5, 90)) if rng.random() < 0.45 else None
                gaps.append((gid, pid, measure, opened.strftime(FMT), closed.strftime(FMT) if closed else None))
                gid += 1

    conn.executemany("INSERT INTO encounters VALUES (?,?,?,?,?,?)", encounters)
    conn.executemany("INSERT INTO claims VALUES (?,?,?,?,?,?)", claims)
    conn.executemany("INSERT INTO labs VALUES (?,?,?,?,?)", labs)
    conn.executemany("INSERT INTO care_gaps VALUES (?,?,?,?,?)", gaps)
    conn.commit()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ["patients", "member_months", "encounters", "claims", "labs", "care_gaps"]}
    conn.close()
    return counts
