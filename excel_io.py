"""Reads article assignments from a workbook and writes per-row status back.

Real sheets rarely arrive in one canonical shape: the header row is often preceded by a
title or author line, and the "scope" column shows up as Synopsis, Brief, Description or
Abstract depending on who made the file. Hardcoding "headers live on row 1, and the
column is literally called Scope" silently produced "Loaded 0 rows" on perfectly good
sheets, so the header is located and its columns are matched by synonym instead.
"""

import datetime
import re
from pathlib import Path
from typing import NamedTuple

import openpyxl

# Accepted spellings for each logical column, lower-cased. Order matters twice over:
# a synonym earlier in its tuple outranks a later one, so a sheet carrying both
# "Synopsis" and "Abstract" resolves scope to Synopsis rather than to whichever
# happens to sit further left.
COLUMN_SYNONYMS = {
    "title": (
        "title", "article title", "proposed article title", "paper title",
        "headline", "topic", "article",
    ),
    "scope": (
        "synopsis", "scope", "brief scope", "brief", "description", "summary",
        "abstract", "details", "outline", "idea", "notes on scope",
    ),
    "author": (
        "author names", "author name", "authors", "author", "co-authors",
        "co-author", "writer", "by",
    ),
    "year": (
        "year", "publication year", "pub year", "target year", "cutoff year",
        "as of year", "up to year",
    ),
    # Recognised so it can be deliberately ignored. "Subject" used to be a title
    # synonym, which meant a sheet whose first column was "Subject Area" generated
    # every article from the subject instead of the title.
    "subject": ("subject area", "subject", "domain", "discipline", "category", "field"),
    "status": ("status",),
    "notes": ("notes", "note"),
}

# Only these two drive generation. Everything else is metadata: year constrains the
# content and citations, author is printed on the PDF, subject is ignored entirely.
CONTENT_COLUMNS = ("title", "scope")

# How far down to look for the header row before giving up.
HEADER_SEARCH_DEPTH = 10

_YEAR_PATTERN = re.compile(r"\b(1[89]\d{2}|20\d{2}|21\d{2})\b")


class ArticleRow(NamedTuple):
    """One assignment. Indices 0-4 are unchanged from the original 5-tuple, so
    positional access elsewhere (r[0], r[4]) keeps working."""
    row_number: int
    title: str
    scope: str
    author: str
    status: str
    year: int | None = None


class MissingColumnsError(ValueError):
    """Raised when a sheet has no recognisable Title/Scope columns."""


def _match_column(header_text: str) -> tuple[str, int] | None:
    """Maps one header cell to (logical column, confidence), or None.

    Confidence exists because substring matching is necessary but dangerous: it lets
    "Article Title (draft)" resolve, and it also let "Subject Area" claim the title
    column ahead of the real, exactly-named Title beside it. Scoring an exact header
    above a partial one — and an earlier synonym above a later one — settles that
    without giving up the flexibility.
    """
    text = str(header_text).strip().lower()
    if not text:
        return None

    best: tuple[str, int] | None = None
    for logical, synonyms in COLUMN_SYNONYMS.items():
        for rank, synonym in enumerate(synonyms):
            if text == synonym:
                score = 1000 - rank
            elif synonym in text:
                # Prefer the longest matching synonym: "author names" beats "author".
                score = 500 - rank + len(synonym)
            else:
                continue
            if best is None or score > best[1]:
                best = (logical, score)
    return best


def parse_year(value) -> int | None:
    """A year cell arrives as 2023, 2023.0, '2023', 'by 2023' or a date. All mean 2023."""
    if value is None:
        return None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.year
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        year = int(value)
        return year if 1800 <= year <= 2199 else None
    match = _YEAR_PATTERN.search(str(value))
    return int(match.group(1)) if match else None


