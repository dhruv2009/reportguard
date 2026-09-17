"""Gemini provider using the REST API directly.

- picks the newest gemini-X.Y-flash model unless GEMINI_MODEL is set
- model turns are appended unchanged so thought signatures are sent back
- 429: waits for retryDelay, raises QuotaExhausted on daily limits
- timeouts and dropped connections are retried
- gemini-3+ models get thinkingLevel=low by default (GEMINI_THINKING_LEVEL, empty to disable)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re

import httpx

from .base import Chat, LLMCache, LLMTurn, Part, Provider, QuotaExhausted, RateLimiter, ToolCall, ToolResult, ToolSpec

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
EXCLUDE = re.compile(r"lite|live|tts|image|audio|embed|omni|translate|robot|computer|native|dialog|exp", re.I)
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]


def to_gemini_schema(schema: dict) -> dict:
    """Convert a JSON Schema (as produced by MCP/pydantic) into Gemini's OpenAPI-subset schema."""
    if "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        out = to_gemini_schema(options[0]) if options else {"type": "string"}
        if len(options) < len(schema["anyOf"]):
            out["nullable"] = True
        desc = schema.get("description") or schema.get("title")
        if desc:
            out["description"] = desc
        return out
    out: dict = {}
    typ = schema.get("type")
    if isinstance(typ, list):
        non_null = [t for t in typ if t != "null"]
        typ = non_null[0] if non_null else "string"
        if len(non_null) < len(schema["type"]):
            out["nullable"] = True
    if typ:
        out["type"] = typ
    desc = schema.get("description") or schema.get("title")
    if "default" in schema and schema["default"] is not None:
        desc = f"{desc or ''} (default: {schema['default']})".strip()
    if desc:
        out["description"] = desc
    for key in ("enum", "nullable", "minimum", "maximum"):
        if key in schema:
            out[key] = schema[key]
    if schema.get("format") in ("date-time", "enum"):
        out["format"] = schema["format"]
    if "properties" in schema:
        out["properties"] = {k: to_gemini_schema(v) for k, v in schema["properties"].items()}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    if "items" in schema:
        out["items"] = to_gemini_schema(schema["items"])
    return out


def _version_key(name: str) -> tuple:
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?-flash(.*)$", name)
    if not m:
        return (-1, -1, 0)
    suffix = m.group(3)
    stability = 2 if suffix == "" else 1 if "latest" in suffix else 0
    return (int(m.group(1)), int(m.group(2) or 0), stability)


