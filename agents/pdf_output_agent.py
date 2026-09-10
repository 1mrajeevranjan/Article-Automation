from pathlib import Path

from state import ArticleState
from pdf_renderer import render_pdf


def run(state: ArticleState, config: dict, output_dir: Path, file_label: str | None = None) -> ArticleState:
    """Pure code, no AI call: renders the PDF and finalizes status/notes."""
    path = render_pdf(state, output_dir, config.get("pdf", {}), file_label=file_label)

    target = state.target_word_count
    tolerance = config.get("validation", {}).get("word_count_tolerance_percent", 10)
    min_words = int(target * (1 - tolerance / 100))
    max_words = int(target * (1 + tolerance / 100))
    current = state.body_word_count()

    if min_words <= current <= max_words:
        state.status = "Success"
        state.notes = f"Saved to {path.name}"
    else:
        state.status = "Success (word count out of range)"
        state.notes = f"{current} words vs target {target} ({min_words}-{max_words}). Saved to {path.name}"

    return state
