"""Builds a static demo page (docs/index.html) from recorded runs.

    python -m reportguard.cli site

Replays the recorded Gemini runs from the response cache (no API calls), marks the
wrong numbers on the report images, and writes one self-contained HTML file that
GitHub Pages can serve.
"""

from __future__ import annotations

import base64
import html
import io
import re

import pdfplumber
from PIL import Image, ImageDraw, ImageFont

from . import config
from .evals import score_run
from .pdf_tools import render_pdf_page

REPO_URL = "https://github.com/dhruv2009/reportguard"
COLAB_URL = "https://colab.research.google.com/github/dhruv2009/reportguard/blob/main/ReportGuard_Colab.ipynb"
RED = (200, 16, 46)
SCALE = 2.0  # PDF render scale (144 dpi)

CAUSES = {
    "timezone_boundary": "Month cut in the wrong timezone",
    "unit_mismatch": "Wrong unit label",
    "refunds_not_subtracted": "Refunds not subtracted",
    "join_fanout": "Join counted items instead of orders",
    "stale_data": "Data snapshot taken too early",
    "chart_table_mismatch": "Chart doesn't match the table",
    "wrong_period": "Shows the wrong month",
    "extraction_error": "Number misread",
    "other": "Other",
}
DASHBOARD_TILES = ["net revenue", "completed orders", "active customers", "avg order value"]


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _tag(draw: ImageDraw.ImageDraw, x: float, y: float, n: int, r: int = 17) -> None:
    draw.ellipse([x - r, y - r, x + r, y + r], fill=RED)
    font = _font(int(r * 1.15))
    text = str(n)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (box[2] - box[0]) / 2 - box[0], y - (box[3] - box[1]) / 2 - box[1]), text, fill="white", font=font)


def _png_b64(img: Image.Image, max_width: int = 1200) -> str:
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def marked_up_images(issues: list[dict], pack: str = "buggy", period: str = config.REPORT_PERIOD) -> dict:
    """Render the report pages and dashboard with numbered red marks on each issue."""
    pdf_path = config.REPORTS_DIR / f"mbr_{period}_{pack}.pdf"
    dash_path = config.REPORTS_DIR / f"dashboard_{period}_{pack}.png"
    pages = [Image.open(io.BytesIO(render_pdf_page(pdf_path, p, scale=SCALE))).convert("RGB") for p in (1, 2)]
    dash = Image.open(dash_path).convert("RGB")
    draws = [ImageDraw.Draw(p) for p in pages]
    dash_draw = ImageDraw.Draw(dash)

    with pdfplumber.open(pdf_path) as pdf:
        words = [p.extract_words() for p in pdf.pages]
        chart = pdf.pages[1].images[0] if pdf.pages[1].images else None
        # crop each page just below its content (ignore the footer line)
        bottoms = []
        for p in pdf.pages:
            body = [w["bottom"] for w in p.extract_words() if w["top"] < p.height - 80] + [i["bottom"] for i in p.images]
            bottoms.append((max(body) + 28) * SCALE if body else None)

    for n, issue in enumerate(issues, start=1):
        label = issue["label"].lower()
        if issue["artifact_id"].endswith(".png"):
            idx = next((i for i, t in enumerate(DASHBOARD_TILES) if t in label), None)
            if idx is None:
                continue
            w, h = dash.size
            x0, x1 = (0.04 + idx * 0.235) * w, (0.04 + idx * 0.235 + 0.215) * w
            y0, y1 = (1 - 0.84) * h, (1 - 0.62) * h
            dash_draw.rounded_rectangle([x0 - 6, y0 - 6, x1 + 6, y1 + 6], radius=12, outline=RED, width=6)
            _tag(dash_draw, x1 + 2, y0 - 2, n, r=20)
            continue
        if "chart" in label and chart:
            # the first bar's data label sits near the top-left of the embedded chart image
            cx = (chart["x0"] + 0.222 * (chart["x1"] - chart["x0"])) * SCALE
            cy = (chart["top"] + 0.13 * (chart["bottom"] - chart["top"])) * SCALE
            draws[1].ellipse([cx - 62, cy - 26, cx + 62, cy + 26], outline=RED, width=5)
            _tag(draws[1], cx + 72, cy - 22, n)
            continue
        target = (issue.get("displayed_text") or "").strip()
        page_hint = re.search(r"page (\d)", issue.get("location", "") or "")
        order = [int(page_hint.group(1)) - 1] if page_hint else [0, 1]
        for pi in order + [p for p in (0, 1) if p not in order]:
            hit = next((wd for wd in words[pi] if wd["text"] == target), None)
            if hit:
                x0, x1 = hit["x0"] * SCALE - 20, hit["x1"] * SCALE + 18
                y0, y1 = hit["top"] * SCALE - 11, hit["bottom"] * SCALE + 11
                draws[pi].ellipse([x0, y0, x1, y1], outline=RED, width=5)
                _tag(draws[pi], x1 + 30, (y0 + y1) / 2, n)
                break

    pages = [pg.crop((0, 0, pg.width, min(pg.height, round(b)))) if b else pg for pg, b in zip(pages, bottoms)]
    return {"page1": _png_b64(pages[0]), "page2": _png_b64(pages[1]), "dashboard": _png_b64(dash)}


