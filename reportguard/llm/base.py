"""Common provider interface, rate limiter and response cache.

Cache modes: off, record (use cached response if there is one, otherwise call and save),
replay (cache only, never calls the API).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class LLMTurn:
    text: str
    tool_calls: list[ToolCall]
    usage: dict = field(default_factory=dict)
    cached: bool = False
    finish_reason: str | None = None


# A user part is {"type": "text", "text": ...} or {"type": "image", "mime": "image/png", "data_b64": ...}
Part = dict


class Chat(ABC):
    @abstractmethod
    async def send(self, parts: list[Part] | None = None, tool_results: list[ToolResult] | None = None) -> LLMTurn:
        """Append user parts and/or tool results, get the model's next turn."""


class Provider(ABC):
    name: str = "base"
    model: str = ""
    supports_vision: bool = True

    @abstractmethod
    def new_chat(self, system: str, tools: list[ToolSpec]) -> Chat: ...

    async def prepare(self) -> None:
        """Optional async setup (e.g. choose a model)."""


class QuotaExhausted(RuntimeError):
    pass


class CacheMiss(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, min_interval_s: float = 0.0):
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        async with self._lock:
            delay = self._last + self.min_interval_s - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class LLMCache:
    """mode: 'off' | 'record' (read if present, else call and save) | 'replay' (never call the API)."""

    def __init__(self, directory: str | Path, mode: str = "record"):
        if mode not in {"off", "record", "replay"}:
            raise ValueError("cache mode must be off, record or replay")
        self.dir = Path(directory)
        self.mode = mode
        self.hits = self.misses = 0
        if mode != "off":
            self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(provider: str, model: str, payload: dict) -> str:
        blob = json.dumps({"p": provider, "m": model, "payload": payload}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        if self.mode == "off":
            return None
        path = self.dir / f"{key}.json"
        if path.exists():
            self.hits += 1
            return json.loads(path.read_text(encoding="utf-8"))
        self.misses += 1
        if self.mode == "replay":
            raise CacheMiss("Replay mode: this request was never recorded. Run once in 'record' mode "
                            "(same data, same prompts) before replaying.")
        return None

    def put(self, key: str, response: dict) -> None:
        if self.mode != "off":
            (self.dir / f"{key}.json").write_text(json.dumps(response), encoding="utf-8")
