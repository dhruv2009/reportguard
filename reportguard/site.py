"""Builds the demo page (docs/index.html) and the README results section from recorded runs.

    python -m reportguard.cli site

Everything is replayed from the response cache, so nothing is sent to Gemini:
  retail report      multi-agent on the buggy and clean packs, plus the single agent if recorded
  health dashboard   the code-only model check and the agents reading the rendered tabs
The README results block is written from the same replayed runs, so the two always agree.
"""

from __future__ import annotations

import base64
import html
import io
import os
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
RESULTS_START, RESULTS_END = "<!-- results:start -->", "<!-- results:end -->"

CAUSES = {
    "timezone_boundary": "Month cut in the wrong timezone",
    "unit_mismatch": "Wrong unit label",
    "refunds_not_subtracted": "Refunds not subtracted",
    "join_fanout": "Rows counted more than once",
    "stale_data": "Data snapshot taken too early",
    "chart_table_mismatch": "Chart doesn't match the table",
    "wrong_period": "Shows the wrong month",
    "wrong_denominator": "Wrong denominator",
    "missing_filter": "Filter missing",
    "definition_drift": "Metric definition drifted",
    "extraction_error": "Number misread",
    "other": "Cause not identified",
}
DASHBOARD_TILES = ["net revenue", "completed orders", "active customers", "avg order value"]