def _count(n: int) -> str:
    words = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
    return words[n] if n < len(words) else str(n)


def _money(v, unit):
    if v is None:
        return "n/a"
    if unit == "usd":
        return f"${v:,.2f}"
    if unit == "percent":
        return f"{v:.2f}%"
    return f"{v:,.0f}"


def _fmt_score(v):
    if v is None or v == "":
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{v:.1f}"
    if isinstance(v, int) and v >= 1000:
        return f"{v / 1000:.0f}K" if v >= 10000 else f"{v / 1000:.1f}K"
    return str(v)


def render_page(buggy: dict, clean: dict, single: dict | None, scores: list[dict], images: dict) -> str:
    e = html.escape
    issues = buggy["issues"]
    n_checks = len(buggy["checks"])
    n_pass = sum(1 for c in buggy["checks"] if c["result"]["status"] == "PASS")
    multi = scores[0]

    findings = []
    for n, i in enumerate(issues, start=1):
        r = i.get("result") or {}
        delta = f"{r['delta_pct']:+.1f}%" if r.get("delta_pct") is not None else ""
        sql = "".join(f"<pre><code>{e(q)}</code></pre>" for q in i.get("evidence_sql") or [])
        where = "dashboard" if i["artifact_id"].endswith(".png") else "report"
        findings.append(f"""
      <li class="finding" id="f{n}">
        <span class="marker" aria-hidden="true">{n}</span>
        <div class="finding-body">
          <h3>{e(i['label'])} <span class="where">in the {where}</span></h3>
          <p class="numbers">Shown <strong>{e(i['displayed_text'])}</strong>, database says
            <strong>{e(_money(r.get('expected'), r.get('unit')))}</strong> <span class="delta">{e(delta)}</span></p>
          <p class="cause">{e(CAUSES.get(i['root_cause'], i['root_cause']))}</p>
          <p>{e(i.get('explanation', ''))}</p>
          {f'<p class="critic">Critic: {e(i["critic_reason"])}</p>' if i.get('critic_reason') else ''}
          {f'<details><summary>SQL that reproduces the wrong number</summary>{sql}</details>' if sql else ''}
        </div>
      </li>""")

    rows = [("Bugs detected", "bugs_detected"), ("Precision", "precision"), ("Root-cause accuracy", "root_cause_accuracy"),
            ("False positives", "false_positives"), ("Extraction recall", "extraction_recall"),
            ("Hidden instruction flagged", "injection_flagged"), ("LLM calls", "llm_calls"), ("Tokens in", "tokens_in"),
            ("Tokens out", "tokens_out")]
    heads = ["Multi-agent, buggy report", "Multi-agent, clean report"] + (["Single agent, buggy report"] if single else [])
    body = []
    for label, key in rows:
        cells = []
        for s in scores:
            v = f"{s['bugs_detected']}/{s['bugs_planted']}" if key == "bugs_detected" and s["bugs_planted"] else \
                ("n/a" if key == "bugs_detected" else _fmt_score(s.get(key)))
            cells.append(f"<td>{e(v)}</td>")
        body.append(f"<tr><th scope='row'>{e(label)}</th>{''.join(cells)}</tr>")

    note = ""
    if single:
        s = scores[2]
        note = (f"<p>The single agent, given every tool at once, caught {s['bugs_detected']} of {s['bugs_planted']} "
                f"with {s['llm_calls']} model calls against {multi['llm_calls']} for the multi-agent pipeline. On this "
                f"test the split doesn't buy accuracy. It buys containment: the only agent that reads the documents "
                f"can't query the database, and pass or fail is computed in code, so an instruction hidden in a report "
                f"can't change a result even if a model follows it.</p>")

    security = "".join(f"<p>{e(s['description'])}</p>" for s in buggy.get("security_notes", []))
    model = f"{buggy['provider']}:{buggy['model']}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReportGuard: checking report numbers against the database</title>
