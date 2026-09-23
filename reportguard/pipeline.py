"""Orchestration.

multi-agent:
  1. extractor     reads the artifacts        (list_artifacts, read_pdf_text + page images)
  2. planner       maps figures to metrics    (list_metrics, get_metric_definition)
  3. host          runs check_metric for each planned check, no LLM
  4. investigator  root causes for failures   (check_metric, run_sql, get_schema, get_metric_definition)
  5. critic        reviews the findings       (check_metric, run_sql, get_metric_definition)

The extractor is the only agent that sees document content and it has no db tools.

single-agent: one agent with all tools, used as a baseline in evals.
"""

from __future__ import annotations

import json
import re
import sys
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from . import config
from .agents import AgentConfig, Tracer, build_system_prompt, mcp_result_text, mcp_tools_to_specs, run_agent
from .llm.base import Provider
from . import metrics as metrics_mod
from .metrics import UNIT_SCALES
from .schemas import (CriticOutput, ExtractionOutput, Finding, InvestigationOutput, Plan, ReportedFigure,
                      SingleAgentOutput, SkippedFigure, Verdict)

ALLOWLISTS = {
    "extractor": {"list_artifacts", "read_pdf_text", "get_semantic_model"},
    "planner": {"list_metrics", "get_metric_definition"},
    "investigator": {"check_metric", "run_sql", "get_schema", "get_metric_definition"},
    "critic": {"check_metric", "run_sql", "get_metric_definition"},
    "single": {"list_artifacts", "read_pdf_text", "get_semantic_model", "list_metrics", "get_metric_definition",
               "get_schema", "check_metric", "run_sql"},
}


@dataclass
class RunResult:
    pack: str
    mode: str
    provider: str
    model: str
    period: str
    artifacts: list[str]
    extraction: dict | None = None
    plan: dict | None = None
    checks: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    verdicts: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)          # issues that go in the report
    consistency: list[dict] = field(default_factory=list)
    security_notes: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    second_look: list[dict] = field(default_factory=list)   # uncertain findings that were re-investigated
    error: str | None = None
    error_traceback: str | None = None

    def to_json(self) -> dict:
        return self.__dict__.copy()


def server_params() -> StdioServerParameters:
    env = get_default_environment()
    env.update({"RG_DATA_DIR": str(config.DATA_DIR), "RG_DOMAIN": config.DOMAIN, "PYTHONUTF8": "1"})
    return StdioServerParameters(command=sys.executable, args=[str(config.PROJECT_ROOT / "run_server.py")], env=env)


