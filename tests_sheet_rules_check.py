"""The rules a sheet's columns imply, verified offline.

  1. Only Title and Scope/Synopsis drive generation. A Subject Area column must never
     be mistaken for the Title — it used to be, and every article was written about
     "Machine Learning" instead of its actual title.
  2. A Year column is a cutoff: the content prompts say so, and every reference is
     forced to a publication year at or below it.
  3. Author columns — however many — all land on the PDF byline, between the title
     and the abstract.

    .venv/bin/python tests_sheet_rules_check.py
"""

import datetime
import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path

import openpyxl
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_manager
import gui_app
import pdf_renderer
from agents import references_agent
from agents._prompts import year_fields
from excel_io import ExcelBatch, parse_year, _match_column
from orchestrator import run_article
from state import ArticleState

PASS, FAIL = "PASS", "FAIL"
results = []
TMP = Path(tempfile.mkdtemp(prefix="sheet_rules_"))


def check(name, condition, detail=""):
    results.append((PASS if condition else FAIL, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def make_sheet(name: str, header: list, rows: list, preamble: list | None = None) -> Path:
    path = TMP / name
    wb = openpyxl.Workbook()
    ws = wb.active
    for line in (preamble or []):
        ws.append(line)
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


# --------------------------------------------------- 1. the right column is the title

def test_subject_area_never_steals_the_title():
    path = make_sheet(
        "subject_first.xlsx",
        ["Subject Area", "Title", "Synopsis", "Year", "Author Names"],
        [["Machine Learning", "Federated Learning at the Edge",
          "Survey of FL under bandwidth constraints", 2023, "R. Ranjan"]],
    )
    rows = list(ExcelBatch(path).read_rows())
    check("Subject Area left of Title does not become the title",
          rows[0].title == "Federated Learning at the Edge", rows[0].title)
    check("Synopsis is the scope", rows[0].scope.startswith("Survey of FL"), rows[0].scope[:30])


def test_exact_header_beats_a_partial_one():
    check("'subject area' resolves to subject, not title",
          _match_column("Subject Area")[0] == "subject")
    check("exact 'Title' resolves to title", _match_column("Title")[0] == "title")
    check("'Article Title (draft)' still resolves to title",
          _match_column("Article Title (draft)")[0] == "title")
    exact = _match_column("Title")[1]
    partial = _match_column("Proposed Article Title for Review")[1]
    check("an exact header outscores a partial one", exact > partial, f"{exact} vs {partial}")


def test_synopsis_preferred_over_abstract():
    path = make_sheet(
        "both_scopes.xlsx",
        ["Title", "Abstract", "Synopsis"],
        [["Zero-Trust Networks", "the abstract cell", "the synopsis cell"]],
    )
    rows = list(ExcelBatch(path).read_rows())
    check("Synopsis wins over Abstract even when Abstract is further left",
          rows[0].scope == "the synopsis cell", rows[0].scope)


def test_column_order_does_not_matter():
    path = make_sheet(
        "shuffled.xlsx",
        ["Year", "Author", "Synopsis", "Category", "Title"],
        [[2020, "S. Mehta", "scope text", "Networking", "6G Spectrum Sharing"]],
    )
    row = list(ExcelBatch(path).read_rows())[0]
    check("columns resolve regardless of order",
          (row.title, row.scope, row.author, row.year)
          == ("6G Spectrum Sharing", "scope text", "S. Mehta", 2020),
          f"{row.title!r} {row.author!r} {row.year!r}")


def test_header_below_a_preamble_still_found():
    path = make_sheet(
        "preamble.xlsx",
        ["Subject Area", "Title", "Synopsis", "Year"],
        [["AI", "Neural Scaling Laws", "scope", 2022]],
        preamble=[["Department of Research — 2026 assignments"], []],
    )
    batch = ExcelBatch(path)
    rows = list(batch.read_rows())
    check("header found under a preamble", batch.header_row == 3, f"row {batch.header_row}")
    check("row read correctly under a preamble",
          rows[0].title == "Neural Scaling Laws" and rows[0].year == 2022)


# ------------------------------------------------------------------- 2. the year rule

def test_year_parsing():
    cases = [(2023, 2023), ("2023", 2023), (2023.0, 2023), ("by 2021", 2021),
             (datetime.datetime(2019, 6, 1), 2019), (None, None), ("", None),
             ("n/a", None), (1500, None), (True, None)]
    for raw, expected in cases:
        got = parse_year(raw)
        check(f"parse_year({raw!r}) -> {expected}", got == expected, f"got {got}")


def test_year_absent_leaves_prompts_clean():
    state = ArticleState(row_number=2, title="t", scope="s")
    fields = year_fields(state)
    check("no Year column -> no temporal text in prompts",
          fields["year_constraint"] == "" and fields["year_reference_rule"] == "")


def test_year_present_constrains_prompts():
    state = ArticleState(row_number=2, title="t", scope="s", year=2021)
    fields = year_fields(state)
    check("year constraint names the year", "2021" in fields["year_constraint"])
    check("reference rule names the year", "2021" in fields["year_reference_rule"])


def test_reference_cutoff_filter():
    check("citation_year takes the publication year, not one in the title",
          references_agent.citation_year(
              '[1] A. B, "Lessons from the 2008 crisis," IEEE Trans., 2019.') == 2019)
    check("post-cutoff reference rejected",
          not references_agent.within_cutoff('[1] A, "P," IEEE, 2024.', 2021))
    check("on-cutoff reference kept",
          references_agent.within_cutoff('[1] A, "P," IEEE, 2021.', 2021))
    check("undated reference kept rather than silently dropped",
          references_agent.within_cutoff('[1] A, "P," IEEE.', 2021))
    check("no cutoff means everything is kept",
          references_agent.within_cutoff('[1] A, "P," IEEE, 2030.', None))


class StubClient:
    """Answers every pipeline call; deliberately mixes post-cutoff citations in."""
    last_model = "stub/model:free"

    def __init__(self):
        self.prompts = []

    def chat_completion(self, system_prompt, user_prompt, model=None, temperature=None):
        self.prompts.append(user_prompt)
        low = user_prompt.lower()
        if "ieee numbered format" in low or "references section" in low:
            return "\n".join(
                f'[{i}] A. Author, "Paper {i}," IEEE Trans., {2018 if i % 2 else 2024}.'
                for i in range(1, 21))
        if "###abstract###" in low:
            return "Abstract sentence. " * 40 + "\nKeywords: a, b, c"
        if "middle-section headings" in low:
            return "\n".join(f"Section {i}" for i in range(1, 4))
        return "Body sentence. " * 80


def test_year_flows_through_the_pipeline():
    config = config_manager.load_config()
    config["validation"]["max_correction_passes"] = 1
    client = StubClient()
    out = Path(tempfile.mkdtemp(prefix="year_pipeline_", dir=TMP))

    state = run_article(2, "A Title", "a scope", "R. Ranjan", 800, 5,
                        config, client, out, year=2021)

    check("article completes with a year set", state.status.startswith("Success"), state.notes)
    check("year lands on the state", state.year == 2021)

    constrained = sum(1 for p in client.prompts if "TEMPORAL CONSTRAINT" in p)
    check("content prompts carry the cutoff", constrained >= 4,
          f"{constrained}/{len(client.prompts)} prompts")

    years = [references_agent.citation_year(r) for r in state.references]
    check("every reference is at or before the cutoff",
          all(y is not None and y <= 2021 for y in years),
          f"max {max(y for y in years if y)}")
    check("post-cutoff citations were replaced, not just dropped",
          len(state.references) == 20, f"{len(state.references)} references")
    check("numbering stays contiguous after filtering",
          [r.split("]")[0] + "]" for r in state.references]
          == [f"[{i}]" for i in range(1, len(state.references) + 1)])


# ------------------------------------------------------------------ 3. author byline

def test_multiple_author_columns_are_merged():
    path = make_sheet(
        "authors.xlsx",
        ["Title", "Synopsis", "Author Name 1", "Author Name 2", "Co-Authors"],
        [["T", "s", "R. Ranjan", "A. Kumar; S. Devi", "P. Singh and M. Rao"],
         ["T2", "s2", "Solo Author", None, ""]],
    )
    rows = list(ExcelBatch(path).read_rows())
    check("names from every author column, split on ; , and 'and'",
          rows[0].author == "R. Ranjan, A. Kumar, S. Devi, P. Singh, M. Rao", rows[0].author)
    check("a single author still reads plainly", rows[1].author == "Solo Author", rows[1].author)


def test_no_author_column_is_not_an_error():
    path = make_sheet("no_author.xlsx", ["Title", "Synopsis"], [["T", "s"]])
    rows = list(ExcelBatch(path).read_rows())
    check("a sheet without authors still reads", len(rows) == 1 and rows[0].author == "")


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def test_authors_sit_between_title_and_abstract():
    state = ArticleState(row_number=2, title="Federated Learning at the Edge", scope="s",
                         author="R. Ranjan, A. Kumar, S. Devi",
                         target_word_count=100, target_sections=3, year=2021)
    state.abstract = "An abstract."
    state.keywords = ["one"]
    state.intro = "Intro."
    state.section_drafts = {"Method": "Body."}
    state.conclusion = "End."
    out = Path(tempfile.mkdtemp(prefix="byline_", dir=TMP))
    path = pdf_renderer.render_pdf(state, out, {"font": "Palatino", "page_size": "A4"})
    text = pdf_text(path)

    check("byline extraction works at all", "R. Ranjan" in text, f"{len(text)} chars")
    for name in ("R. Ranjan", "A. Kumar", "S. Devi"):
        check(f"'{name}' appears on the PDF", name in text)
    if "R. Ranjan" in text and "Abstract" in text:
        check("byline sits below the title and above the abstract",
              text.index("Federated") < text.index("R. Ranjan") < text.index("Abstract"))


# ------------------------------------------------------------------ 4. the rows table

def test_table_shows_the_sheet_title_and_year():
    path = make_sheet(
        "table.xlsx",
        ["Subject Area", "Title", "Synopsis", "Year", "Author"],
        [["Machine Learning", "Federated Learning at the Edge", "scope", 2023, "R. Ranjan"],
         ["Security", "Zero-Trust Networks", "scope", None, "S. Mehta"]],
    )
    root = tk.Tk()
    root.withdraw()
    try:
        tab = gui_app.BatchTab(root)
        tab.excel_path = path
        tab.output_dir = TMP
        tab._load_rows()
        root.update()
        rendered = [tab.tree.item(i, "values") for i in tab.tree.get_children()]
        titles = [v[gui_app.COL["title"]] for v in rendered]
        years = [v[gui_app.COL["year"]] for v in rendered]
        check("table shows the Title column, not Subject Area",
              titles == ["Federated Learning at the Edge", "Zero-Trust Networks"], str(titles))
        check("table shows the year, and an em dash when absent",
              years == ["2023", "—"] or years == [2023, "—"], str(years))
    finally:
        root.destroy()


def test_refresh_defaults_does_not_overwrite_the_title():
    """Regression: _refresh_defaults wrote the word count into values[2] — the Title."""
    path = make_sheet("refresh.xlsx", ["Title", "Synopsis"], [["Keep This Title", "scope"]])
    root = tk.Tk()
    root.withdraw()
    try:
        tab = gui_app.BatchTab(root)
        tab.excel_path = path
        tab.output_dir = TMP
        tab._load_rows()
        root.update()
        tab._refresh_defaults()
        root.update()
        values = tab.tree.item(tab.tree.get_children()[0], "values")
        check("Refresh Defaults leaves the title alone",
              values[gui_app.COL["title"]] == "Keep This Title", str(values))
        defaults = config_manager.load_config().get("defaults", {})
        check("Refresh Defaults updates the word count cell",
              str(values[gui_app.COL["word_count"]]) == str(defaults["word_count"]), str(values))
    finally:
        root.destroy()


if __name__ == "__main__":
    try:
        test_subject_area_never_steals_the_title()
        test_exact_header_beats_a_partial_one()
        test_synopsis_preferred_over_abstract()
        test_column_order_does_not_matter()
        test_header_below_a_preamble_still_found()
        test_year_parsing()
        test_year_absent_leaves_prompts_clean()
        test_year_present_constrains_prompts()
        test_reference_cutoff_filter()
        test_year_flows_through_the_pipeline()
        test_multiple_author_columns_are_merged()
        test_no_author_column_is_not_an_error()
        test_authors_sit_between_title_and_abstract()
        test_table_shows_the_sheet_title_and_year()
        test_refresh_defaults_does_not_overwrite_the_title()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
