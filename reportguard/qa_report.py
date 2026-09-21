"""Markdown QA report. Numbers come from check_metric results."""

from __future__ import annotations

import re


def _fmt(v, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "usd":
        return f"${v:,.2f}"
    if unit == "percent":
        return f"{v:.2f}%"
    return f"{v:,.0f}"


def _escape_dollars(md: str) -> str:
    """Escape $ outside code so Colab/Jupyter don't render amounts as LaTeX math."""
    parts = re.split(r"(```.*?```|`[^`\n]*`)", md, flags=re.S)
    return "".join(p if p.startswith("`") else p.replace("$", "\\$") for p in parts)


def render_markdown(r) -> str:
    lines = [f"# ReportGuard QA report: {r.pack} pack, {r.period}", "",
             f"Mode: **{r.mode}** | Model: `{r.provider}:{r.model}` | Artifacts: {', '.join(r.artifacts)}", ""]
    if r.error:
        lines += [f"> Run did not finish: {r.error}", ""]
        if getattr(r, "error_traceback", None):
            lines += ["```", r.error_traceback[-2500:], "```", ""]

    n_checks = len(r.checks)
    n_pass = sum(1 for c in r.checks if c["result"]["status"] == "PASS")
    confirmed = [i for i in r.issues if i.get("verdict") in ("confirmed", "unreviewed")]
    uncertain = [i for i in r.issues if i.get("verdict") == "uncertain"]
    if r.mode == "multi_agent":
        lines += [f"**{n_checks} numbers checked, {n_pass} passed, {len(confirmed)} confirmed issues, "
                  f"{len(uncertain)} uncertain.**", ""]
    else:
        lines += [f"**{len(r.issues)} issues reported by the single agent.**", ""]

    if r.security_notes:
        lines += ["## Security notes", ""]
        lines += [f"- `{s['artifact_id']}`: {s['description']}" for s in r.security_notes]
        lines.append("")
    blocked = r.stats.get("security_events", [])
    if blocked:
        lines += [f"- {len(blocked)} tool call(s) outside an agent's allowlist were blocked.", ""]

    if r.issues:
        lines += ["## Issues", "", "| # | Artifact | Figure | Shown | Expected | Delta | Root cause | Verdict |",
                  "|---|---|---|---|---|---|---|---|"]
        for n, i in enumerate(r.issues, 1):
            res = i.get("result") or {}
            unit = res.get("unit", "")
            delta = f"{res['delta_pct']:+.1f}%" if res.get("delta_pct") is not None else "n/a"
            lines.append(f"| {n} | {i['artifact_id']} | {i['label']} | {i['displayed_text']} | "
                         f"{_fmt(res.get('expected'), unit)} | {delta} | {i['root_cause']} | {i.get('verdict', '')} |")
        lines.append("")
        for n, i in enumerate(r.issues, 1):
            lines += [f"### {n}. {i['label']} ({i['artifact_id']})", "", i.get("explanation", "")]
            if i.get("critic_reason"):
                lines += ["", f"_Critic ({i['verdict']}):_ {i['critic_reason']}"]
            for q in i.get("evidence_sql") or []:
                lines += ["", "```sql", q, "```"]
            lines.append("")

    if r.consistency:
        lines += ["## Cross-figure inconsistencies", ""]
        for c in r.consistency:
            shown = "; ".join(f"{f['label']} = {f['displayed_text']} in {f['artifact_id']} ({f['status']})"
                              for f in c["figures"])
            dim = f" [{c['dimension_value']}]" if c["dimension_value"] else ""
            lines.append(f"- **{c['metric_id']}{dim}**: {shown}")
        lines.append("")

    s = r.stats
    if s:
        lines += ["## Run stats", "",
                  f"LLM calls: {s['llm_calls']} ({s['llm_calls_from_cache']} from cache) | Tool calls: {s['tool_calls']} "
                  f"({s['tool_errors']} errors) | Tokens in/out: {s['input_tokens']:,}/{s['output_tokens']:,} | "
                  f"Validation retries: {s['validation_retries']} | Salvaged outputs: {s.get('salvaged_outputs', 0)} | "
                  f"Wall time: {s['wall_time_s']}s", ""]
        lines += ["| Agent | LLM calls | Tool calls | Tokens in | Tokens out |", "|---|---|---|---|---|"]
        for agent, a in s["by_agent"].items():
            lines.append(f"| {agent} | {a['llm_calls']} | {a['tool_calls']} | {a['input_tokens']:,} | {a['output_tokens']:,} |")
    return _escape_dollars("\n".join(lines) + "\n")

