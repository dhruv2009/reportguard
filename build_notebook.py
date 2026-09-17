"""Builds ReportGuard_Colab.ipynb from the repo files: python build_notebook.py"""

from pathlib import Path

import nbformat as nbf

ROOT = Path(__file__).parent
FILES = sorted(
    [p for p in (ROOT / "reportguard").rglob("*.py")]
    + [ROOT / "run_server.py", ROOT / "skills/report-qa/SKILL.md", ROOT / "tests/test_reportguard.py",
       ROOT / "requirements.txt", ROOT / "pytest.ini", ROOT / "README.md"]
)

md = nbf.v4.new_markdown_cell
code = nbf.v4.new_code_cell
cells = []

cells.append(md("""# ReportGuard

Checks the numbers in a PDF report and dashboard against the database using an MCP server and a
few agents (extractor, planner, investigator, critic).

Needs `GEMINI_API_KEY` in Colab Secrets for the Gemini sections. Everything before that runs without a key.
"""))

cells.append(md("## Settings"))
cells.append(code("""USE_DRIVE_FOR_CACHE = True        # store LLM responses on Drive so they survive a new session
RUN_SINGLE_AGENT_BASELINE = True
PROJECT = "/content/reportguard"

import os, sys, time, json, subprocess
try:
    import google.colab  # noqa: F401
    IN_COLAB = True
except ImportError:
    IN_COLAB = False

cache_dir = f"{PROJECT}/data/llm_cache"
if IN_COLAB and USE_DRIVE_FOR_CACHE:
    from google.colab import drive
    drive.mount("/content/drive")
    cache_dir = "/content/drive/MyDrive/reportguard_llm_cache"
os.environ["RG_CACHE_DIR"] = cache_dir
for d in ["reportguard/llm", "skills/report-qa", "tests"]:
    os.makedirs(f"{PROJECT}/{d}", exist_ok=True)
print(PROJECT, cache_dir)"""))

cells.append(md("## Install"))
cells.append(code("""packages = ["mcp==2.2.0", "reportlab==4.4.10", "pdfplumber==0.11.9", "pypdfium2==5.6.0",
            "matplotlib==3.10.8", "pydantic>=2.12", "httpx>=0.27", "pytest"]
if not os.environ.get("RG_SKIP_INSTALL"):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *packages], check=True)
"""))

cells.append(md("## Project files"))
for f in FILES:
    rel = f.relative_to(ROOT).as_posix()
    cells.append(code(f"%%writefile {{PROJECT}}/{rel}\n" + f.read_text()))

cells.append(code("""os.chdir(PROJECT)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
"""))

cells.append(md("## Generate data and reports"))
cells.append(code("""from IPython.display import display, Markdown, Image
from reportguard import config
from reportguard.cli import setup
from reportguard.pdf_tools import render_pdf_page

print(json.dumps(setup(), indent=2))
pdf = config.REPORTS_DIR / "mbr_2026-08_buggy.pdf"

display(Image(render_pdf_page(pdf, 1, scale=1.1)))
display(Image(render_pdf_page(pdf, 2, scale=1.1)))

display(Image(filename=str(config.REPORTS_DIR / "dashboard_2026-08_buggy.png"), width=950))"""))

cells.append(md("## Tests"))
cells.append(code("""out = subprocess.run([sys.executable, "-m", "pytest", "--color=no"], cwd=PROJECT, capture_output=True, text=True)
print(out.stdout[-3000:], out.stderr[-2000:])"""))

cells.append(md("## MCP server"))
cells.append(code("""from reportguard.pipeline import connect_mcp
from reportguard.agents import mcp_result_text

async with connect_mcp() as mcp:
    print("Server:", mcp.server_info.name, "| protocol", mcp.protocol_version)
    print("\\nTools:")
    for t in (await mcp.list_tools()).tools:
        print(f"  {t.name:24s} {t.description.splitlines()[0][:90]}")
    print("\\nResources:", [r.uri for r in (await mcp.list_resources()).resources],
          [t.uri_template for t in (await mcp.list_resource_templates()).resource_templates])
    print("Prompts:", [p.name for p in (await mcp.list_prompts()).prompts])

    print("\\ncheck_metric ACTIVE_CUSTOMERS=1105, 2026-08")
    r = await mcp.call_tool("check_metric", {"metric_id": "ACTIVE_CUSTOMERS", "reported_value": 1105,
                                             "unit_label": "", "period": "2026-08"})
    print(mcp_result_text(r))
    print("\\nsame value, 2026-07")
    r = await mcp.call_tool("check_metric", {"metric_id": "ACTIVE_CUSTOMERS", "reported_value": 1105,
                                             "unit_label": "", "period": "2026-07"})
    print(json.loads(mcp_result_text(r))["status"])

    for attack in ["DELETE FROM orders", "SELECT 1; DROP TABLE orders", "SELECT load_extension('evil')"]:
        r = await mcp.call_tool("run_sql", {"query": attack})
        print(f"\\n{attack} -> {mcp_result_text(r)}")"""))

