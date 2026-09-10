"""Manual smoke test — no network calls. Verifies excel_io round-trip, PDF rendering,
and the orchestrator pipeline with a fake AIClient standing in for the real API."""

import shutil
from pathlib import Path

import openpyxl
import yaml

from excel_io import ExcelBatch
from state import ArticleState
from pdf_renderer import render_pdf


class FakeClient:
    def chat_completion(self, system_prompt, user_prompt, model=None, temperature=None):
        if "Outline Agent" in system_prompt or "outline" in user_prompt.lower():
            pass
        if "outline" in user_prompt.lower() and "headings" in user_prompt.lower():
            return "Background\nCurrent Approaches\nChallenges"
        if "###INTRODUCTION###" in user_prompt or "Introduction (~" in user_prompt:
            return "###INTRODUCTION###\n" + ("This is the introduction. " * 20) + \
                   "\n###CONCLUSION###\n" + ("This is the conclusion. " * 20)
        if "###ABSTRACT###" in user_prompt or "Produce:" in user_prompt:
            return "###ABSTRACT###\n" + ("This is the abstract. " * 30) + \
                   "\n###KEYWORDS###\nalpha, beta, gamma, delta, epsilon, zeta, eta, theta, iota, kappa"
        if "###VERDICT###" in user_prompt:
            return "###VERDICT###\nwithin_range\n###SECTIONS###\nnone"
        if "IEEE numbered format" in user_prompt:
            return "\n".join(f'[{i}] A. Author, "Fake paper {i}," in Proc. Fake Conf., 2024, pp. 1-5.' for i in range(1, 21))
        # writer agent
        return "This is drafted section content. " * 60


def test_excel_roundtrip(tmp_dir: Path):
    xlsx_path = tmp_dir / "sample.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Title", "Scope", "Author"])
    ws.append(["The Future of Quantum Cryptography", "Overview of post-quantum encryption standards.", "Dr. Jane Smith"])
    ws.append(["", "Missing title row", ""])
    ws.append(["Edge Computing Trends", "Survey of edge computing adoption in 2025.", ""])
    wb.save(xlsx_path)

    batch = ExcelBatch(xlsx_path)
    rows = list(batch.read_rows())
    assert len(rows) == 2, f"expected 2 valid rows, got {len(rows)}"
    batch.write_status(rows[0][0], "Success", "test note")
    batch.save()

    reopened = ExcelBatch(xlsx_path)
    rows2 = list(reopened.read_rows())
    assert rows2[0][4] == "Success", f"expected persisted status 'Success', got {rows2[0][4]!r}"
    print("excel_io round-trip: OK")


def test_pdf_render(tmp_dir: Path):
    state = ArticleState(
        row_number=1,
        title="The Future of Quantum Cryptography",
        scope="Overview of post-quantum encryption standards.",
        author="Dr. Jane Smith",
        target_word_count=500,
        target_sections=5,
    )
    state.outline = ["Background", "Current Approaches", "Challenges"]
    state.section_drafts = {h: ("Section content. " * 40) for h in state.outline}
    state.intro = "Introduction text. " * 20
    state.conclusion = "Conclusion text. " * 20
    state.abstract = "Abstract text. " * 30
    state.keywords = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]

    path = render_pdf(state, tmp_dir, {"font": "Times-Roman", "page_size": "A4"})
    assert path.exists() and path.stat().st_size > 0
    print(f"pdf render: OK ({path.name}, {path.stat().st_size} bytes)")


def test_orchestrator_pipeline(tmp_dir: Path):
    import orchestrator

    config = yaml.safe_load(open("config.yaml"))
    client = FakeClient()
    state = orchestrator.run_article(
        row_number=1,
        title="Edge Computing Trends",
        scope="Survey of edge computing adoption in 2025.",
        author="",
        word_count=800,
        sections=5,
        config=config,
        client=client,
        output_dir=tmp_dir,
    )
    assert state.status.startswith("Success"), f"expected Success status, got {state.status!r}: {state.notes}"
    assert len(state.keywords) == 10, f"expected 10 keywords, got {len(state.keywords)}"
    assert len(state.outline) == 3, f"expected 3 middle sections, got {len(state.outline)}"
    assert len(state.references) == 20, f"expected 20 references, got {len(state.references)}"
    print(f"orchestrator pipeline: OK (status={state.status}, notes={state.notes}, references={len(state.references)})")


if __name__ == "__main__":
    tmp_dir = Path("_manual_test_output")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()

    test_excel_roundtrip(tmp_dir)
    test_pdf_render(tmp_dir)
    test_orchestrator_pipeline(tmp_dir)

    print("\nAll manual checks passed.")
