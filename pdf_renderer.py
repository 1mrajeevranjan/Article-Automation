import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from state import ArticleState


def sanitize_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", text).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or "untitled"


def _page_size(name: str):
    return A4 if name.upper() == "A4" else LETTER


# Maps a configured base font to its standard-14 bold/italic variants.
_FONT_VARIANTS = {
    "Times-Roman": {"regular": "Times-Roman", "bold": "Times-Bold", "italic": "Times-Italic"},
    "Helvetica": {"regular": "Helvetica", "bold": "Helvetica-Bold", "italic": "Helvetica-Oblique"},
    "Courier": {"regular": "Courier", "bold": "Courier-Bold", "italic": "Courier-Oblique"},
}

# Book Antiqua is a licensed Microsoft font not present on macOS. Palatino — shipped
# with macOS, and the typeface Book Antiqua was designed to match — is used in its place.
_PALATINO_TTC = "/System/Library/Fonts/Palatino.ttc"
_PALATINO_FACES = {"Palatino": 0, "Palatino-Italic": 1, "Palatino-Bold": 2, "Palatino-BoldItalic": 3}
_palatino_registered = False


def _ensure_palatino() -> bool:
    """Registers Palatino's four faces with reportlab. Returns False if unavailable."""
    global _palatino_registered
    if _palatino_registered:
        return True
    if not Path(_PALATINO_TTC).exists():
        return False
    try:
        for face_name, index in _PALATINO_FACES.items():
            pdfmetrics.registerFont(TTFont(face_name, _PALATINO_TTC, subfontIndex=index))
        _palatino_registered = True
        return True
    except Exception:
        return False


def _styles(font: str):
    if font in ("Palatino", "Book Antiqua") and _ensure_palatino():
        variants = {"regular": "Palatino", "bold": "Palatino-Bold", "italic": "Palatino-Italic"}
    else:
        variants = _FONT_VARIANTS.get(font, _FONT_VARIANTS["Times-Roman"])
    return {
        "title": ParagraphStyle("Title", fontName=variants["bold"],
                                 fontSize=18, alignment=TA_CENTER, spaceAfter=6),
        "author": ParagraphStyle("Author", fontName=variants["italic"],
                                  fontSize=12, alignment=TA_CENTER, spaceAfter=14),
        "label": ParagraphStyle("Label", fontName=variants["bold"],
                                 fontSize=11, alignment=TA_LEFT, spaceBefore=10, spaceAfter=4),
        "abstract": ParagraphStyle("Abstract", fontName=variants["regular"], fontSize=11, alignment=TA_JUSTIFY,
                                    leftIndent=20, rightIndent=20, spaceAfter=10),
        "keywords": ParagraphStyle("Keywords", fontName=variants["italic"],
                                    fontSize=10, alignment=TA_LEFT, spaceAfter=14),
        "heading": ParagraphStyle("Heading", fontName=variants["bold"],
                                   fontSize=14, alignment=TA_LEFT, spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("Body", fontName=variants["regular"], fontSize=11, alignment=TA_JUSTIFY, spaceAfter=8, leading=15),
        "reference": ParagraphStyle("Reference", fontName=variants["regular"], fontSize=10, alignment=TA_LEFT, spaceAfter=4, leading=13),
        "disclaimer": ParagraphStyle("Disclaimer", fontName=variants["italic"], fontSize=9, alignment=TA_LEFT, spaceBefore=8, textColor="#B00020"),
    }


def _make_footer(font_name: str):
    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 9)
        canvas.drawCentredString(doc.pagesize[0] / 2, 0.5 * inch, str(canvas.getPageNumber()))
        canvas.restoreState()
    return _footer


def render_pdf(state: ArticleState, output_dir: Path, pdf_cfg: dict, file_label: str | None = None) -> Path:
    """file_label overrides the filename prefix — used for regenerations (e.g. "7.1", "7.2")."""
    font = pdf_cfg.get("font", "Palatino")
    page_size = _page_size(pdf_cfg.get("page_size", "A4"))
    styles = _styles(font)
    footer_font = styles["body"].fontName

    label = file_label if file_label is not None else str(state.row_number)
    filename = f"{label}_{sanitize_filename(state.title)}.pdf"
    output_path = output_dir / filename

    doc = SimpleDocTemplate(str(output_path), pagesize=page_size,
                             topMargin=1 * inch, bottomMargin=1 * inch,
                             leftMargin=1 * inch, rightMargin=1 * inch)
    story = [Paragraph(escape(state.title), styles["title"])]
    if state.author:
        story.append(Paragraph(escape(state.author), styles["author"]))
    else:
        story.append(Spacer(1, 10))

    story.append(Paragraph("Abstract", styles["label"]))
    story.append(Paragraph(escape(state.abstract), styles["abstract"]))

    story.append(Paragraph(f"<b>Keywords:</b> {escape(', '.join(state.keywords))}", styles["keywords"]))

    story.append(Paragraph("Introduction", styles["heading"]))
    story.append(Paragraph(escape(state.intro), styles["body"]))

    for heading, text in state.section_drafts.items():
        story.append(Paragraph(escape(heading), styles["heading"]))
        story.append(Paragraph(escape(text), styles["body"]))

    story.append(Paragraph("Conclusion", styles["heading"]))
    story.append(Paragraph(escape(state.conclusion), styles["body"]))

    if state.references:
        story.append(Paragraph("References", styles["heading"]))
        story.append(Paragraph(
            "AI-generated for illustrative structure only — NOT verified real sources. "
            "Verify or replace every citation before any academic submission.",
            styles["disclaimer"],
        ))
        for ref in state.references:
            story.append(Paragraph(escape(ref), styles["reference"]))

    footer = _make_footer(footer_font)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output_path