def root_exception(exc: BaseException) -> BaseException:
    """anyio wraps errors raised inside the MCP client context in ExceptionGroups; get the real one."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _record_error(result: "RunResult", exc: BaseException, say) -> None:
    root = root_exception(exc)
    result.error = f"{type(root).__name__}: {root}"
    result.error_traceback = "".join(traceback.format_exception(root))
    say(f"Run stopped: {result.error}")


@asynccontextmanager
async def connect_mcp():
    # server stderr goes to a file; in Jupyter/Colab sys.stderr can't be passed to a subprocess
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUNS_DIR / "mcp_server.log", "a") as log:
        async with Client(stdio_client(server_params(), errlog=log)) as client:
            yield client


def shift_period(period: str, months: int) -> str:
    """'2026-08', -1 -> '2026-07'."""
    year, month = (int(x) for x in period.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def pack_artifacts(pack: str, period: str) -> list[str]:
    if config.DOMAIN == "health":
        return [f"tab{i}_{period}_{pack}.png" for i in range(1, 5)]
    return [f"mbr_{period}_{pack}.pdf", f"dashboard_{period}_{pack}.png"]


_DISPLAY_RE = re.compile(r"^\s*(?P<neg>-)?\s*(?P<cur>\$)?\s*(?P<num>[\d,]*\.?\d+)\s*(?P<suf>[KkMm%])?\s*$")


def parse_display(text: str) -> tuple[float, str, int] | None:
    """'$246.5K' -> (246.5, '$K', 1); '1,105' -> (1105.0, '', 0); '5.1%' -> (5.1, '%', 1)."""
    m = _DISPLAY_RE.match(text or "")
    if not m:
        return None
    num = m.group("num").replace(",", "")
    value = float(num) * (-1 if m.group("neg") else 1)
    decimals = len(num.split(".")[1]) if "." in num else 0
    suffix = (m.group("suf") or "").upper()
    unit = "%" if suffix == "%" else f"{m.group('cur') or ''}{suffix}"
    return value, unit, decimals


def _scale(unit_label: str) -> float | None:
    key = (unit_label or "").strip().lower()
    for scales in UNIT_SCALES.values():
        if key in scales:
            return scales[key]
    return None


def validate_extraction(out: ExtractionOutput, artifacts: list[str]) -> list[str]:
    errors, seen = [], set()
    if not out.figures:
        errors.append("No figures extracted. Every KPI, table number and chart data label must be listed.")
    for f in out.figures:
        if f.figure_id in seen:
            errors.append(f"Duplicate figure_id {f.figure_id}")
        seen.add(f.figure_id)
        if f.artifact_id not in artifacts:
            errors.append(f"{f.figure_id}: artifact_id {f.artifact_id!r} is not one of {artifacts}")
        if _scale(f.unit_label) is None:
            errors.append(f"{f.figure_id}: unit_label {f.unit_label!r} must be one of '$', '$K', '$M', '%', 'K', 'M', ''")
        parsed = parse_display(f.displayed_text)
        if parsed:
            p_value, p_unit, p_dec = parsed
            if p_unit and p_unit != "$":  # text has K/M/% so compare scaled values
                s1, s2 = _scale(p_unit), _scale(f.unit_label)
                if s1 and s2 and abs(p_value * s1 - f.value * s2) > max(1e-6, 0.001 * abs(p_value * s1)):
                    errors.append(f"{f.figure_id}: value {f.value} {f.unit_label!r} contradicts displayed_text "
                                  f"{f.displayed_text!r}")
            elif abs(p_value - f.value) > 1e-6 * max(1, abs(p_value)):
                errors.append(f"{f.figure_id}: value {f.value} does not equal displayed_text {f.displayed_text!r}")
            if p_dec != f.display_decimals:
                errors.append(f"{f.figure_id}: display_decimals should be {p_dec} for {f.displayed_text!r}")
    return errors


def validate_plan(plan: Plan, figures: list[ReportedFigure]) -> list[str]:
    errors = []
    ids = {f.figure_id for f in figures}
    covered: dict[str, int] = {}
    for c in plan.checks:
        covered[c.figure_id] = covered.get(c.figure_id, 0) + 1
        if c.figure_id not in ids:
            errors.append(f"{c.check_id}: unknown figure_id {c.figure_id}")
        m = metrics_mod.METRICS.get(c.metric_id)
        if m is None:
            errors.append(f"{c.check_id}: unknown metric_id {c.metric_id}. Use list_metrics.")
        elif m.dimension and not c.dimension_value:
            errors.append(f"{c.check_id}: {c.metric_id} requires dimension_value")
        elif not m.dimension and c.dimension_value:
            errors.append(f"{c.check_id}: {c.metric_id} takes no dimension_value; set it to null")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", c.period):
            errors.append(f"{c.check_id}: period {c.period!r} must be YYYY-MM")
    for s in plan.skipped:
        covered[s.figure_id] = covered.get(s.figure_id, 0) + 1
    for fid in sorted(ids):
        if covered.get(fid, 0) == 0:
            errors.append(f"Figure {fid} is neither checked nor skipped")
        elif covered[fid] > 1:
            errors.append(f"Figure {fid} appears {covered[fid]} times; use it exactly once")
    return errors


def validate_findings(out: InvestigationOutput, failed_ids: set[str]) -> list[str]:
    got = [f.check_id for f in out.findings]
    errors = [f"Missing finding for failed check {c}" for c in sorted(failed_ids - set(got))]
    errors += [f"{c} is not a failed check" for c in sorted(set(got) - failed_ids)]
    errors += [f"Duplicate finding for {c}" for c in sorted({c for c in got if got.count(c) > 1})]
    return errors


def validate_verdicts(out: CriticOutput, finding_ids: set[str]) -> list[str]:
    got = [v.finding_id for v in out.verdicts]
    errors = [f"Missing verdict for {f}" for f in sorted(finding_ids - set(got))]
    errors += [f"Unknown finding_id {f}" for f in sorted(set(got) - finding_ids)]
    return errors


def salvage_extraction(out: ExtractionOutput | None, artifacts: list[str]) -> ExtractionOutput | None:
    if out is None:
        return None
    bad = {e.split(":")[0] for e in validate_extraction(out, artifacts)}
    seen, keep = set(), []
    for f in out.figures:
        if f.figure_id not in bad and f.figure_id not in seen:
            seen.add(f.figure_id)
            keep.append(f)
    return out.model_copy(update={"figures": keep}) if keep else None


def salvage_plan(plan: Plan | None, figures: list[ReportedFigure]) -> Plan | None:
    if plan is None:
        return None
    ids = {f.figure_id for f in figures}
    checks, used = [], set()
    for c in plan.checks:
        m = metrics_mod.METRICS.get(c.metric_id)
        ok = (c.figure_id in ids and c.figure_id not in used and m is not None
              and bool(m.dimension) == bool(c.dimension_value) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", c.period))
        if ok:
            used.add(c.figure_id)
            checks.append(c)
    skipped = [s for s in plan.skipped if s.figure_id in ids and s.figure_id not in used]
    used |= {s.figure_id for s in skipped}
    skipped += [SkippedFigure(figure_id=f, reason="not validly planned (dropped by host)") for f in sorted(ids - used)]
    return Plan(checks=checks, skipped=skipped)


def salvage_findings(out: InvestigationOutput | None, failed_ids: set[str]) -> InvestigationOutput:
    keep, seen = [], set()
    for f in (out.findings if out else []):
        if f.check_id in failed_ids and f.check_id not in seen:
            seen.add(f.check_id)
            keep.append(f)
    for n, cid in enumerate(sorted(failed_ids - seen), 1):
        keep.append(Finding(finding_id=f"AUTO{n}", check_id=cid, root_cause="other", confidence="low",
                            explanation="The investigator did not return a finding for this failed check."))
    return InvestigationOutput(findings=keep)


def salvage_verdicts(out: CriticOutput | None, finding_ids: set[str]) -> CriticOutput:
    keep = {v.finding_id: v for v in (out.verdicts if out else []) if v.finding_id in finding_ids}
    for fid in finding_ids - set(keep):
        keep[fid] = Verdict(finding_id=fid, verdict="uncertain", reason="No verdict returned by the critic.")
    return CriticOutput(verdicts=list(keep.values()))


def _j(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


async def _images(mcp, artifacts: list[str], provider: Provider, tracer: Tracer) -> list[dict]:
    if not provider.supports_vision:
        return []
    listing = json.loads(mcp_result_text(await mcp.call_tool("list_artifacts", {})))
    pages = {a["artifact_id"]: a["pages"] for a in listing["artifacts"]}
    parts = []
    for art in artifacts:
        for page in range(1, pages.get(art, 1) + 1):
            res = await mcp.call_tool("get_artifact_image", {"artifact_id": art, "page": page})
            tracer.add("host", "tool_call", tool="get_artifact_image", args={"artifact_id": art, "page": page},
                       is_error=bool(res.is_error), latency_s=0)
            for c in res.content:
                if getattr(c, "type", "") == "image":
                    parts.append({"type": "text", "text": f"[Image: {art}, page {page}]"})
                    parts.append({"type": "image", "mime": c.mime_type, "data_b64": c.data})
    return parts


def _consistency(figures: dict[str, ReportedFigure], checks: list[dict]) -> list[dict]:
    """Figures for the same metric/dimension/period that don't agree with each other."""
    groups: dict[tuple, list[dict]] = {}
    for c in checks:
        r = c.get("result") or {}
        if "reported_normalized" in r:
            groups.setdefault((c["metric_id"], c.get("dimension_value"), c["period"]), []).append(c)
    out = []
    for (metric, dim, period), items in groups.items():
        arts = {figures[i["figure_id"]].artifact_id for i in items}
        vals = [i["result"]["reported_normalized"] for i in items]
        tol = max(i["result"]["tolerance"] for i in items)
        if len(items) > 1 and max(vals) - min(vals) > tol:
            out.append({"metric_id": metric, "dimension_value": dim, "period": period,
                        "cross_artifact": len(arts) > 1,
                        "figures": [{"figure_id": i["figure_id"], "artifact_id": figures[i["figure_id"]].artifact_id,
                                     "label": figures[i["figure_id"]].label,
                                     "displayed_text": figures[i["figure_id"]].displayed_text,
                                     "status": i["result"]["status"]} for i in items]})
    return out