# ---------------------------------------------------------------- images
def _font(size: int):
    """Bold font for the marker digits. matplotlib ships DejaVu, so this works on any machine."""
    candidates = []
    try:
        import matplotlib
        candidates.append(os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf"))
    except ImportError:
        pass
    candidates += ["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _tag(draw: ImageDraw.ImageDraw, x: float, y: float, n: int, r: int = 18) -> None:
    draw.ellipse([x - r, y - r, x + r, y + r], fill=RED)
    font = _font(int(r * 1.2))
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
            _tag(dash_draw, x1 + 2, y0 - 2, n, r=22)
            continue
        if "chart" in label and chart:
            # the first bar's data label sits near the top-left of the embedded chart image
            cx = (chart["x0"] + 0.222 * (chart["x1"] - chart["x0"])) * SCALE
            cy = (chart["top"] + 0.13 * (chart["bottom"] - chart["top"])) * SCALE
            draws[1].ellipse([cx - 62, cy - 26, cx + 62, cy + 26], outline=RED, width=5)
            _tag(draws[1], cx + 74, cy - 22, n)
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
                _tag(draws[pi], x1 + 32, (y0 + y1) / 2, n)
                break

    pages = [pg.crop((0, 0, pg.width, min(pg.height, round(b)))) if b else pg for pg, b in zip(pages, bottoms)]
    return {"page1": _png_b64(pages[0]), "page2": _png_b64(pages[1]), "dashboard": _png_b64(dash)}


# ---------------------------------------------------------------- formatting
def _count(n: int) -> str:
    words = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
    return words[n] if n < len(words) else str(n)


def _num(v, unit) -> str:
    if v is None:
        return "n/a"
    if unit == "usd":
        return f"${v:,.2f}"
    if unit == "percent":
        return f"{v:.2f}%"
    if float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _delta(result: dict) -> str:
    """Percent difference, or 'N× too large/small' when the number is off by an order of magnitude."""
    ratio = (result or {}).get("ratio_reported_to_expected")
    if ratio and ratio >= 10:
        return f"{ratio:,.0f}× too large"
    if ratio and 0 < ratio <= 0.1:
        return f"{1 / ratio:,.0f}× too small"
    pct = (result or {}).get("delta_pct")
    return f"{pct:+.1f}%" if pct is not None else ""


def _fmt_score(v) -> str:
    if v is None or v == "":
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{v:.1f}"
    if isinstance(v, int) and v >= 1000:
        return f"{v / 1000:.0f}K" if v >= 10000 else f"{v / 1000:.1f}K"
    return str(v)


def _verdict_counts(run: dict) -> tuple[int, int]:
    issues = run.get("issues", [])
    return sum(1 for i in issues if i.get("verdict") == "confirmed"), len(issues)


def _uncertain_phrase(ok: int, total: int) -> str:
    left = total - ok
    return "the other one is marked uncertain" if left == 1 else f"the other {left} are marked uncertain"


def _second_look_line(run: dict) -> str:
    second = run.get("second_look") or []
    if not second:
        return ""
    ok = sum(1 for x in second if x["verdict"] == "confirmed")
    if len(second) == 1:
        return (" One explanation the critic couldn't verify at first got a second investigation and "
                + ("was then confirmed." if ok else "stayed uncertain."))
    return (f" {len(second)} explanations the critic couldn't verify at first got a second investigation; "
            f"{ok} of them {'was' if ok == 1 else 'were'} then confirmed.")


def _badge(verdict: str) -> str:
    label = {"confirmed": "Confirmed", "uncertain": "Uncertain", "unreviewed": "Not reviewed"}.get(verdict, verdict)
    return f'<span class="verdict {html.escape(verdict)}">{html.escape(label)}</span>'


# ---------------------------------------------------------------- shared result rows
def retail_rows(data: dict) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Column headers and rows for the retail results table (page and README use the same rows)."""
    runs, scores = data["runs"], data["scores"]
    heads = ["Multi-agent, buggy report", "Multi-agent, clean report"] + \
        (["Single agent, buggy report"] if len(runs) > 2 else [])

    def cell(i: int, key: str) -> str:
        s, r = scores[i], runs[i]
        if key == "bugs":
            return f"{s['bugs_detected']}/{s['bugs_planted']}" if s["bugs_planted"] else "n/a (no bugs)"
        if key == "confirmed":
            if r["mode"] == "single_agent":
                return "n/a (no critic)"
            ok, total = _verdict_counts(r)
            return f"{ok} of {total}" if total else "n/a (nothing flagged)"
        if key in ("precision", "root_cause_accuracy", "injection_flagged") and not s["bugs_planted"]:
            return "n/a"
        if key == "tokens":
            return f"{_fmt_score(s.get('tokens_in'))} / {_fmt_score(s.get('tokens_out'))}"
        return _fmt_score(s.get(key))

    spec = [("Bugs detected", "bugs"), ("Precision", "precision"), ("Root-cause accuracy", "root_cause_accuracy"),
            ("Explanations confirmed by the critic", "confirmed"), ("False positives", "false_positives"),
            ("Extraction recall", "extraction_recall"), ("Hidden instruction flagged", "injection_flagged"),
            ("LLM calls", "llm_calls"), ("Tokens in / out", "tokens")]
    return heads, [(label, [cell(i, key) for i in range(len(runs))]) for label, key in spec]


def retail_note(data: dict) -> str:
    runs, scores = data["runs"], data["scores"]
    if len(runs) < 3:
        return ""
    s, m = scores[2], scores[0]
    return (f"The single agent, given every tool at once, caught {s['bugs_detected']} of {s['bugs_planted']} "
            f"with {s['llm_calls']} model calls against {m['llm_calls']} for the multi-agent pipeline. On this test "
            f"the split doesn't buy accuracy. It buys containment: the only agent that reads the documents can't "
            f"query the database, and pass or fail is computed in code, so an instruction hidden in a report can't "
            f"change a result even if a model follows it.")


def health_rows(h: dict) -> list[tuple[str, str, str]]:
    ok, total = _verdict_counts(h["buggy"])
    return [
        ("Planted bugs caught", f"{h['model_hits']}/{h['bugs']}", f"{h['agent_hits']}/{h['bugs']}"),
        ("False alarms on the clean dashboard", str(h["model_clean_failed"]), str(h["agent_clean_fp"])),
        ("Explanations confirmed by the critic", "n/a", f"{ok} of {total}"),
        ("Model calls", "0", str(h["buggy"]["stats"].get("llm_calls", 0))),
        ("Work done", f"{h['model']['measures_checked']} measures in {h['model']['wall_time_s']}s",
         f"{len(h['buggy'].get('checks', []))} displayed numbers read and checked"),
    ]


def health_note(h: dict) -> str:
    ok, total = _verdict_counts(h["buggy"])
    note = (f"The model check needs no model calls, which is what makes it scale, but it can't see a bug that "
            f"only exists in the rendering ({', '.join(h['render_only']) or 'none this run'}). ")
    if total and ok < total:
        note += (f"The critic confirmed {ok} of {total} explanations in this run; {_uncertain_phrase(ok, total)}. "
                 f"That means the number is wrong, but the investigator didn't reproduce it exactly with SQL, so "
                 f"the critic wouldn't sign off on the cause.")
    elif total:
        note += f"The critic confirmed all {total} explanations in this run."
    return note + _second_look_line(h["buggy"])


# ---------------------------------------------------------------- page
CSS = """
:root {
  --bg: #e9eef3; --paper: #ffffff; --ink: #14213d; --muted: #52607a; --rule: #c5cfdb;
  --red: #c8102e; --red-soft: #fbe3e7; --ok: #1f6b4f; --ok-soft: #dcf0e7; --warn: #8a5a00; --warn-soft: #fbefd5;
  --code-bg: #f3f6f9; color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1b2b; --paper: #16263b; --ink: #e5ebf2; --muted: #9fb0c4; --rule: #2c3f57;
    --red: #ff5c6c; --red-soft: #3a1f2a; --ok: #74c7a4; --ok-soft: #173a2e; --warn: #f0c060; --warn-soft: #3a2f14;
    --code-bg: #0c1624; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1b2b; --paper: #16263b; --ink: #e5ebf2; --muted: #9fb0c4; --rule: #2c3f57;
  --red: #ff5c6c; --red-soft: #3a1f2a; --ok: #74c7a4; --ok-soft: #173a2e; --warn: #f0c060; --warn-soft: #3a2f14;
  --code-bg: #0c1624; color-scheme: dark;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 400 1.0625rem/1.6 "IBM Plex Sans", "Segoe UI", system-ui, sans-serif; font-variant-numeric: tabular-nums; }
a { color: inherit; text-decoration-color: var(--red); text-underline-offset: 3px; }
a:focus-visible, summary:focus-visible { outline: 3px solid var(--red); outline-offset: 3px; border-radius: 2px; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 0 1.5rem; }
header.top { display: flex; justify-content: space-between; align-items: center; gap: 1rem; padding: 1.25rem 0; flex-wrap: wrap; }
.brand { font-weight: 700; font-size: 1.125rem; }
.links { display: flex; gap: 1.25rem; flex-wrap: wrap; font-weight: 500; }
.hero { padding: 2.5rem 0 1.5rem; max-width: 46rem; }
h1 { font-size: clamp(2.1rem, 5vw, 3.6rem); line-height: 1.05; letter-spacing: -0.02em; margin: 0 0 1.25rem; font-weight: 700; }
.lede { font-size: 1.2rem; color: var(--muted); margin: 0; max-width: 40rem; }
.sheets { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 1.5rem; margin: 2rem 0 1rem; align-items: start; }
.sheets .stack, .gallery { display: grid; gap: 1.5rem; }
.gallery { grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 1.5rem 0 2rem; }
figure { margin: 0; }
figure img { display: block; width: 100%; height: auto; background: #fff; border: 1px solid var(--rule);
  box-shadow: 0 18px 40px -24px rgba(20, 33, 61, 0.45); }
figcaption { font-size: 0.9rem; color: var(--muted); margin-top: 0.5rem; }
section { padding: 3rem 0; border-top: 1px solid var(--rule); }
h2 { font-size: 1.75rem; line-height: 1.2; margin: 0 0 1rem; letter-spacing: -0.01em; }
h3 { font-size: 1.2rem; margin: 2rem 0 0.5rem; }
.intro { max-width: 44rem; color: var(--muted); margin: 0 0 1.5rem; }
ol.findings { list-style: none; padding: 0; margin: 0; }
.finding { display: grid; grid-template-columns: 2.75rem minmax(0, 1fr); gap: 1rem; padding: 1.5rem 0; border-bottom: 1px solid var(--rule); }
.marker { width: 2.25rem; height: 2.25rem; border-radius: 50%; background: var(--red); color: #fff;
  display: grid; place-items: center; font-weight: 700; }
.finding h3 { margin: 0.1rem 0 0.35rem; }
.where { font-weight: 400; color: var(--muted); font-size: 1rem; }
.finding p { margin: 0.35rem 0; max-width: 46rem; }
.numbers strong { font-weight: 600; }
.delta { color: var(--red); font-weight: 600; margin-left: 0.25rem; white-space: nowrap; }
.tags { display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 0.4rem 0; }
.cause, .verdict { display: inline-block; font-weight: 600; font-size: 0.9rem; padding: 0.1rem 0.6rem; border-radius: 4px; }
.cause { background: var(--red-soft); color: var(--red); }
.verdict.confirmed { background: var(--ok-soft); color: var(--ok); }
.verdict.uncertain { background: var(--warn-soft); color: var(--warn); }
.verdict.unreviewed { background: var(--code-bg); color: var(--muted); }
.critic { color: var(--muted); font-size: 0.95rem; }
details { margin-top: 0.5rem; }
summary { cursor: pointer; font-weight: 500; }
pre, code { font-family: "IBM Plex Mono", ui-monospace, Consolas, monospace; }
pre { background: var(--code-bg); border: 1px solid var(--rule); padding: 0.9rem 1rem; overflow-x: auto; margin: 0.6rem 0 0;
  font-size: 0.85rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
td code { font-size: 0.82rem; background: var(--code-bg); padding: 0.05rem 0.3rem; border-radius: 3px; }
.callout { background: var(--paper); border-left: 4px solid var(--red); padding: 1.1rem 1.25rem; max-width: 48rem; }
.callout p { margin: 0.3rem 0; }
ol.steps { padding-left: 1.4rem; margin: 0; max-width: 48rem; }
ol.steps li { padding: 0.5rem 0; }
.tools { color: var(--muted); display: block; font-size: 0.95rem; }
.table-scroll { overflow-x: auto; max-width: 100%; }
table { border-collapse: collapse; width: 100%; min-width: 34rem; background: var(--paper); }
th, td { text-align: left; padding: 0.65rem 0.85rem; border-bottom: 1px solid var(--rule); vertical-align: top; }
thead th { font-weight: 600; font-size: 0.95rem; border-bottom: 2px solid var(--ink); }
tbody th { font-weight: 500; }
td.num { white-space: nowrap; }
.caught { color: var(--ok); font-weight: 600; }
.missed { color: var(--red); font-weight: 600; }
.note { max-width: 48rem; margin-top: 1.25rem; }
footer { padding: 2rem 0 3rem; color: var(--muted); font-size: 0.9rem; border-top: 1px solid var(--rule); }
@media (max-width: 820px) {
  .sheets, .gallery { grid-template-columns: minmax(0, 1fr); }
  .finding { grid-template-columns: 2.25rem minmax(0, 1fr); gap: 0.75rem; }
  .marker { width: 1.9rem; height: 1.9rem; font-size: 0.9rem; }
}
"""


def _table(heads: list[str], rows: list[list[str]], first_is_header: bool = True) -> str:
    th = "".join(f'<th scope="col">{h}</th>' for h in heads)
    body = []
    for row in rows:
        cells = [f"<th scope='row'>{row[0]}</th>" if first_is_header else f"<td>{row[0]}</td>"]
        cells += [f"<td>{c}</td>" for c in row[1:]]
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f'<div class="table-scroll"><table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _retail_findings(issues: list[dict]) -> str:
    e = html.escape
    out = []
    for n, i in enumerate(issues, start=1):
        r = i.get("result") or {}
        sql = "".join(f"<pre><code>{e(q)}</code></pre>" for q in i.get("evidence_sql") or [])
        where = "dashboard" if i["artifact_id"].endswith(".png") else "report"
        critic = f'<p class="critic">Critic: {e(i["critic_reason"])}</p>' if i.get("critic_reason") else ""
        details = f"<details><summary>SQL behind the explanation</summary>{sql}</details>" if sql else ""
        second = '<span class="verdict unreviewed">Second look</span>' if i.get("second_look") else ""
        out.append(f"""
      <li class="finding" id="f{n}">
        <span class="marker" aria-hidden="true">{n}</span>
        <div class="finding-body">
          <h3>{e(i['label'])} <span class="where">in the {where}</span></h3>
          <p class="numbers">Shown <strong>{e(i['displayed_text'])}</strong>, database says
            <strong>{e(_num(r.get('expected'), r.get('unit')))}</strong> <span class="delta">{e(_delta(r))}</span></p>
          <div class="tags"><span class="cause">{e(CAUSES.get(i['root_cause'], i['root_cause']))}</span>{_badge(i.get('verdict', 'unreviewed'))}{second}</div>
          <p>{e(i.get('explanation', ''))}</p>
          {critic}
          {details}
        </div>
      </li>""")
    return "".join(out)


def _health_section(h: dict) -> str:
    e = html.escape
    gallery = "".join(f'<figure><img src="data:image/png;base64,{img}" alt="{e(name)} tab of the population health '
                      f'dashboard"><figcaption>Tab {n}: {e(name)}</figcaption></figure>'
                      for n, (name, img) in enumerate(h["tabs"], start=1))
    model_rows = [[e(f["tab"]), e(f["measure"]), _num(f["published_value"], None), _num(f["expected"], None),
                   e(_delta({"delta_pct": f.get("delta_pct")})), f"<code>{e(f['expression'])}</code>"]
                  for f in h["model"]["failed"]]
    agent_rows = []
    for i in h["buggy"]["issues"]:
        r = i.get("result") or {}
        agent_rows.append([e(h["tab_of"](i["artifact_id"])), e(i["label"]), e(i["displayed_text"]),
                           e(_num(r.get("expected"), r.get("unit"))), e(_delta(r)),
                           e(CAUSES.get(i["root_cause"], i["root_cause"])), _badge(i.get("verdict", "unreviewed"))])
    path_rows = [[e(b["bug_id"]), e(b["description"]),
                  f'<span class="{m}">{m}</span>', f'<span class="{a}">{a}</span>']
                 for b, m, a in h["paths"]]
    summary_rows = [[e(a), e(b), e(c)] for a, b, c in health_rows(h)]
    security = "".join(f"<p>{e(s['description'])}</p>" for s in h["buggy"].get("security_notes", []))
    return f"""
  <section aria-labelledby="dashboard" id="dashboard">
    <h2>The same checks on a four-tab BI dashboard</h2>
    <p class="intro">A population health report in the style of an embedded Power BI dashboard: {h['numbers_shown']}
      numbers on screen, {h['model']['measures_checked']} published measures behind them, and {h['bugs']} planted bugs.
      A report like this can be wrong in two places, so it gets checked two ways.</p>
    <div class="gallery">{gallery}</div>

    <h3>Both paths side by side</h3>
    {_table(["", "Model check (code)", "Agents on the rendered tabs"], summary_rows)}
    <p class="note">{e(health_note(h))}</p>

    <h3>Path 1: the semantic model, checked in code</h3>
    <p class="intro">Every published measure is recomputed from its governed definition. No model calls, so it costs
      the same for 30 measures or 3,000. These are the ones that didn't match:</p>
    {_table(["Tab", "Measure", "Published", "Expected", "Delta", "Expression"], model_rows, first_is_header=False)}

    <h3>Path 2: agents read what a viewer sees</h3>
    <p class="intro">The agents read the four rendered tabs, the same pipeline as the report above. This is the only way
      to catch a chart drawn from a stale extract or a tile whose label says thousands while the number is dollars.</p>
    {_table(["Tab", "Figure", "Shown", "Expected", "Delta", "Cause", "Critic"], agent_rows, first_is_header=False)}

    <h3>Which path caught which bug</h3>
    {_table(["Bug", "What went wrong", "Model check", "Agents"], path_rows, first_is_header=False)}

    <h3>Another hidden instruction</h3>
    <p class="intro">This time the instruction sits in a measure's description inside the semantic model, where only an
      automated reader would find it.</p>
    <div class="callout">{security or '<p>No hidden instruction was reported in this run.</p>'}</div>
  </section>"""


def render_page(retail: dict, health: dict | None, images: dict) -> str:
    e = html.escape
    buggy = retail["runs"][0]
    issues = buggy["issues"]
    n_checks = len(buggy["checks"])
    n_pass = sum(1 for c in buggy["checks"] if c["result"]["status"] == "PASS")
    ok, total = _verdict_counts(buggy)
    heads, rows = retail_rows(retail)
    results_table = _table(["Metric"] + heads, [[e(label)] + [e(c) for c in cells] for label, cells in rows])
    note = retail_note(retail)
    security = "".join(f"<p>{e(s['description'])}</p>" for s in buggy.get("security_notes", []))
    model = f"{buggy['provider']}:{buggy['model']}"
    nav_dash = '<a href="#dashboard">BI dashboard</a>' if health else ""
    verdict_line = (f"The critic confirmed {ok} of {total} explanations; {_uncertain_phrase(ok, total)}."
                    if total and ok < total else "The critic confirmed every explanation.")
    health_html = _health_section(health) if health else ""
    try_cmds = "\n".join(["pip install -r requirements.txt", "python -m reportguard.cli setup", "python -m pytest",
                          "python -m reportguard.cli run --pack buggy",
                          "python -m reportguard.cli setup --domain health",
                          "python -m reportguard.cli model-check --domain health"])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReportGuard: checking report numbers against the database</title>
<meta name="description" content="A multi-agent system that verifies every number in a business report or BI dashboard against SQL data and explains the mistakes.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <span class="brand">ReportGuard</span>
    <nav class="links"><a href="#findings">Report</a>{nav_dash}<a href="{REPO_URL}">Code on GitHub</a><a href="{COLAB_URL}">Open in Colab</a></nav>
  </header>

  <div class="hero">
    <h1>{_count(len(issues))} numbers in this report are wrong.</h1>
    <p class="lede">ReportGuard read the monthly business review and its dashboard, checked all {n_checks} numbers
      against the database, and marked the {len(issues)} that don't match. I planted every mistake on purpose, using
      the kind of SQL and labeling errors that happen in real reporting pipelines.{' Further down, the same checks run on a four-tab BI dashboard.' if health else ''}</p>
  </div>

  <div class="sheets">
    <figure>
      <img src="data:image/png;base64,{images['page1']}" alt="Page 1 of the monthly business review with the wrong key metrics circled in red and numbered">
      <figcaption>Monthly business review, page 1</figcaption>
    </figure>
    <div class="stack">
      <figure>
        <img src="data:image/png;base64,{images['dashboard']}" alt="Sales dashboard with the wrong tile outlined in red">
        <figcaption>Sales dashboard</figcaption>
      </figure>
      <figure>
        <img src="data:image/png;base64,{images['page2']}" alt="Page 2 of the review with the wrong chart label circled in red">
        <figcaption>Monthly business review, page 2</figcaption>
      </figure>
    </div>
  </div>

  <section aria-labelledby="findings">
    <h2 id="findings">What it found</h2>
    <p class="intro">{n_pass} of {n_checks} numbers matched. For each one that didn't, an investigator agent worked out
      why using SQL, and a critic agent checked the explanation before it made the report. {verdict_line}{e(_second_look_line(buggy))}
      The explanations below are the agents' own words from the recorded run.</p>
    <ol class="findings">{_retail_findings(issues)}
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
      <li><strong>Month evidence, in code.</strong> Each failing number is also checked against the previous and next month, so a number that belongs to another month arrives with proof attached.
        <span class="tools">Tool: check_metric</span></li>
      <li><strong>Investigator</strong> takes the failures and finds the cause with read-only SQL.
        <span class="tools">Tools: check_metric, run_sql, get_schema, get_metric_definition. Never sees document text.</span></li>
      <li><strong>Critic</strong> tests each explanation and marks it confirmed, uncertain or rejected. Uncertain ones go back to the investigator once, with the critic's objection, and are reviewed again.
        <span class="tools">Tools: check_metric, run_sql, get_metric_definition</span></li>
    </ol>
  </section>

  <section aria-labelledby="results">
    <h2 id="results">Measured results</h2>
    <p class="intro">Scored against answer keys the agents can't reach. The clean report has no mistakes, so any issue
      raised there would be a false alarm.</p>
    {results_table}
    {f'<p class="note">{e(note)}</p>' if note else ''}
  </section>
{health_html}
  <section aria-labelledby="try" class="try">
    <h2 id="try">Run it yourself</h2>
    <p class="intro">Open the notebook in Colab, or run it locally. Tests, the model check and a no-key mock run work out
      of the box; the agent runs need a Gemini API key.</p>
    <pre><code>{e(try_cmds)}</code></pre>
  </section>

  <footer>Recorded runs with <code>{e(model)}</code>, replayed from the response cache.
    <a href="{REPO_URL}">Source code</a></footer>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------- README
def render_readme_results(retail: dict, health: dict | None) -> str:
    model = f"{retail['runs'][0]['provider']}:{retail['runs'][0]['model']}"
    heads, rows = retail_rows(retail)
    lines = [RESULTS_START,
             f"Results with `{model}`, generated by `python -m reportguard.cli site` from the same recorded runs the",
             "demo page shows.", "", "**Retail report**", "",
             "| Metric | " + " | ".join(heads) + " |", "|---|" + "---|" * len(heads)]
    lines += [f"| {label} | " + " | ".join(cells) + " |" for label, cells in rows]
    note = retail_note(retail)
    if note:
        lines += ["", note]
    if health:
        lines += ["", "**Population health BI dashboard**", "",
                  "| | Model check (code) | Agents on the rendered tabs |", "|---|---|---|"]
        lines += [f"| {a} | {b} | {c} |" for a, b, c in health_rows(health)]
        lines += ["", health_note(health)]
    lines += ["", "This is one recorded run on a small synthetic benchmark.", RESULTS_END]
    return "\n".join(lines)


def update_readme(readme_path, block: str) -> bool:
    """Replace the text between the results markers. Returns False if the markers are missing."""
    text = readme_path.read_text(encoding="utf-8")
    start, end = text.find(RESULTS_START), text.find(RESULTS_END)
    if start == -1 or end == -1:
        return False
    readme_path.write_text(text[:start] + block + text[end + len(RESULTS_END):], encoding="utf-8")
    return True


# ---------------------------------------------------------------- replay
async def _replay_retail(with_single: bool, warnings: list[str]) -> dict:
    from .llm import make_provider
    from .pipeline import run_multi_agent, run_single_agent

    def replay():
        return make_provider("gemini", cache_mode="replay")

    config.set_domain("retail")
    if not config.DB_PATH.exists():
        from .cli import setup
        setup("retail")
    runs = [(await run_multi_agent(replay(), "buggy", verbose=False)).to_json(),
            (await run_multi_agent(replay(), "clean", verbose=False)).to_json()]
    for name, run in zip(("buggy", "clean"), runs):
        if run.get("error"):
            raise RuntimeError(f"Replay of the retail {name} run failed: {run['error']}. Run the notebook's Gemini "
                               f"cells once (record mode) before building the page.")
    if with_single:
        single = (await run_single_agent(replay(), "buggy", verbose=False)).to_json()
        if single.get("error"):
            warnings.append(f"single-agent run not found in the cache, left off the page ({single['error'][:120]})")
        else:
            runs.append(single)
    scores = [score_run(r) for r in runs]
    images = marked_up_images(runs[0]["issues"], "buggy", runs[0]["period"])
    return {"runs": runs, "scores": scores, "images": images}


def health_data(buggy: dict, clean: dict) -> dict:
    """Everything the health section needs, from the two agent runs plus the code-only model check.
    Call with the health domain active."""
    import json

    from .health.dashboard import TABS
    from .health.model_check import validate_semantic_model

    model = validate_semantic_model("buggy")
    model_clean = validate_semantic_model("clean")
    manifest = json.loads((config.MANIFEST_DIR / "buggy.json").read_text(encoding="utf-8"))
    key = lambda m, d: (m, (d or "").strip().lower() or None)  # noqa: E731
    model_hits = {key(f["metric_id"], f["dimension_value"]) for f in model["failed"]}
    agent_hits = {key(i["metric_id"], i.get("dimension_value")) for i in buggy["issues"]
                  if i.get("verdict") != "rejected"}
    paths = []
    for b in manifest["bugs"]:
        k = key(b["metric_id"], b["dimension_value"])
        paths.append((b, "caught" if k in model_hits else "missed", "caught" if k in agent_hits else "missed"))

    def tab_of(artifact_id: str) -> str:
        m = re.match(r"tab(\d)_", artifact_id)
        return TABS[int(m.group(1)) - 1] if m else artifact_id

    tabs = []
    for n, name in enumerate(TABS, start=1):
        img = Image.open(config.REPORTS_DIR / f"tab{n}_{manifest['period']}_buggy.png")
        tabs.append((name, _png_b64(img, max_width=900)))
    return {"buggy": buggy, "clean": clean, "model": model, "model_clean_failed": len(model_clean["failed"]),
            "agent_clean_fp": score_run(clean)["false_positives"], "bugs": len(manifest["bugs"]),
            "model_hits": sum(1 for _, m, _ in paths if m == "caught"),
            "agent_hits": sum(1 for _, _, a in paths if a == "caught"),
            "render_only": [b["bug_id"] for b, m, a in paths if m == "missed" and a == "caught"],
            "numbers_shown": len(manifest["figures"]), "paths": paths, "tab_of": tab_of, "tabs": tabs}


async def _replay_health(warnings: list[str]) -> dict | None:
    from .llm import make_provider
    from .pipeline import run_multi_agent

    config.set_domain("health")
    if not config.DB_PATH.exists():
        from .cli import setup
        setup("health")
    runs = {}
    for pack in ("buggy", "clean"):
        run = (await run_multi_agent(make_provider("gemini", cache_mode="replay"), pack, verbose=False)).to_json()
        if run.get("error"):
            warnings.append(f"health dashboard {pack} run not found in the cache, section left off the page "
                            f"({run['error'][:120]})")
            return None
        runs[pack] = run
    return health_data(runs["buggy"], runs["clean"])


async def build_demo_page(out_path=None, readme_path=None, with_single: bool = True, with_health: bool = True) -> dict:
    """Replay the recorded runs (no API calls), write docs/index.html and refresh the README results block."""
    original = config.DOMAIN
    warnings: list[str] = []
    try:
        retail = await _replay_retail(with_single, warnings)
        health = await _replay_health(warnings) if with_health else None
    finally:
        config.set_domain(original)
    page = render_page(retail, health, retail["images"])
    out_path = out_path or config.PROJECT_ROOT / "docs" / "index.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    readme_path = readme_path or config.PROJECT_ROOT / "README.md"
    if not update_readme(readme_path, render_readme_results(retail, health)):
        warnings.append(f"no results markers in {readme_path}; README not updated")
    return {"page": out_path, "readme": readme_path, "warnings": warnings,
            "sections": ["retail"] + (["health"] if health else [])}
