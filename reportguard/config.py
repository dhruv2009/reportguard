"""Paths and settings. Override with RG_* env vars."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RG_DATA_DIR", PROJECT_ROOT / "data"))

DB_PATH = DATA_DIR / "warehouse.db"          # opened read-only by the tools
REPORTS_DIR = DATA_DIR / "reports"           # reports/dashboards to check
MANIFEST_DIR = DATA_DIR / "manifests"        # answer keys, not exposed via MCP
RUNS_DIR = DATA_DIR / "runs"                 # run outputs
SKILL_PATH = PROJECT_ROOT / "skills" / "report-qa" / "SKILL.md"
CACHE_DIR = Path(os.environ.get("RG_CACHE_DIR", DATA_DIR / "llm_cache"))

REPORT_PERIOD = os.environ.get("RG_PERIOD", "2026-08")
SQL_MAX_ROWS = int(os.environ.get("RG_SQL_MAX_ROWS", "50"))
SQL_MAX_VM_STEPS = int(os.environ.get("RG_SQL_MAX_VM_STEPS", "5000000"))