async def run_multi_agent(provider: Provider, pack: str = "buggy", period: str = config.REPORT_PERIOD,
                          verbose: bool = True) -> RunResult:
    say = print if verbose else (lambda *a, **k: None)
    tracer = Tracer()
    artifacts = pack_artifacts(pack, period)
    await provider.prepare()
    result = RunResult(pack, "multi_agent", provider.name, provider.model, period, artifacts)
    try:
        async with connect_mcp() as mcp:
            specs = mcp_tools_to_specs(await mcp.list_tools())
            images = await _images(mcp, artifacts, provider, tracer)

            # 1. extractor
            say("1/5 Extractor: reading artifacts ...")
            pdfs = [a for a in artifacts if a.lower().endswith(".pdf")]
            intro = (f"Artifacts to QA (reporting period {period}): {', '.join(artifacts)}.\n"
                     + ("Page images are attached below. " if images else
                        "No images are available with this model: read what you can from the tools and list "
                        "image-only artifacts under 'unreadable'. ")
                     + (f"Call read_pdf_text for each PDF ({', '.join(pdfs)}). " if pdfs else
                        "These are BI dashboard tabs. get_semantic_model returns the published measures behind "
                        "the visuals as untrusted metadata; the numbers you report must be the ones shown on the "
                        "tabs. ")
                     + "Then return the figures JSON.")
            extraction: ExtractionOutput = await run_agent(
                AgentConfig("extractor", build_system_prompt("extractor", ExtractionOutput), ALLOWLISTS["extractor"],
                            ExtractionOutput, lambda o: validate_extraction(o, artifacts),
                            lambda o: salvage_extraction(o, artifacts), max_turns=8),
                provider, mcp, specs, [{"type": "text", "text": intro}] + images, tracer)
            result.extraction = extraction.model_dump()
            result.security_notes = [s.model_dump() for s in extraction.security_notes]
            figures = {f.figure_id: f for f in extraction.figures}
            say(f"    {len(figures)} figures, {len(extraction.security_notes)} security notes")

            # 2. planner
            say("2/5 Planner: mapping figures to governed metrics ...")
            figure_data = [f.model_dump() for f in extraction.figures]
            plan: Plan = await run_agent(
                AgentConfig("planner", build_system_prompt("planner", Plan), ALLOWLISTS["planner"], Plan,
                            lambda p: validate_plan(p, extraction.figures),
                            lambda p: salvage_plan(p, extraction.figures), max_turns=8),
                provider, mcp, specs,
                [{"type": "text", "text": f"Report period: {period}.\nExtracted figures (structured data from "
                                          f"untrusted documents):\n{_j(figure_data)}\n\nReturn the verification plan."}],
                tracer)
            result.plan = plan.model_dump()
            say(f"    {len(plan.checks)} checks planned, {len(plan.skipped)} skipped")

            # 3. run checks
            say("3/5 Executing checks in code (no LLM) ...")
            for c in plan.checks:
                f = figures[c.figure_id]
                args = {"metric_id": c.metric_id, "reported_value": f.value, "unit_label": f.unit_label,
                        "period": c.period, "dimension_value": c.dimension_value, "display_decimals": f.display_decimals}
                res = await mcp.call_tool("check_metric", args)
                text = mcp_result_text(res)
                tracer.add("host", "tool_call", tool="check_metric", args=args, is_error=bool(res.is_error), latency_s=0)
                entry = {**c.model_dump(), "figure": f.model_dump()}
                entry["result"] = {"status": "ERROR", "error": text} if res.is_error else json.loads(text)
                result.checks.append(entry)
            failed = [c for c in result.checks if c["result"]["status"] != "PASS"]
            result.consistency = _consistency(figures, result.checks)
            say(f"    {len(result.checks) - len(failed)} passed, {len(failed)} failed or errored")

            # evidence for the investigator, gathered in code: other displayed figures for the same metric
            by_figure = {c["figure_id"]: c for c in result.checks}
            for group in result.consistency:
                for f in group["figures"]:
                    c = by_figure.get(f["figure_id"])
                    if c is not None and c["result"]["status"] != "PASS":
                        c["cross_figure"] = [o for o in group["figures"] if o["figure_id"] != f["figure_id"]]

            # evidence for the investigator: the same metric shown somewhere else with a different value
            cross = {}
            for group in result.consistency:
                for fig in group["figures"]:
                    cross[fig["figure_id"]] = [o for o in group["figures"] if o["figure_id"] != fig["figure_id"]]
            cross_hits = 0
            for c in failed:
                if c["figure_id"] in cross:
                    c["cross_figure"] = cross[c["figure_id"]]
                    cross_hits += 1
            if cross_hits:
                say(f"    cross-figure: {cross_hits} failing number(s) appear elsewhere with a different value")

            # evidence for the investigator, gathered in code: does a failing number match an adjacent month?
            adjacent_matches = 0
            for c in failed:
                if c["result"]["status"] != "FAIL":
                    continue
                f = c["figure"]
                evidence = []
                for p in (shift_period(c["period"], -1), shift_period(c["period"], 1)):
                    args = {"metric_id": c["metric_id"], "reported_value": f["value"], "unit_label": f["unit_label"],
                            "period": p, "dimension_value": c["dimension_value"],
                            "display_decimals": f["display_decimals"]}
                    res = await mcp.call_tool("check_metric", args)
                    tracer.add("host", "tool_call", tool="check_metric", args=args, is_error=bool(res.is_error),
                               latency_s=0)
                    if not res.is_error:
                        r = json.loads(mcp_result_text(res))
                        evidence.append({"period": p, "status": r["status"], "expected": r["expected"]})
                c["adjacent_periods"] = evidence
                adjacent_matches += any(e["status"] == "PASS" for e in evidence)
            if failed:
                say(f"    month evidence: {adjacent_matches} of {len(failed)} failing numbers match the previous or next "
                    f"month exactly")

            # 4. investigator
            findings: InvestigationOutput = InvestigationOutput(findings=[])
            if failed:
                say("4/5 Investigator: root-causing failures ...")
                failed_ids = {c["check_id"] for c in failed}
                findings = await run_agent(
                    AgentConfig("investigator", build_system_prompt("investigator", InvestigationOutput, True),
                                ALLOWLISTS["investigator"], InvestigationOutput,
                                lambda o: validate_findings(o, failed_ids),
                                lambda o: salvage_findings(o, failed_ids), max_turns=16),
                    provider, mcp, specs,
                    [{"type": "text", "text": f"Report period: {period}. Failed checks:\n{_j(failed)}\n\n"
                                              f"Investigate each and return one finding per check_id."}], tracer)
            result.findings = [f.model_dump() for f in findings.findings]

            # 5. critic
            verdicts: dict[str, dict] = {}
            if findings.findings:
                say("5/5 Critic: challenging findings ...")
                by_check = {c["check_id"]: c for c in failed}
                finding_ids = {f.finding_id for f in findings.findings}
                review = [{**f.model_dump(), "check": by_check[f.check_id]} for f in findings.findings]
                critic: CriticOutput = await run_agent(
                    AgentConfig("critic", build_system_prompt("critic", CriticOutput, True), ALLOWLISTS["critic"],
                                CriticOutput, lambda o: validate_verdicts(o, finding_ids),
                                lambda o: salvage_verdicts(o, finding_ids), max_turns=10),
                    provider, mcp, specs,
                    [{"type": "text", "text": f"Findings to review:\n{_j(review)}\n\nReturn one verdict per finding."}],
                    tracer)
                verdicts = {v.finding_id: v.model_dump() for v in critic.verdicts}

                # second look: re-investigate what the critic could not verify, then review it again
                uncertain = [f for f in findings.findings if verdicts[f.finding_id]["verdict"] == "uncertain"]
                if uncertain:
                    say(f"Second look: re-investigating {len(uncertain)} of {len(findings.findings)} findings the critic "
                        f"marked uncertain ...")
                    retry_ids = {f.check_id for f in uncertain}
                    brief = [{**by_check[f.check_id], "first_root_cause": f.root_cause,
                              "first_explanation": f.explanation,
                              "critic_objection": verdicts[f.finding_id]["reason"]} for f in uncertain]
                    retry: InvestigationOutput = await run_agent(
                        AgentConfig("second_look", build_system_prompt("investigator", InvestigationOutput, True),
                                    ALLOWLISTS["investigator"], InvestigationOutput,
                                    lambda o: validate_findings(o, retry_ids),
                                    lambda o: salvage_findings(o, retry_ids), max_turns=16),
                        provider, mcp, specs,
                        [{"type": "text", "text": f"Report period: {period}. Failed checks:\n{_j(brief)}\n\n"
                                                  f"The critic could not verify your first explanation for these "
                                                  f"checks; its objection is in critic_objection. Re-investigate "
                                                  f"each one: reproduce the reported number exactly with SQL, or "
                                                  f"return root_cause 'other' with low confidence. Return one "
                                                  f"finding per check_id."}], tracer)
                    fresh = [f.model_copy(update={"finding_id": f"S{n}"}) for n, f in enumerate(retry.findings, 1)]
                    fresh_ids = {f.finding_id for f in fresh}
                    review2 = [{**f.model_dump(), "check": by_check[f.check_id]} for f in fresh]
                    critic2: CriticOutput = await run_agent(
                        AgentConfig("second_critic", build_system_prompt("critic", CriticOutput, True),
                                    ALLOWLISTS["critic"], CriticOutput, lambda o: validate_verdicts(o, fresh_ids),
                                    lambda o: salvage_verdicts(o, fresh_ids), max_turns=10),
                        provider, mcp, specs,
                        [{"type": "text", "text": f"Findings to review:\n{_j(review2)}\n\nReturn one verdict per "
                                                  f"finding."}], tracer)
                    second = {v.finding_id: v.model_dump() for v in critic2.verdicts}
                    for f in fresh:
                        first = next(u for u in uncertain if u.check_id == f.check_id)
                        result.second_look.append({"check_id": f.check_id, "first_root_cause": first.root_cause,
                                                   "root_cause": f.root_cause,
                                                   "verdict": second[f.finding_id]["verdict"]})
                    verdicts.update(second)
                    order = {c["check_id"]: n for n, c in enumerate(failed)}
                    merged = [f for f in findings.findings if f.check_id not in retry_ids] + fresh
                    findings = InvestigationOutput(findings=sorted(merged, key=lambda f: order[f.check_id]))
                    result.findings = [f.model_dump() for f in findings.findings]
                    confirmed_now = sum(1 for x in result.second_look if x["verdict"] == "confirmed")
                    say(f"    {confirmed_now} of {len(fresh)} confirmed on the second look")
                result.verdicts = list(verdicts.values())

            by_check = {c["check_id"]: c for c in result.checks}
            for f in result.findings:
                v = verdicts.get(f["finding_id"], {"verdict": "unreviewed", "reason": ""})
                if v["verdict"] == "rejected":
                    continue
                c = by_check[f["check_id"]]
                result.issues.append({
                    "artifact_id": c["figure"]["artifact_id"], "label": c["figure"]["label"],
                    "displayed_text": c["figure"]["displayed_text"], "metric_id": c["metric_id"],
                    "dimension_value": c["dimension_value"], "period": c["period"], "result": c["result"],
                    "root_cause": f["root_cause"], "confidence": f["confidence"], "explanation": f["explanation"],
                    "evidence_sql": f["evidence_sql"], "verdict": v["verdict"], "critic_reason": v["reason"],
                    "second_look": f["finding_id"].startswith("S")})
            say("Done.")
    except Exception as exc:  # keep partial results
        _record_error(result, exc, say)
    result.stats = tracer.stats()
    result.trace = tracer.events
    return result


