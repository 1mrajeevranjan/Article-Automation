"""Reads article assignments from a workbook and writes per-row status back.

Real sheets rarely arrive in one canonical shape: the header row is often preceded by a
title or author line, and the "scope" column shows up as Synopsis, Brief, Description or
Abstract depending on who made the file. Hardcoding "headers live on row 1, and the
column is literally called Scope" silently produced "Loaded 0 rows" on perfectly good
sheets, so the header is located and its columns are matched by synonym instead.
"""

from pathlib import Path

import openpyxl

# Accepted spellings for each logical column, lower-cased. Order matters only for
# readability; matching is exact-then-substring against the header cell text.
COLUMN_SYNONYMS = {
    "title": (
        "title", "article title", "proposed article title", "article",
        "topic", "paper title", "headline", "subject",
    ),
    "scope": (
        "scope", "synopsis", "brief scope", "brief", "description", "summary",
        "abstract", "details", "outline", "idea", "notes on scope",
    ),
    "author": ("author", "writer", "by", "authors", "author name"),
    "status": ("status",),
    "notes": ("notes", "note"),
}

# How far down to look for the header row before giving up.
HEADER_SEARCH_DEPTH = 10


class MissingColumnsError(ValueError):
    """Raised when a sheet has no recognisable Title/Scope columns."""


def _match_column(header_text: str) -> str | None:
    """Maps one header cell to a logical column name, or None."""
    text = str(header_text).strip().lower()
    if not text:
        return None
    for logical, synonyms in COLUMN_SYNONYMS.items():
        if text in synonyms:
            return logical
    # Fall back to substring matching so "Article Title (draft)" still resolves.
    for logical, synonyms in COLUMN_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in text:
                return logical
    return None


class ExcelBatch:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.workbook = openpyxl.load_workbook(self.path)
        self.sheet = self.workbook.active
        self.header_row, self._col_index = self._locate_header()
        self._ensure_status_notes_columns()

    # ------------------------------------------------------------------ header

    def _index_row(self, row: int) -> dict[str, int]:
        """Logical column name -> column number, for one candidate header row."""
        index: dict[str, int] = {}
        for col in range(1, self.sheet.max_column + 1):
            value = self.sheet.cell(row=row, column=col).value
            if value is None:
                continue
            logical = _match_column(value)
            if logical and logical not in index:
                index[logical] = col
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
            if "title" in index and "scope" in index:
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
                self._col_index[name] = new_col

    # -------------------------------------------------------------------- rows

    def _cell_value(self, row: int, key: str) -> str:
        col = self._col_index.get(key)
        if col is None:
            return ""
        value = self.sheet.cell(row=row, column=col).value
        return str(value).strip() if value is not None else ""

    def _is_repeated_header(self, title: str, scope: str) -> bool:
        """Sheets that group articles per author repeat the header above each group.

        Those rows have real text in both columns, so they otherwise sail through as
        articles titled "Title" — inflating the row count and generating a junk PDF.
        """
        return (title.strip().lower() in COLUMN_SYNONYMS["title"]
                and scope.strip().lower() in COLUMN_SYNONYMS["scope"])

    def read_rows(self):
        """Yields (row_number, title, scope, author, existing_status). Skips invalid rows."""
        for row in range(self.header_row + 1, self.sheet.max_row + 1):
            title = self._cell_value(row, "title")
            scope = self._cell_value(row, "scope")
            author = self._cell_value(row, "author")
            status = self._cell_value(row, "status")

            if self._is_repeated_header(title, scope):
                continue

            if not title or not scope:
                if title or scope:  # only touch genuinely present-but-incomplete rows
                    self.write_status(row, "Failed", "Missing required field (Title or Scope)")
                continue

            yield row, title, scope, author, status

    def write_status(self, row: int, status: str, notes: str):
        self.sheet.cell(row=row, column=self._col_index["status"], value=status)
        self.sheet.cell(row=row, column=self._col_index["notes"], value=notes)

    def save(self):
        self.workbook.save(self.path)
