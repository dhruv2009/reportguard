"""LLM providers."""

from .. import config
from .base import CacheMiss, LLMCache, Provider, QuotaExhausted


def make_provider(name: str = "gemini", cache_mode: str = "record", **kwargs) -> Provider:
    """name: 'gemini' (free tier, default) | 'ollama' (offline backup) | 'claude' (paid key) | 'mock' (no AI)."""
    cache = LLMCache(config.CACHE_DIR / name, mode=cache_mode)
    if name == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider(cache=cache, **kwargs)
    if name == "ollama":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(cache=cache, **kwargs)
    if name == "claude":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(cache=cache, **kwargs)
    if name == "mock":
        from .mock import MockProvider
        return MockProvider()
    raise ValueError(f"Unknown provider {name!r}")


__all__ = ["make_provider", "LLMCache", "Provider", "CacheMiss", "QuotaExhausted"]