async def run_single_agent(provider: Provider, pack: str = "buggy", period: str = config.REPORT_PERIOD,
                           verbose: bool = True) -> RunResult:
    say = print if verbose else (lambda *a, **k: None)
    tracer = Tracer()
    artifacts = pack_artifacts(pack, period)
    await provider.prepare()
    result = RunResult(pack, "single_agent", provider.name, provider.model, period, artifacts)
    try:
        async with connect_mcp() as mcp:
            specs = mcp_tools_to_specs(await mcp.list_tools())
            images = await _images(mcp, artifacts, provider, tracer)
            say("Single agent: running the whole QA job ...")
            out: SingleAgentOutput = await run_agent(
                AgentConfig("single", build_system_prompt("single", SingleAgentOutput, True), ALLOWLISTS["single"],
                            SingleAgentOutput, None, max_turns=25),
                provider, mcp, specs,
                [{"type": "text", "text": f"QA these artifacts for period {period}: {', '.join(artifacts)}. "
                                          f"Page images are attached."}] + images, tracer)
            result.issues = [{**i.model_dump(), "verdict": "unreviewed"} for i in out.issues]
            result.security_notes = [s.model_dump() for s in out.security_notes]
            say(f"Done. {len(result.issues)} issues reported.")
    except Exception as exc:  # keep partial results
        _record_error(result, exc, say)
    result.stats = tracer.stats()
    result.trace = tracer.events
    return result


def save_run(result: RunResult, name: str | None = None) -> Path:
    from .qa_report import render_markdown
    run_dir = config.RUNS_DIR / (name or f"{time.strftime('%Y%m%d-%H%M%S')}_{result.mode}_{result.pack}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps(result.to_json(), indent=1, default=str), encoding="utf-8")
    (run_dir / "qa_report.md").write_text(render_markdown(result), encoding="utf-8")
    if result.error_traceback:
        (run_dir / "error.txt").write_text(result.error_traceback, encoding="utf-8")
    return run_dir
