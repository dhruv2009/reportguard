"""OpenAI-compatible chat completions provider (used with Ollama for offline runs).
Vision is off by default since most small local models can't read images.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from .base import Chat, LLMCache, LLMTurn, Part, Provider, RateLimiter, ToolCall, ToolResult, ToolSpec


class OpenAICompatProvider(Provider):
    name = "openai_compat"

    def __init__(self, base_url: str = "http://localhost:11434/v1", model: str = "qwen2.5:7b-instruct",
                 api_key: str = "ollama", cache: LLMCache | None = None, supports_vision: bool = False,
                 min_interval_s: float = 0.0, timeout_s: float = 600.0, http_client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.cache = cache or LLMCache("/tmp/rg_cache", mode="off")
        self.supports_vision = supports_vision
        self.limiter = RateLimiter(min_interval_s)
        self._timeout = timeout_s
        self._client = http_client
        self.calls = 0

    def new_chat(self, system: str, tools: list[ToolSpec]) -> Chat:
        return OpenAICompatChat(self, system, tools)

    async def generate(self, payload: dict) -> tuple[dict, bool]:
        key = self.cache.key(self.name, self.model, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached, True
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        for attempt in range(4):
            await self.limiter.wait()
            r = await self._client.post(f"{self.base_url}/chat/completions", json=payload,
                                        headers={"Authorization": f"Bearer {self.api_key}"})
            if r.status_code == 200:
                data = r.json()
                self.calls += 1
                self.cache.put(key, data)
                return data, False
            if r.status_code in (429, 500, 502, 503):
                await asyncio.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"Chat completions error {r.status_code}: {r.text[:800]}")
        raise RuntimeError("Chat completions endpoint kept failing")


class OpenAICompatChat(Chat):
    def __init__(self, provider: OpenAICompatProvider, system: str, tools: list[ToolSpec]):
        self.p = provider
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                         "parameters": t.input_schema or {"type": "object", "properties": {}}}}
                      for t in tools]

    async def send(self, parts: list[Part] | None = None, tool_results: list[ToolResult] | None = None) -> LLMTurn:
        for r in tool_results or []:
            self.messages.append({"role": "tool", "tool_call_id": r.call_id,
                                  "content": ("ERROR: " if r.is_error else "") + r.content})
        if parts:
            if self.p.supports_vision and any(p["type"] == "image" for p in parts):
                content = [{"type": "text", "text": p["text"]} if p["type"] == "text" else
                           {"type": "image_url", "image_url": {"url": f"data:{p['mime']};base64,{p['data_b64']}"}}
                           for p in parts]
            else:
                content = "\n\n".join(p["text"] for p in parts if p["type"] == "text")
            self.messages.append({"role": "user", "content": content})

        payload = {"model": self.p.model, "messages": self.messages, "temperature": 0}
        if self.tools:
            payload["tools"] = self.tools
        data, cached = await self.p.generate(payload)
        msg = data["choices"][0]["message"]
        clean = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            clean["tool_calls"] = msg["tool_calls"]
        self.messages.append(clean)

        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            raw = tc["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.get("id") or f"call_{i}", tc["function"]["name"], args))
        usage = data.get("usage", {})
        return LLMTurn(text=msg.get("content") or "", tool_calls=calls, cached=cached,
                       usage={"input_tokens": usage.get("prompt_tokens", 0),
                              "output_tokens": usage.get("completion_tokens", 0)},
                       finish_reason=data["choices"][0].get("finish_reason"))