class GeminiProvider(Provider):
    name = "gemini"
    supports_vision = True

    def __init__(self, api_key: str | None = None, model: str | None = None, cache: LLMCache | None = None,
                 min_interval_s: float = 6.5, max_retries: int = 6, http_client: httpx.AsyncClient | None = None,
                 timeout_s: float = 360.0, thinking_level: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL", "")
        self.cache = cache or LLMCache("/tmp/rg_cache", mode="off")
        self.limiter = RateLimiter(min_interval_s)
        self.max_retries = max_retries
        self._client = http_client
        self._timeout = timeout_s
        self.thinking_level = os.environ.get("GEMINI_THINKING_LEVEL", "low") if thinking_level is None else thinking_level
        self._candidates: list[str] = []
        self._model_locked = False
        self.calls = 0

    def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set. Create a free key in Google AI Studio.")
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    async def prepare(self) -> None:
        model_file = self.cache.dir / "gemini_model.txt" if self.cache.mode != "off" else None
        if self.model:
            self._candidates = [self.model]
        elif self.cache.mode == "replay" and model_file and model_file.exists():
            self.model = model_file.read_text(encoding="utf-8").strip()
            self._candidates = [self.model]
        else:
            self._candidates = await self._list_flash_models() or FALLBACK_MODELS
            self.model = self._candidates[0]
        if model_file and self.cache.mode == "record":
            model_file.write_text(self.model, encoding="utf-8")

    async def _list_flash_models(self) -> list[str]:
        names, token = [], None
        try:
            for _ in range(10):
                params = {"pageSize": 1000, **({"pageToken": token} if token else {})}
                r = await self._client_or_new().get(f"{API_BASE}/models", headers=self._headers(), params=params)
                if r.status_code != 200:
                    return []
                data = r.json()
                for m in data.get("models", []):
                    name = m.get("name", "").removeprefix("models/")
                    if ("generateContent" in m.get("supportedGenerationMethods", [])
                            and "flash" in name and not EXCLUDE.search(name) and _version_key(name)[0] >= 2):
                        names.append(name)
                token = data.get("nextPageToken")
                if not token:
                    break
        except httpx.HTTPError:
            return []
        return sorted(set(names), key=_version_key, reverse=True)

    def new_chat(self, system: str, tools: list[ToolSpec]) -> Chat:
        return GeminiChat(self, system, tools)

    async def generate(self, payload: dict) -> tuple[dict, bool]:
        key = self.cache.key(self.name, self.model, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached, True
        attempt = 0
        while True:
            await self.limiter.wait()
            try:
                r = await self._client_or_new().post(f"{API_BASE}/models/{self.model}:generateContent",
                                                     headers=self._headers(), content=json.dumps(self._with_config(payload)))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise RuntimeError(f"Gemini request failed after {self.max_retries} retries: {exc!r}") from exc
                await asyncio.sleep(min(60, 2 ** attempt * 3) + random.uniform(0, 1.5))
                continue
            if r.status_code == 200:
                data = r.json()
                self.calls += 1
                self._model_locked = True
                self.cache.put(self.cache.key(self.name, self.model, payload), data)
                return data, False
            body = r.text[:2000]
            if r.status_code == 400 and self.thinking_level and "thinking" in body.lower():
                self.thinking_level = ""  # model doesn't support thinkingLevel
                continue
            if r.status_code in (404, 429) and not self._model_locked and self._switch_model_if_unavailable(body, r.status_code):
                key = self.cache.key(self.name, self.model, payload)
                continue
            if r.status_code == 429:
                if "PerDay" in body and "PerMinute" not in body:
                    raise QuotaExhausted("Gemini daily free-tier quota reached. Wait for the daily reset, "
                                         "or replay a recorded run with cache mode 'replay'.")
                delay = self._retry_delay(body) or min(60, 2 ** attempt * 5)
            elif r.status_code in (500, 502, 503, 504):
                delay = min(60, 2 ** attempt * 3)
            else:
                raise RuntimeError(f"Gemini API error {r.status_code} for model {self.model}: {body}")
            attempt += 1
            if attempt > self.max_retries:
                raise RuntimeError(f"Gemini API still failing after {self.max_retries} retries: {body[:500]}")
            await asyncio.sleep(delay + random.uniform(0, 1.5))

    def _with_config(self, payload: dict) -> dict:
        if self.thinking_level and _version_key(self.model)[0] >= 3:
            return {**payload, "generationConfig": {"thinkingConfig": {"thinkingLevel": self.thinking_level}}}
        return payload

    def _switch_model_if_unavailable(self, body: str, status: int) -> bool:
        unavailable = status == 404 or "limit: 0" in body
        if unavailable and self.model in self._candidates:
            idx = self._candidates.index(self.model)
            if idx + 1 < len(self._candidates):
                self.model = self._candidates[idx + 1]
                if self.cache.mode == "record":
                    (self.cache.dir / "gemini_model.txt").write_text(self.model, encoding="utf-8")
                return True
        return False

    @staticmethod
    def _retry_delay(body: str) -> float | None:
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
        return float(m.group(1)) if m else None


def _as_object(text: str) -> dict | list | str:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


class GeminiChat(Chat):
    def __init__(self, provider: GeminiProvider, system: str, tools: list[ToolSpec]):
        self.p = provider
        self.system = system
        self.contents: list[dict] = []
        self.decls = []
        for t in tools:
            decl = {"name": t.name, "description": (t.description or "")[:1024]}
            params = to_gemini_schema(t.input_schema or {})
            if params.get("properties"):
                decl["parameters"] = params
            self.decls.append(decl)
        self._n = 0

    async def send(self, parts: list[Part] | None = None, tool_results: list[ToolResult] | None = None) -> LLMTurn:
        new_parts: list[dict] = []
        for r in tool_results or []:
            payload = _as_object(r.content)
            fr = {"name": r.name, "response": {"error": payload} if r.is_error else {"result": payload}}
            if r.call_id and not r.call_id.startswith("rg_"):
                fr["id"] = r.call_id
            new_parts.append({"functionResponse": fr})
        for part in parts or []:
            if part["type"] == "text":
                new_parts.append({"text": part["text"]})
            elif part["type"] == "image":
                new_parts.append({"inlineData": {"mimeType": part["mime"], "data": part["data_b64"]}})
        if new_parts:
            self.contents.append({"role": "user", "parts": new_parts})

        payload: dict = {"systemInstruction": {"parts": [{"text": self.system}]}, "contents": self.contents}
        if self.decls:
            payload["tools"] = [{"functionDeclarations": self.decls}]
        data, cached = await self.p.generate(payload)

        if data.get("promptFeedback", {}).get("blockReason"):
            raise RuntimeError(f"Gemini blocked the prompt: {data['promptFeedback']}")
        candidate = (data.get("candidates") or [{}])[0]
        content = candidate.get("content") or {}
        model_parts = content.get("parts") or [{"text": "(no output)"}]
        self.contents.append({"role": "model", "parts": model_parts})  # unchanged, keeps thoughtSignature

        text = "".join(p.get("text", "") for p in model_parts if "text" in p and not p.get("thought"))
        calls = []
        for p in model_parts:
            if "functionCall" in p:
                self._n += 1
                fc = p["functionCall"]
                calls.append(ToolCall(fc.get("id") or f"rg_{self._n}", fc["name"], fc.get("args") or {}))
        um = data.get("usageMetadata", {})
        usage = {"input_tokens": um.get("promptTokenCount", 0),
                 "output_tokens": um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)}
        return LLMTurn(text=text, tool_calls=calls, usage=usage, cached=cached,
                       finish_reason=candidate.get("finishReason"))
