"""Model-level validation: compare a BI report's published measures against the warehouse.

This is the path that scales. Every measure in the semantic model is recomputed from its
governed definition and compared in code, so a report with hundreds of measures costs
nothing in model calls. It catches wrong values, but it cannot see anything that exists
only in the rendering: a chart drawn from a stale extract, or a tile whose label says
thousands while the number is in dollars. That is what the agent pass over the rendered
tabs is for.
"""

from __future__ import annotations

import json
import time

from .. import config
from .. import metrics as metrics_mod
from ..metrics import check_metric
from ..sql_guard import connect_readonly

UNIT_LABELS = {"usd": "$", "percent": "%"}


def load_semantic_model(pack: str = "buggy", period: str | None = None) -> dict:
    period = period or config.REPORT_PERIOD
    path = config.REPORTS_DIR / f"semantic_model_{period}_{pack}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_semantic_model(pack: str = "buggy", period: str | None = None) -> dict:
    """Check every published measure against its governed definition. No LLM calls."""
    model = load_semantic_model(pack, period)
    started = time.monotonic()
    conn = connect_readonly(config.DB_PATH)
    results = []
    try:
        for m in model["measures"]:
            metric = metrics_mod.METRICS.get(m["metric_id"])
            if metric is None:
                results.append({**m, "status": "UNMAPPED"})
                continue
            unit = UNIT_LABELS.get(metric.unit, "")
            decimals = 0 if metric.unit == "count" else 2
            r = check_metric(conn, m["metric_id"], m["published_value"], unit, model["period"],
                             m["dimension_value"], decimals)
            results.append({"measure": m["measure"], "tab": m["tab"], "visual": m["visual"],
                            "metric_id": m["metric_id"], "dimension_value": m["dimension_value"],
                            "expression": m["expression"], "published_value": m["published_value"],
                            "expected": r["expected"], "delta_pct": r["delta_pct"], "status": r["status"]})
    finally:
        conn.close()
    failed = [r for r in results if r["status"] != "PASS"]
    return {"pack": pack, "period": model["period"], "measures_checked": len(results),
            "passed": len(results) - len(failed), "failed": failed, "llm_calls": 0,
            "wall_time_s": round(time.monotonic() - started, 2), "results": results}


def report_markdown(summary: dict) -> str:
    lines = [f"**Model check: {summary['measures_checked']} published measures, {summary['passed']} match the "
             f"warehouse, {len(summary['failed'])} do not.** No model calls, {summary['wall_time_s']}s.", ""]
    if summary["failed"]:
        lines += ["| Tab | Measure | Published | Expected | Delta | Expression |", "|---|---|---|---|---|---|"]
        for f in summary["failed"]:
            delta = f"{f['delta_pct']:+.1f}%" if f.get("delta_pct") is not None else ""
            lines.append(f"| {f['tab']} | {f['measure']} | {f['published_value']:,.2f} | {f['expected']:,.2f} | "
                         f"{delta} | `{f['expression']}` |")
    return "\n".join(lines) + "\n"


def compare_paths(model_summary: dict, agent_result=None, pack: str = "buggy") -> str:
    """Which planted bug each validation path caught: the code-only model check, and the agents
    reading the rendered tabs."""
    manifest = json.loads((config.MANIFEST_DIR / f"{pack}.json").read_text(encoding="utf-8"))
    key = lambda m, d: (m, (d or "").strip().lower() or None)
    model_hits = {key(f["metric_id"], f["dimension_value"]) for f in model_summary["failed"]}
    issues = [] if agent_result is None else [i for i in (agent_result if isinstance(agent_result, dict)
                                                          else agent_result.to_json())["issues"]
                                              if i.get("verdict") != "rejected"]
    agent_hits = {key(i["metric_id"], i.get("dimension_value")) for i in issues}

    lines = ["| Bug | What went wrong | Model check (code) | Rendered tabs (agents) |", "|---|---|---|---|"]
    for b in manifest["bugs"]:
        k = key(b["metric_id"], b["dimension_value"])
        agent_cell = ("caught" if k in agent_hits else "missed") if agent_result is not None else "not run"
        lines.append(f"| {b['bug_id']} | {b['description']} | {'caught' if k in model_hits else 'missed'} "
                     f"| {agent_cell} |")
    n_model = sum(1 for b in manifest["bugs"] if key(b["metric_id"], b["dimension_value"]) in model_hits)
    cost = f"{model_summary['measures_checked']} measures, 0 model calls, {model_summary['wall_time_s']}s"
    lines += ["", f"Model check: {n_model}/{len(manifest['bugs'])} bugs, {cost}. It compares published measures "
                  f"against the warehouse, so it can't see a bug that only exists in the rendering."]
    if agent_result is not None:
        stats = (agent_result if isinstance(agent_result, dict) else agent_result.to_json())["stats"]
        n_agent = sum(1 for b in manifest["bugs"] if key(b["metric_id"], b["dimension_value"]) in agent_hits)
        lines.append(f"Rendered tabs: {n_agent}/{len(manifest['bugs'])} bugs, {stats['llm_calls']} model calls, "
                     f"{stats['wall_time_s']}s. It reads what a viewer sees, so it catches label and chart bugs "
                     f"the model check can't.")
    return "\n".join(lines) + "\n"