<meta name="description" content="A multi-agent system that verifies every number in a business report against SQL data and explains the mistakes.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #e9eef3; --paper: #ffffff; --ink: #14213d; --muted: #52607a; --rule: #c5cfdb;
  --red: #c8102e; --red-soft: #fbe3e7; --pass: #2f6f57; --code-bg: #f3f6f9;
  color-scheme: light;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #0f1b2b; --paper: #16263b; --ink: #e5ebf2; --muted: #9fb0c4; --rule: #2c3f57;
    --red: #ff5c6c; --red-soft: #3a1f2a; --pass: #74c7a4; --code-bg: #0c1624; color-scheme: dark;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #0f1b2b; --paper: #16263b; --ink: #e5ebf2; --muted: #9fb0c4; --rule: #2c3f57;
  --red: #ff5c6c; --red-soft: #3a1f2a; --pass: #74c7a4; --code-bg: #0c1624; color-scheme: dark;
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{ margin: 0; background: var(--bg); color: var(--ink);
  font: 400 1.0625rem/1.6 "IBM Plex Sans", "Segoe UI", system-ui, sans-serif; font-variant-numeric: tabular-nums; }}
a {{ color: inherit; text-decoration-color: var(--red); text-underline-offset: 3px; }}
a:focus-visible, summary:focus-visible {{ outline: 3px solid var(--red); outline-offset: 3px; border-radius: 2px; }}
.wrap {{ max-width: 1120px; margin: 0 auto; padding: 0 1.5rem; }}
header.top {{ display: flex; justify-content: space-between; align-items: center; gap: 1rem; padding: 1.25rem 0; }}
.brand {{ font-weight: 700; font-size: 1.125rem; }}
.links {{ display: flex; gap: 1.25rem; flex-wrap: wrap; font-weight: 500; }}
.hero {{ padding: 2.5rem 0 1.5rem; max-width: 46rem; }}
h1 {{ font-size: clamp(2.1rem, 5vw, 3.6rem); line-height: 1.05; letter-spacing: -0.02em; margin: 0 0 1.25rem; font-weight: 700; }}
.lede {{ font-size: 1.2rem; color: var(--muted); margin: 0; max-width: 40rem; }}
.sheets {{ display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 1.5rem; margin: 2rem 0 1rem; align-items: start; }}
.sheets .stack {{ display: grid; gap: 1.5rem; }}
figure {{ margin: 0; }}
figure img {{ display: block; width: 100%; height: auto; background: #fff; border: 1px solid var(--rule);
  box-shadow: 0 18px 40px -24px rgba(20, 33, 61, 0.45); }}
figcaption {{ font-size: 0.9rem; color: var(--muted); margin-top: 0.5rem; }}
section {{ padding: 3rem 0; border-top: 1px solid var(--rule); }}
h2 {{ font-size: 1.75rem; line-height: 1.2; margin: 0 0 1rem; letter-spacing: -0.01em; }}
.intro {{ max-width: 44rem; color: var(--muted); margin: 0 0 2rem; }}
ol.findings {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 0; }}
.finding {{ display: grid; grid-template-columns: 2.75rem minmax(0, 1fr); gap: 1rem; padding: 1.5rem 0; border-bottom: 1px solid var(--rule); }}
.marker {{ width: 2.25rem; height: 2.25rem; border-radius: 50%; background: var(--red); color: #fff;
  display: grid; place-items: center; font-weight: 700; }}
