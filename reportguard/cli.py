"""    python -m reportguard.cli setup
    python -m reportguard.cli run --pack buggy [--mode single] [--provider mock] [--cache replay]
    python -m reportguard.cli eval [--with-single]
    python -m reportguard.cli site                  (demo page from recorded runs, no API calls)
"""

from __future__ import annotations

import argparse
import asyncio
import json

from . import config


def setup() -> dict:
    from .data_gen import build_warehouse
    from .reports import generate_packs
    counts = build_warehouse(config.DB_PATH)
    packs = generate_packs(config.DB_PATH, config.REPORTS_DIR, config.MANIFEST_DIR, config.REPORT_PERIOD)
    return {"warehouse": counts, "packs": packs}


async def run(provider_name: str, cache: str, mode: str, pack: str, min_interval: float | None = None):
    from .llm import make_provider
    from .pipeline import run_multi_agent, run_single_agent, save_run
    kwargs = {"min_interval_s": min_interval} if (min_interval is not None and provider_name != "mock") else {}
    provider = make_provider(provider_name, cache, **kwargs)
    runner = run_multi_agent if mode == "multi" else run_single_agent
    result = await runner(provider, pack=pack)
    path = save_run(result)
    return result, path


async def evaluate(provider_name: str, cache: str, include_single: bool, min_interval: float | None = None):
    from .evals import score_run, scorecard_markdown
    scores = []
    plan = [("multi", "buggy"), ("multi", "clean")] + ([("single", "buggy")] if include_single else [])
    for mode, pack in plan:
        print(f"\n=== {mode} agent on {pack} pack ===")
        result, path = await run(provider_name, cache, mode, pack, min_interval)
        scores.append(score_run(result))
        print(f"saved {path}")
    card = scorecard_markdown(scores)
    (config.RUNS_DIR / "scorecard.md").write_text(card, encoding="utf-8")
    (config.RUNS_DIR / "scores.json").write_text(json.dumps(scores, indent=1), encoding="utf-8")
    return scores, card


def main() -> None:
    p = argparse.ArgumentParser(prog="reportguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("site")
    for name in ("run", "eval"):
        s = sub.add_parser(name)
        s.add_argument("--provider", default="gemini", choices=["gemini", "ollama", "claude", "mock"])
        s.add_argument("--cache", default="record", choices=["off", "record", "replay"])
        s.add_argument("--min-interval", type=float, default=None, help="seconds between LLM calls")
        if name == "run":
            s.add_argument("--mode", default="multi", choices=["multi", "single"])
            s.add_argument("--pack", default="buggy", choices=["buggy", "clean"])
        else:
            s.add_argument("--with-single", action="store_true", help="also run the single-agent baseline")
    a = p.parse_args()
    if a.cmd == "setup":
        print(json.dumps(setup(), indent=2))
    elif a.cmd == "site":
        from .site import build_demo_page
        print("Wrote", asyncio.run(build_demo_page()))
    elif a.cmd == "run":
        result, path = asyncio.run(run(a.provider, a.cache, a.mode, a.pack, a.min_interval))
        print((path / "qa_report.md").read_text(encoding="utf-8"))
    else:
        _, card = asyncio.run(evaluate(a.provider, a.cache, a.with_single, a.min_interval))
        print(card)


if __name__ == "__main__":
    main()
