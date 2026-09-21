"""Agent loop: tool calls restricted to an allowlist, JSON output validated
against a pydantic model (with repair attempts), and a tracer for calls/tokens.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from . import config
from .llm.base import LLMTurn, Part, Provider, ToolResult, ToolSpec

MAX_TOOL_RESULT_CHARS = 12_000


class AgentFailed(RuntimeError):
    pass


def load_skill_sections(path=config.SKILL_PATH) -> dict[str, str]:
    """Split SKILL.md into sections keyed by their '## ' heading."""
    text = path.read_text(encoding="utf-8")
    body = text.split("---", 2)[2] if text.startswith("---") else text
    sections, current, lines = {}, "_intro", []
    for line in body.splitlines():
        if line.startswith("## "):
            sections[current] = "\n".join(lines).strip()
            current, lines = line[3:].strip(), []
        else:
            lines.append(line)
    sections[current] = "\n".join(lines).strip()
    return sections


def build_system_prompt(role: str, output_model: type[BaseModel], include_signatures: bool = False,
                        extra_section: str | None = None) -> str:
    s = load_skill_sections()
    parts = [f"You are the {role} agent in ReportGuard, a data-quality system that checks business reports "
             f"against a data warehouse.", "## Shared rules\n" + s["Shared rules"]]
    role_key = "Single-agent mode" if role == "single" else f"Role: {role.capitalize()}"
    parts.append(f"## Your role\n{s[role_key]}")
    if include_signatures:
        parts.append("## Root-cause signatures\n" + s["Root-cause signatures"])
    if extra_section:
        parts.append(f"## {extra_section}\n" + s[extra_section])
    schema = json.dumps(output_model.model_json_schema(), separators=(",", ":"))
    parts.append(f"## Output\nWhen you are done, reply with ONLY a JSON object that validates against this JSON "
                 f"Schema:\n{schema}")
    return "\n\n".join(parts)


@dataclass
class Tracer:
    events: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def add(self, agent: str, kind: str, **data: Any) -> None:
        self.events.append({"t": round(time.monotonic() - self.started, 2), "agent": agent, "kind": kind, **data})

    def stats(self) -> dict:
        llm = [e for e in self.events if e["kind"] == "llm_call"]
        tools = [e for e in self.events if e["kind"] == "tool_call"]
        by_agent: dict[str, dict] = {}
        for e in llm:
            a = by_agent.setdefault(e["agent"], {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0})
            a["llm_calls"] += 1
            a["input_tokens"] += e.get("input_tokens", 0)
            a["output_tokens"] += e.get("output_tokens", 0)
        for e in tools:
            by_agent.setdefault(e["agent"], {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0})
            by_agent[e["agent"]]["tool_calls"] += 1
        return {
            "llm_calls": len(llm),
            "llm_calls_from_cache": sum(1 for e in llm if e.get("cached")),
            "tool_calls": len(tools),
            "tool_errors": sum(1 for e in tools if e.get("is_error")),
            "input_tokens": sum(e.get("input_tokens", 0) for e in llm),
            "output_tokens": sum(e.get("output_tokens", 0) for e in llm),
            "security_events": [e for e in self.events if e["kind"] == "security"],
            "validation_retries": sum(1 for e in self.events if e["kind"] == "validation_error"),
            "salvaged_outputs": sum(1 for e in self.events if e["kind"] == "salvaged"),
            "wall_time_s": round(time.monotonic() - self.started, 1),
            "by_agent": by_agent,
        }


def mcp_tools_to_specs(list_tools_result) -> dict[str, ToolSpec]:
    return {t.name: ToolSpec(t.name, t.description or "", t.input_schema or {"type": "object", "properties": {}})
            for t in list_tools_result.tools}


def mcp_result_text(result) -> str:
    chunks = []
    for c in result.content:
        if getattr(c, "type", "") == "text":
            chunks.append(c.text)
        elif getattr(c, "type", "") == "image":
            chunks.append("[image content omitted]")
    text = "\n".join(chunks)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f"\n...[truncated {len(text) - MAX_TOOL_RESULT_CHARS} chars]"
    return text


def parse_json_output(text: str, model: type[BaseModel]) -> BaseModel:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in the final answer.")
    return model.model_validate_json(cleaned[start:end + 1])


@dataclass
class AgentConfig:
    name: str
    system: str
    allowed_tools: set[str]
    output_model: type[BaseModel]
    validator: Callable[[BaseModel], list[str]] | None = None
    salvage: Callable[[BaseModel | None], BaseModel | None] | None = None
    max_turns: int = 12
    max_repairs: int = 2


async def run_agent(cfg: AgentConfig, provider: Provider, mcp_client, tool_specs: dict[str, ToolSpec],
                    user_parts: list[Part], tracer: Tracer) -> BaseModel:
    tools = [tool_specs[n] for n in sorted(cfg.allowed_tools) if n in tool_specs]
    chat = provider.new_chat(cfg.system, tools)
    parts: list[Part] | None = user_parts
    results: list[ToolResult] | None = None
    repairs = 0
    tracer.add(cfg.name, "agent_start", tools=[t.name for t in tools])

    for turn in range(cfg.max_turns):
        t0 = time.monotonic()
        resp: LLMTurn = await chat.send(parts, results)
        tracer.add(cfg.name, "llm_call", turn=turn, cached=resp.cached, latency_s=round(time.monotonic() - t0, 2),
                   tool_calls=[c.name for c in resp.tool_calls], finish_reason=resp.finish_reason, **resp.usage)
        parts, results = None, None

        if resp.tool_calls:
            results = []
            for call in resp.tool_calls:
                if call.name not in cfg.allowed_tools:
                    tracer.add(cfg.name, "security", event="blocked_tool_call", tool=call.name)
                    results.append(ToolResult(call.id, call.name,
                                              f"Tool '{call.name}' is not permitted for the {cfg.name} agent.", True))
                    continue
                t1 = time.monotonic()
                try:
                    mcp_res = await mcp_client.call_tool(call.name, call.args)
                    text, is_error = mcp_result_text(mcp_res), bool(mcp_res.is_error)
                except Exception as exc:
                    text, is_error = f"Tool call failed: {exc}", True
                tracer.add(cfg.name, "tool_call", tool=call.name, args=call.args, is_error=is_error,
                           latency_s=round(time.monotonic() - t1, 2), result_preview=text[:300])
                results.append(ToolResult(call.id, call.name, text, is_error))
            if turn >= cfg.max_turns - 3:  # close to max_turns
                for r in results:
                    r.content += "\n[ReportGuard: tool budget almost used up. Reply with your final JSON now.]"
            continue

        try:
            output = parse_json_output(resp.text, cfg.output_model)
            errors = cfg.validator(output) if cfg.validator else []
        except (ValueError, ValidationError) as exc:
            output, errors = None, [str(exc)[:1500]]
        if not errors:
            tracer.add(cfg.name, "agent_done", turns=turn + 1)
            return output
        repairs += 1
        tracer.add(cfg.name, "validation_error", errors=errors[:10])
        if repairs > cfg.max_repairs:
            return _salvage_or_fail(cfg, output, tracer, f"output still invalid after {cfg.max_repairs} repairs: {errors[:3]}")
        parts = [{"type": "text", "text": "Your final answer failed validation. Fix these problems and reply with "
                                          "ONLY the corrected JSON object:\n- " + "\n- ".join(errors[:15])}]
    return _salvage_or_fail(cfg, None, tracer, f"no valid final answer within {cfg.max_turns} turns")


def _salvage_or_fail(cfg: AgentConfig, output: BaseModel | None, tracer: Tracer, reason: str) -> BaseModel:
    """Use the valid part of the output if possible, otherwise fail."""
    if cfg.salvage:
        salvaged = cfg.salvage(output)
        if salvaged is not None:
            tracer.add(cfg.name, "salvaged", reason=reason)
            return salvaged
    raise AgentFailed(f"{cfg.name}: {reason}")