class ExcelBatch:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.workbook = openpyxl.load_workbook(self.path)
        self.sheet = self.workbook.active
        self.header_row, self._col_index = self._locate_header()
        self._ensure_status_notes_columns()

    # ------------------------------------------------------------------ header

    def _index_row(self, row: int) -> dict[str, list[int]]:
        """Logical column name -> column numbers, for one candidate header row.

        A list rather than a single column because author is routinely split across
        several ("Author 1", "Author 2", "Co-authors") and every name has to reach the
        PDF. For every other logical column only the best-scoring match is kept.
        """
        scored: dict[str, list[tuple[int, int]]] = {}   # logical -> [(score, col)]
        for col in range(1, self.sheet.max_column + 1):
            value = self.sheet.cell(row=row, column=col).value
            if value is None:
                continue
            match = _match_column(value)
            if match:
                scored.setdefault(match[0], []).append((match[1], col))

        index: dict[str, list[int]] = {}
        for logical, hits in scored.items():
            if logical == "author":
                index[logical] = [col for _, col in hits]   # keep sheet order
            else:
                # Highest score wins; ties go to the leftmost column.
                index[logical] = [min(hits, key=lambda h: (-h[0], h[1]))[1]]
        return index

    def _locate_header(self) -> tuple[int, dict[str, int]]:
        """Finds the first row that carries both a title and a scope column.

        Sheets commonly open with a stray title/author line, so row 1 is a guess, not a
        guarantee. The first row containing both required columns wins.
        """
        best_partial: tuple[int, dict[str, int]] | None = None
        depth = min(HEADER_SEARCH_DEPTH, self.sheet.max_row)

        for row in range(1, depth + 1):
            index = self._index_row(row)
            if all(name in index for name in CONTENT_COLUMNS):
                return row, index
            if index and best_partial is None:
                best_partial = (row, index)

        found = sorted(best_partial[1]) if best_partial else []
        raise MissingColumnsError(
            f"{self.path.name}: could not find Title and Scope columns in the first "
            f"{depth} rows"
            + (f" (recognised only: {', '.join(found)})" if found else "")
            + ". Accepted names — Title: "
            + ", ".join(COLUMN_SYNONYMS['title'][:4])
            + "; Scope: "
            + ", ".join(COLUMN_SYNONYMS['scope'][:5])
            + "."
        )

    def _ensure_status_notes_columns(self):
        for name in ("status", "notes"):
            if name not in self._col_index:
                new_col = self.sheet.max_column + 1
                self.sheet.cell(row=self.header_row, column=new_col, value=name.capitalize())
                self._col_index[name] = [new_col]

    # -------------------------------------------------------------------- rows

    def _column(self, key: str) -> int | None:
        cols = self._col_index.get(key)
        return cols[0] if cols else None

    def _cell_value(self, row: int, key: str) -> str:
        col = self._column(key)
        if col is None:
            return ""
        value = self.sheet.cell(row=row, column=col).value
        return str(value).strip() if value is not None else ""

    def _author_names(self, row: int) -> str:
        """Every name from every author column, joined for the PDF byline.

        Sheets split authors across columns as often as they pack them into one, and a
        dropped co-author on a paper's byline is not a cosmetic defect.
        """
        names: list[str] = []
        for col in self._col_index.get("author", []):
            value = self.sheet.cell(row=row, column=col).value
            if value is None:
                continue
            for name in re.split(r"[;,\n]| and ", str(value)):
                name = name.strip()
                if name and name not in names:
                    names.append(name)
        return ", ".join(names)

    def _is_repeated_header(self, title: str, scope: str) -> bool:
        """Sheets that group articles per author repeat the header above each group.

        Those rows have real text in both columns, so they otherwise sail through as
        articles titled "Title" — inflating the row count and generating a junk PDF.
        """
        return (title.strip().lower() in COLUMN_SYNONYMS["title"]
                and scope.strip().lower() in COLUMN_SYNONYMS["scope"])

    def read_rows(self):
        """Yields ArticleRow per valid row. Only Title and Scope feed generation."""
        for row in range(self.header_row + 1, self.sheet.max_row + 1):
            title = self._cell_value(row, "title")
            scope = self._cell_value(row, "scope")

            if self._is_repeated_header(title, scope):
                continue

            if not title or not scope:
                if title or scope:  # only touch genuinely present-but-incomplete rows
                    self.write_status(row, "Failed", "Missing required field (Title or Scope)")
                continue

            year = None
            year_col = self._column("year")
            if year_col is not None:
                year = parse_year(self.sheet.cell(row=row, column=year_col).value)

            yield ArticleRow(
                row_number=row,
                title=title,
                scope=scope,
                author=self._author_names(row),
                status=self._cell_value(row, "status"),
                year=year,
            )

    def write_status(self, row: int, status: str, notes: str):
        self.sheet.cell(row=row, column=self._column("status"), value=status)
        self.sheet.cell(row=row, column=self._column("notes"), value=notes)

    def save(self):
        self.workbook.save(self.path)