.finding h3 {{ margin: 0.1rem 0 0.35rem; font-size: 1.2rem; }}
.where {{ font-weight: 400; color: var(--muted); font-size: 1rem; }}
.finding p {{ margin: 0.35rem 0; max-width: 46rem; }}
.numbers strong {{ font-weight: 600; }}
.delta {{ color: var(--red); font-weight: 600; margin-left: 0.25rem; }}
.cause {{ display: inline-block; background: var(--red-soft); color: var(--red); font-weight: 600; padding: 0.1rem 0.6rem; border-radius: 4px; }}
.critic {{ color: var(--muted); font-size: 0.95rem; }}
details {{ margin-top: 0.5rem; }}
summary {{ cursor: pointer; font-weight: 500; }}
pre {{ background: var(--code-bg); border: 1px solid var(--rule); padding: 0.9rem 1rem; overflow-x: auto; margin: 0.6rem 0 0;
  font: 400 0.85rem/1.5 "IBM Plex Mono", ui-monospace, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }}
.callout {{ background: var(--paper); border-left: 4px solid var(--red); padding: 1.1rem 1.25rem; max-width: 48rem; }}
.callout p {{ margin: 0.3rem 0; }}
ol.steps {{ padding-left: 1.4rem; margin: 0; max-width: 48rem; }}
ol.steps li {{ padding: 0.5rem 0; }}
.tools {{ color: var(--muted); display: block; font-size: 0.95rem; }}
.table-scroll {{ overflow-x: auto; max-width: 100%; }}
table {{ border-collapse: collapse; width: 100%; min-width: 34rem; background: var(--paper); }}
th, td {{ text-align: left; padding: 0.7rem 0.9rem; border-bottom: 1px solid var(--rule); }}
thead th {{ font-weight: 600; font-size: 0.95rem; border-bottom: 2px solid var(--ink); }}
tbody th {{ font-weight: 500; }}
td {{ font-variant-numeric: tabular-nums; }}
.try pre {{ max-width: 48rem; }}
footer {{ padding: 2rem 0 3rem; color: var(--muted); font-size: 0.9rem; border-top: 1px solid var(--rule); }}
@media (max-width: 820px) {{
  .sheets {{ grid-template-columns: minmax(0, 1fr); }}
  .finding {{ grid-template-columns: 2.25rem minmax(0, 1fr); gap: 0.75rem; }}
  .marker {{ width: 1.9rem; height: 1.9rem; font-size: 0.9rem; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <span class="brand">ReportGuard</span>
    <nav class="links"><a href="{REPO_URL}">Code on GitHub</a><a href="{COLAB_URL}">Open in Colab</a></nav>
  </header>

  <div class="hero">
    <h1>{_count(len(issues))} numbers in this report are wrong.</h1>
    <p class="lede">ReportGuard read the monthly business review and its dashboard, checked all {n_checks} numbers
      against the database, and marked the {len(issues)} that don't match. I planted every mistake on purpose, using
      the kind of SQL and labeling errors that happen in real reporting pipelines.</p>
  </div>

  <div class="sheets">
    <figure>
      <img src="data:image/png;base64,{images['page1']}" alt="Page 1 of the monthly business review with the wrong key metrics circled in red and numbered">
      <figcaption>Monthly business review, page 1</figcaption>
    </figure>
    <div class="stack">
      <figure>
        <img src="data:image/png;base64,{images['dashboard']}" alt="Sales dashboard with the active customers tile outlined in red">
        <figcaption>Sales dashboard</figcaption>
      </figure>
      <figure>
        <img src="data:image/png;base64,{images['page2']}" alt="Page 2 of the review with the Electronics chart label circled in red">
        <figcaption>Monthly business review, page 2</figcaption>
      </figure>
    </div>
  </div>

  <section aria-labelledby="findings">
    <h2 id="findings">What it found</h2>
    <p class="intro">{n_pass} of {n_checks} numbers matched. For each one that didn't, an investigator agent worked out
      why and reproduced the wrong value with SQL, and a critic agent checked the explanation before it made the report.
      The explanations below are the agents' own words from the recorded run.</p>
    <ol class="findings">{''.join(findings)}
    </ol>
  </section>

  <section aria-labelledby="security">
    <h2 id="security">The hidden instruction</h2>
    <p class="intro">Page 1 also carries a line of white, 1-point text telling automated reviewers to pass everything.
      A person reading the PDF can't see it. ReportGuard reported it and kept checking.</p>
    <div class="callout">{security or '<p>No hidden text was reported in this run.</p>'}</div>
  </section>

  <section aria-labelledby="how">
    <h2 id="how">How it works</h2>
    <p class="intro">Every tool comes from an MCP server, and each agent only gets the tools its job needs.</p>
    <ol class="steps">
      <li><strong>Extractor</strong> reads the PDF text and page images and lists every number with its unit.
        <span class="tools">Tools: list_artifacts, read_pdf_text. No database access.</span></li>
      <li><strong>Planner</strong> maps each number to a metric definition and a period. Code validates the plan and sends it back if anything is missing.
        <span class="tools">Tools: list_metrics, get_metric_definition</span></li>
      <li><strong>Checks run in code.</strong> Each metric is recomputed with reviewed SQL and compared within a rounding tolerance. No model decides pass or fail.
        <span class="tools">Tool: check_metric</span></li>
      <li><strong>Investigator</strong> takes the failures and finds the cause with read-only SQL.
        <span class="tools">Tools: check_metric, run_sql, get_schema, get_metric_definition. Never sees document text.</span></li>
      <li><strong>Critic</strong> tests each explanation and can reject it.
        <span class="tools">Tools: check_metric, run_sql, get_metric_definition</span></li>
    </ol>
  </section>

  <section aria-labelledby="results">
    <h2 id="results">Measured results</h2>
    <p class="intro">Scored against answer keys the agents can't reach. The clean report has no mistakes, so any issue
      raised there would be a false alarm.</p>
    <div class="table-scroll">
      <table>
        <thead><tr><th scope="col">Metric</th>{''.join(f'<th scope="col">{e(h)}</th>' for h in heads)}</tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>
    </div>
    {note}
  </section>

  <section aria-labelledby="try" class="try">
    <h2 id="try">Run it yourself</h2>
    <p class="intro">Open the notebook in Colab, or run it locally. Tests and a no-key mock run work out of the box;
      the agent runs need a Gemini API key.</p>
    <pre><code>pip install -r requirements.txt
python -m reportguard.cli setup
python -m pytest
python -m reportguard.cli run --pack buggy</code></pre>
  </section>

  <footer>Recorded run with <code>{e(model)}</code>, replayed from the response cache.
    <a href="{REPO_URL}">Source code</a></footer>
</div>
</body>
</html>
"""


async def build_demo_page(out_path=None, with_single: bool = True):
    """Replay the recorded runs (no API calls) and write docs/index.html."""
    from .llm import make_provider
    from .pipeline import run_multi_agent, run_single_agent

    def replay():
        return make_provider("gemini", cache_mode="replay")

    buggy = (await run_multi_agent(replay(), "buggy", verbose=False)).to_json()
    clean = (await run_multi_agent(replay(), "clean", verbose=False)).to_json()
    single = (await run_single_agent(replay(), "buggy", verbose=False)).to_json() if with_single else None
    for name, run in (("buggy", buggy), ("clean", clean), ("single", single)):
        if run and run.get("error"):
            raise RuntimeError(f"Replay of the {name} run failed: {run['error']}. Run the notebook's Gemini cells once "
                               f"(record mode) before building the page.")
    scores = [score_run(buggy), score_run(clean)] + ([score_run(single)] if single else [])
    images = marked_up_images(buggy["issues"], "buggy", buggy["period"])
    page = render_page(buggy, clean, single, scores, images)
    out_path = out_path or config.PROJECT_ROOT / "docs" / "index.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path
