"""Paths and settings. Override with RG_* env vars."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RG_DATA_DIR", PROJECT_ROOT / "data"))

DOMAIN = os.environ.get("RG_DOMAIN", "retail")   # retail | health
DOMAIN_DIR = DATA_DIR if DOMAIN == "retail" else DATA_DIR / DOMAIN

DB_PATH = DOMAIN_DIR / ("warehouse.db" if DOMAIN == "retail" else "population_health.db")
REPORTS_DIR = DOMAIN_DIR / "reports"         # reports/dashboards to check
MANIFEST_DIR = DOMAIN_DIR / "manifests"      # answer keys, not exposed via MCP
RUNS_DIR = DOMAIN_DIR / "runs"               # run outputs
SKILL_PATH = PROJECT_ROOT / "skills" / "report-qa" / "SKILL.md"
CACHE_DIR = Path(os.environ.get("RG_CACHE_DIR", DATA_DIR / "llm_cache"))

REPORT_PERIOD = os.environ.get("RG_PERIOD", "2026-08")
SQL_MAX_ROWS = int(os.environ.get("RG_SQL_MAX_ROWS", "50"))
SQL_MAX_VM_STEPS = int(os.environ.get("RG_SQL_MAX_VM_STEPS", "5000000"))


def set_domain(name: str) -> None:
    """Point everything at another domain's warehouse, artifacts and metric catalog.

        from reportguard import config; config.set_domain("health")
    """
    global DOMAIN, DOMAIN_DIR, DB_PATH, REPORTS_DIR, MANIFEST_DIR, RUNS_DIR
    if name not in ("retail", "health"):
        raise ValueError("domain must be retail or health")
    os.environ["RG_DOMAIN"] = name
    DOMAIN = name
    DOMAIN_DIR = DATA_DIR if name == "retail" else DATA_DIR / name
    DB_PATH = DOMAIN_DIR / ("warehouse.db" if name == "retail" else "population_health.db")
    REPORTS_DIR, MANIFEST_DIR, RUNS_DIR = DOMAIN_DIR / "reports", DOMAIN_DIR / "manifests", DOMAIN_DIR / "runs"
    from . import metrics
    if name == "health":
        from .health.metrics import METRICS as catalog
    else:
        catalog = metrics.RETAIL_METRICS
    metrics.METRICS = catalog
