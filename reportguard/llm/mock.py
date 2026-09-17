"""Rule-based mock provider for tests and runs without an API key.

Calls the real MCP tools. The planner's first answer drops a figure and the
investigator calls a tool it isn't allowed to use, so the repair loop and the
allowlist check get exercised.
"""

from __future__ import annotations

import json
import re

from .base import Chat, LLMTurn, Part, Provider, ToolCall, ToolResult, ToolSpec

KEYWORDS = [("refund rate", "REFUND_RATE"), ("gross revenue", "GROSS_REVENUE"), ("refunds", "REFUNDS"),
            ("net revenue", "NET_REVENUE"), ("order value", "AOV"), ("orders", "ORDERS"),
            ("active customers", "ACTIVE_CUSTOMERS"), ("new customers", "NEW_CUSTOMERS")]
CATEGORIES = ["Electronics", "Home", "Apparel", "Beauty", "Sports"]


def _first_json(text: str):
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                return json.JSONDecoder().raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    return None


class MockProvider(Provider):
    name = "mock"
    model = "scripted-rules"
    supports_vision = False

    def new_chat(self, system: str, tools: list[ToolSpec]) -> Chat:
        role = re.match(r"You are the (\w+) agent", system).group(1)
        return MockChat(role)


class MockChat(Chat):
    def __init__(self, role: str):
        self.role, self.turn, self.memory = role, 0, {}

    def _reply(self, text: str = "", calls: list[ToolCall] | None = None) -> LLMTurn:
        self.turn += 1
        return LLMTurn(text=text, tool_calls=calls or [], usage={"input_tokens": 0, "output_tokens": 0})

    async def send(self, parts: list[Part] | None = None, tool_results: list[ToolResult] | None = None) -> LLMTurn:
        text = "\n".join(p["text"] for p in (parts or []) if p["type"] == "text")
        return await getattr(self, f"_{self.role}")(text, tool_results or [])

    async def _extractor(self, text, results):
        if self.turn == 0:
            self.memory["pdfs"] = sorted(set(re.findall(r"[\w\-]+\.pdf", text)))
            self.memory["pngs"] = sorted(set(re.findall(r"[\w\-]+\.png", text)))
            return self._reply(calls=[ToolCall(f"x{i}", "read_pdf_text", {"artifact_id": a})
                                      for i, a in enumerate(self.memory["pdfs"])])
        from ..pipeline import parse_display
        figures, notes, n = [], [], 0
        for r in results:
            doc = json.loads(r.content)
            for page in doc["pages"]:
                for table in page["tables"]:
                    for row in table[1:]:
                        parsed = parse_display(row[1])
                        if not parsed:
                            continue
                        value, unit, dec = parsed
                        if "($k)" in row[0].lower():
                            unit = "$K"
                        n += 1
                        figures.append({"figure_id": f"F{n}", "artifact_id": doc["artifact_id"],
                                        "location": f"page {page['page']} table", "label": row[0],
                                        "displayed_text": row[1], "value": value, "unit_label": unit,
                                        "display_decimals": dec, "period_label": None, "notes": None})
            for h in doc["hidden_text"]:
                notes.append({"artifact_id": doc["artifact_id"],
                              "description": f"Hidden instruction text on page {h['page']} (not followed)"})
        out = {"figures": figures, "security_notes": notes,
               "unreadable": [f"{p}: image-only artifact" for p in self.memory["pngs"]] + ["PDF chart data labels"]}
        return self._reply(json.dumps(out))

    async def _planner(self, text, results):
        if self.turn == 0:
            self.memory["figures"] = _first_json(text.split("untrusted documents):", 1)[1])
            self.memory["period"] = re.search(r"Report period: (\d{4}-\d{2})", text).group(1)
            return self._reply(calls=[ToolCall("p0", "list_metrics", {})])
        checks, skipped = [], []
        for f in self.memory["figures"]:
            label = f["label"].lower()
            cat = next((c for c in CATEGORIES if c.lower() in label), None)
            metric = "CATEGORY_REVENUE" if cat else next((m for k, m in KEYWORDS if k in label), None)
            if metric:
                checks.append({"check_id": f"C{len(checks) + 1}", "figure_id": f["figure_id"], "metric_id": metric,
                               "dimension_value": cat, "period": self.memory["period"], "reason": "label match"})
            else:
                skipped.append({"figure_id": f["figure_id"], "reason": "no matching metric"})
        if self.turn == 1 and checks:  # first answer is incomplete on purpose (tests repair)
            return self._reply(json.dumps({"checks": checks[:-1], "skipped": skipped}))
        return self._reply(json.dumps({"checks": checks, "skipped": skipped}))

    async def _investigator(self, text, results):
        if self.turn == 0:
            self.memory["failed"] = _first_json(text.split("Failed checks:", 1)[1])
            calls = [ToolCall(f"i{n}", "run_sql", {"query": "SELECT COUNT(*) AS n FROM orders WHERE status='completed'"})
                     for n, _ in enumerate(self.memory["failed"][:2])]
            calls.append(ToolCall("i_bad", "read_pdf_text", {"artifact_id": "anything.pdf"}))  # must be blocked
            return self._reply(calls=calls)
        findings = []
        for n, c in enumerate(self.memory["failed"], 1):
            ratio = (c["result"] or {}).get("ratio_reported_to_expected") or 0
            cause = "unit_mismatch" if ratio > 500 else "other"
            findings.append({"finding_id": f"R{n}", "check_id": c["check_id"], "root_cause": cause,
                             "explanation": f"Scripted rule: ratio {ratio}", "evidence_sql": [],
                             "evidence_summary": "", "confidence": "high" if cause != "other" else "low"})
        return self._reply(json.dumps({"findings": findings}))

    async def _critic(self, text, results):
        items = _first_json(text.split("Findings to review:", 1)[1])
        return self._reply(json.dumps({"verdicts": [
            {"finding_id": f["finding_id"], "verdict": "confirmed" if f["confidence"] == "high" else "uncertain",
             "reason": "scripted"} for f in items]}))

    async def _single(self, text, results):
        if self.turn == 0:
            pdfs = sorted(set(re.findall(r"[\w\-]+\.pdf", text)))
            return self._reply(calls=[ToolCall("s0", "read_pdf_text", {"artifact_id": pdfs[0]})])
        doc = json.loads(results[0].content)
        notes = [{"artifact_id": doc["artifact_id"], "description": "Hidden instruction text found"}
                 for _ in doc["hidden_text"][:1]]
        return self._reply(json.dumps({"issues": [], "checks_passed": 0, "security_notes": notes}))
