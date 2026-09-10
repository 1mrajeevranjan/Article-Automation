"""Offline checks for the four fixes shipped together:

  a) the fetched model catalogue reaches every sheet tab, not just Settings
  b) an unavailable model is reported with the reason, not just "unavailable"
  c) a successful row logs how long each stage took
  d) the PDF carries no red disclaimer paragraph

No network and no real Excel file — every provider call is stubbed, so this runs
in about a second and can gate a commit.

    .venv/bin/python tests_models_everywhere_check.py
"""

import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_manager
import ai_client
import gui_app
import pdf_renderer
from batch_runner import _mmss
from state import ArticleState

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, condition, detail=""):
    results.append((PASS if condition else FAIL, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- (b) reasons

def test_reason_classification():
    cases = [
        (429, "Rate limit exceeded: free-models-per-day", "daily free quota used up"),
        (403, "This model is only available on agentic harnesses", "not available via plain API"),
        (504, "upstream aborted", "provider down"),
        (502, "bad gateway", "provider down"),
        (404, "no such model", "model not offered"),
    ]
    for code, detail, expected in cases:
        got = ai_client._classify_reason(code, detail)
        check(f"HTTP {code} -> '{expected}'", got == expected, f"got '{got}'")


def test_ping_models_delegates():
    """ping_models must stay a view over probe_models, preserving caller order."""
    fake = {"a": (True, ""), "b": (False, "provider down"), "c": (True, "")}
    with patch.object(ai_client, "probe_models", return_value=fake):
        alive = ai_client.ping_models(["c", "b", "a"], {})
    check("ping_models filters and keeps caller order", alive == ["c", "a"], f"got {alive}")


# ----------------------------------------------------- (a) catalogue everywhere

def test_config_carries_full_catalogue():
    cfg = config_manager.load_config()
    models = cfg.get("free_models") or []
    check("config.yaml holds the whole catalogue, not a curated few",
          len(models) > 4, f"{len(models)} models")
    check("configured default model is in the catalogue",
          cfg["ai_provider"]["model"] in models, cfg["ai_provider"]["model"])


def test_fetch_persists_and_notifies():
    """Settings' fetch must write config and tell the app, or sheet tabs stay stale."""
    root = tk.Tk()
    root.withdraw()
    notified = []
    catalogue = [f"vendor/model-{i}:free" for i in range(19)]
    saved = {}

    try:
        tab = gui_app.SettingsTab(root, on_models_changed=lambda: notified.append(True))
        tab.vars["api_key"].set("sk-test")
        tab.vars["base_url"].set("https://example.invalid/api/v1")

        with patch.object(config_manager, "fetch_free_models", return_value=catalogue), \
             patch.object(gui_app.config_manager, "fetch_free_models", return_value=catalogue), \
             patch.object(gui_app.config_manager, "save_config", side_effect=lambda c: saved.update(c)):
            tab._fetch_models()

        check("fetch writes the catalogue to config",
              saved.get("free_models") == catalogue, f"{len(saved.get('free_models', []))} saved")
        check("fetch notifies the app so sheet tabs refresh", notified == [True])
        check("Settings combo offers every fetched model",
              list(tab.model_combo.cget("values")) == catalogue)
    finally:
        root.destroy()


def test_sheet_tab_offers_every_model():
    root = tk.Tk()
    root.withdraw()
    catalogue = [f"vendor/model-{i}:free" for i in range(19)]
    cfg = dict(config_manager.load_config())
    cfg["free_models"] = catalogue

    try:
        tab = gui_app.BatchTab(root)
        with patch.object(gui_app.config_manager, "load_config", return_value=cfg):
            tab._refresh_parallel_models()
        values = list(tab.parallel_model_combo.cget("values"))
        check("sheet tab offers Auto + every catalogue model",
              values == [gui_app.AUTO_MODEL] + catalogue, f"{len(values)} entries")
    finally:
        root.destroy()


def test_app_pushes_catalogue_to_every_tab():
    """The App-level callback must reach all open sheet tabs, not just the active one."""
    check("App exposes _refresh_model_lists", hasattr(gui_app.App, "_refresh_model_lists"))
    source = Path(gui_app.__file__).read_text(encoding="utf-8")
    check("App wires the callback into SettingsTab",
          "SettingsTab(self.notebook, on_models_changed=self._refresh_model_lists)" in source)
    check("callback loops over every sheet tab",
          "for tab in self.sheet_tabs:" in source.split("def _refresh_model_lists")[1][:300])


# ------------------------------------------------------------- (c) stage timings

def test_stage_timings_recorded_and_formatted():
    state = ArticleState(row_number=2, title="t", scope="s")
    check("ArticleState carries stage timings", state.stage_timings == [])

    orchestrator_src = Path("orchestrator.py").read_text(encoding="utf-8")
    check("_mark appends to state.stage_timings",
          "state.stage_timings.append((stage, elapsed))" in orchestrator_src)

    runner_src = Path("batch_runner.py").read_text(encoding="utf-8")
    check("success log prints the total",
          '{state.status} in {_mmss(total)}' in runner_src)
    check("success log prints the per-stage breakdown",
          'f"{stage} {_mmss(seconds)}"' in runner_src)

    check("_mmss under a minute", _mmss(34.6) == "35s", _mmss(34.6))
    check("_mmss over a minute", _mmss(252.3) == "4m12s", _mmss(252.3))
    check("_mmss pads seconds", _mmss(65) == "1m05s", _mmss(65))


# ---------------------------------------------------------------- (d) no disclaimer

BANNED = "AI-generated for illustrative structure only"


def pdf_text(path: Path) -> str:
    """Everything drawn on the page, as text.

    Searching the raw PDF bytes looks like it works and does not — twice over. The
    content streams are Flate-compressed, and with the embedded Palatino subset the
    text is written as glyph indices rather than ASCII. Both make a naive substring
    check pass while the words are plainly on the page, so decode properly.
    """
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def test_pdf_text_extraction_actually_works():
    """Guards the guard: if pdf_text stops seeing text, every check below goes green
    for the wrong reason."""
    state = ArticleState(row_number=1, title="Sentinel Heading", scope="s",
                         target_word_count=50, target_sections=3)
    state.intro = "Intro."
    state.section_drafts = {"Method": "Body."}
    state.conclusion = "Done."
    out_dir = Path(tempfile.mkdtemp(prefix="pdf_probe_"))
    try:
        path = pdf_renderer.render_pdf(state, out_dir, {"font": "Palatino", "page_size": "A4"})
        text = pdf_text(path)
        check("pdf_text can read text off a rendered page", "Sentinel" in text,
              f"{len(text)} chars extracted")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_pdf_has_no_disclaimer():
    source = Path("pdf_renderer.py").read_text(encoding="utf-8")
    check("disclaimer text is gone from the renderer", BANNED not in source)
    check("disclaimer style is gone too", '"disclaimer": ParagraphStyle' not in source)

    state = ArticleState(row_number=2, title="Test Article", scope="scope",
                         author="A. Author", target_word_count=100, target_sections=3)
    state.abstract = "An abstract."
    state.keywords = ["one", "two"]
    state.intro = "Intro text."
    state.section_drafts = {"Method": "Method text."}
    state.conclusion = "Conclusion text."
    state.references = ["[1] A. Author, \"A paper,\" IEEE Trans., 2024."]

    out_dir = Path(tempfile.mkdtemp(prefix="disclaimer_check_"))
    try:
        path = pdf_renderer.render_pdf(state, out_dir, {"font": "Palatino", "page_size": "A4"})
        text = pdf_text(path)
        check("PDF actually rendered", path.exists() and path.stat().st_size > 1000,
              f"{path.stat().st_size} bytes")
        check("rendered PDF still has a References section", "References" in text)
        check("rendered PDF carries no disclaimer text", BANNED not in text)
        # reportlab emits the style's colour as an `rg` operator; #B00020 -> .690 0 .125.
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    test_reason_classification()
    test_ping_models_delegates()
    test_config_carries_full_catalogue()
    test_fetch_persists_and_notifies()
    test_sheet_tab_offers_every_model()
    test_app_pushes_catalogue_to_every_tab()
    test_stage_timings_recorded_and_formatted()
    test_pdf_text_extraction_actually_works()
    test_pdf_has_no_disclaimer()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
