"""Anthropic Messages API provider."""

from __future__ import annotations

import asyncio
import os

import httpx

from .base import Chat, LLMCache, LLMTurn, Part, Provider, RateLimiter, ToolCall, ToolResult, ToolSpec

API_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(Provider):
    name = "claude"
    supports_vision = True

    def __init__(self, api_key: str | None = None, model: str | None = None, cache: LLMCache | None = None,
                 max_tokens: int = 4096, min_interval_s: float = 0.0, http_client: httpx.AsyncClient | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
        self.cache = cache or LLMCache("/tmp/rg_cache", mode="off")
        self.max_tokens = max_tokens
        self.limiter = RateLimiter(min_interval_s)
        self._client = http_client
        self.calls = 0

    def new_chat(self, system: str, tools: list[ToolSpec]) -> Chat:
        return AnthropicChat(self, system, tools)

    async def generate(self, payload: dict) -> tuple[dict, bool]:
        key = self.cache.key(self.name, self.model, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached, True
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = self._client or httpx.AsyncClient(timeout=300)
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        for attempt in range(6):
            await self.limiter.wait()
            r = await self._client.post(API_URL, json=payload, headers=headers)
            if r.status_code == 200:
                data = r.json()
                self.calls += 1
                self.cache.put(key, data)
                return data, False
            if r.status_code in (429, 500, 502, 503, 529):
                await asyncio.sleep(float(r.headers.get("retry-after", 2 ** attempt * 2)))
                continue
            raise RuntimeError(f"Anthropic API error {r.status_code}: {r.text[:800]}")
        raise RuntimeError("Anthropic API kept failing")


class AnthropicChat(Chat):
    def __init__(self, provider: AnthropicProvider, system: str, tools: list[ToolSpec]):
        self.p, self.system = provider, system
        self.messages: list[dict] = []
        self.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in tools]

    async def send(self, parts: list[Part] | None = None, tool_results: list[ToolResult] | None = None) -> LLMTurn:
        content: list[dict] = [{"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
                                "is_error": r.is_error} for r in tool_results or []]
        for part in parts or []:
            if part["type"] == "text":
                content.append({"type": "text", "text": part["text"]})
            else:
                content.append({"type": "image", "source": {"type": "base64", "media_type": part["mime"],
                                                            "data": part["data_b64"]}})
        if content:
            self.messages.append({"role": "user", "content": content})
        payload = {"model": self.p.model, "max_tokens": self.p.max_tokens, "system": self.system,
                   "messages": self.messages}
        if self.tools:
            payload["tools"] = self.tools
        data, cached = await self.p.generate(payload)
        blocks = data.get("content") or [{"type": "text", "text": "(no output)"}]
        self.messages.append({"role": "assistant", "content": blocks})
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in blocks if b.get("type") == "tool_use"]
        usage = data.get("usage", {})
        return LLMTurn(text=text, tool_calls=calls, cached=cached, finish_reason=data.get("stop_reason"),
                       usage={"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)})