cells.append(md("## Mock provider run (no API key)"))
cells.append(code("""from reportguard.llm import make_provider
from reportguard.pipeline import run_multi_agent, run_single_agent, save_run
from reportguard.qa_report import render_markdown
from reportguard.evals import score_run, scorecard_markdown

mock_result = await run_multi_agent(make_provider("mock"), pack="buggy")
display(Markdown(render_markdown(mock_result)))
display(Markdown(scorecard_markdown([score_run(mock_result)])))"""))

cells.append(md("## Gemini"))
cells.append(code("""GEMINI_READY = False
if IN_COLAB and not os.environ.get("GEMINI_API_KEY"):
    try:
        from google.colab import userdata
        os.environ["GEMINI_API_KEY"] = userdata.get("GEMINI_API_KEY")
    except Exception as exc:
        print("secret not available:", type(exc).__name__)
if os.environ.get("GEMINI_API_KEY"):
    gemini = make_provider("gemini", cache_mode="record")
    await gemini.prepare()
    print(gemini.model)
    GEMINI_READY = True
else:
    print("GEMINI_API_KEY not set")"""))

cells.append(md("## Buggy pack"))
cells.append(code("""if GEMINI_READY:
    buggy = await run_multi_agent(gemini, pack="buggy")
    print("Saved to", save_run(buggy, f"{gemini.model}_multi_buggy"))
    display(Markdown(render_markdown(buggy)))
else:
    print("skipped")"""))

cells.append(md("## Clean pack"))
cells.append(code("""if GEMINI_READY:
    clean = await run_multi_agent(gemini, pack="clean")
    print("Saved to", save_run(clean, f"{gemini.model}_multi_clean"))
    display(Markdown(render_markdown(clean)))
else:
    print("skipped")"""))

cells.append(md("## Eval"))
cells.append(code("""if GEMINI_READY:
    runs = [buggy, clean]
    if RUN_SINGLE_AGENT_BASELINE:
        single = await run_single_agent(gemini, pack="buggy")
        save_run(single, f"{gemini.model}_single_buggy")
        runs.append(single)
    scores = [score_run(r) for r in runs]
    card = scorecard_markdown(scores)
    (config.RUNS_DIR / "scorecard.md").write_text(card)
    display(Markdown(card))
    for s in scores:
        if s["false_positive_details"]:
            print(s["mode"], s["pack"], "false positives:", s["false_positive_details"])
else:
    print("skipped")"""))

cells.append(md("## Trace"))
cells.append(code("""import pandas as pd
r = buggy if GEMINI_READY else mock_result
cols = ["t", "agent", "kind", "turn", "tool", "tool_calls", "is_error", "cached", "latency_s", "input_tokens", "output_tokens"]
df = pd.DataFrame(r.trace).reindex(columns=cols)
display(df[df["kind"].isin(["llm_call", "tool_call", "security", "validation_error"])])
display(pd.DataFrame(r.stats["by_agent"]).T)"""))

cells.append(md("## Replay from cache"))
cells.append(code("""try:
    replayer = make_provider("gemini", cache_mode="replay")
    start = time.time()
    replayed = await run_multi_agent(replayer, pack="buggy")
    print(f"{time.time() - start:.1f}s, {replayed.stats['llm_calls_from_cache']}/{replayed.stats['llm_calls']} calls from cache")
    display(Markdown(render_markdown(replayed)))
except Exception as exc:
    print("replay failed:", exc)"""))

cells.append(md("## Ollama (optional, needs a GPU runtime)"))
cells.append(code("""RUN_OLLAMA = False
OFFLINE_MODEL = "qwen2.5:7b-instruct"
if RUN_OLLAMA:
    subprocess.run("apt-get -qq install -y zstd pciutils > /dev/null && curl -fsSL https://ollama.com/install.sh | sh",
                   shell=True, check=True)
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)
    subprocess.run(["ollama", "pull", OFFLINE_MODEL], check=True)
    local = make_provider("ollama", cache_mode="record", model=OFFLINE_MODEL)
    offline = await run_multi_agent(local, pack="buggy")
    display(Markdown(render_markdown(offline)))
    display(Markdown(scorecard_markdown([score_run(offline)])))
else:
    pass"""))

cells.append(md("## Download"))
cells.append(code("""import shutil
archive = shutil.make_archive("/content/reportguard_project", "zip", root_dir="/content", base_dir="reportguard")
print(archive)
if IN_COLAB:
    from google.colab import files
    files.download(archive)"""))

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {"colab": {"provenance": [], "toc_visible": True},
                  "kernelspec": {"display_name": "Python 3", "name": "python3"},
                  "language_info": {"name": "python"}}
out = ROOT / "ReportGuard_Colab.ipynb"
nbf.write(nb, out)
print(f"Wrote {out} with {len(cells)} cells")
