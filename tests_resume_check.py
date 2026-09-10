"""Reproduces the reported failure and verifies resume behaviour, headless.

Scenario (matches what happened live):
  - rows 2..13 succeed, then the provider dies
  - remaining rows fail
  - the circuit breaker halts the batch early
  - re-rendering the batch shows finished rows as Done + unticked
  - pressing Run Batch again runs ONLY the unfinished rows

Run: .venv/bin/python tests_resume_check.py
"""

import os
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import openpyxl

os.environ.setdefault("AI_API_KEY", "test-key")

from dotenv import load_dotenv

load_dotenv()

import gui_app
from excel_io import ExcelBatch

TMP = Path("/tmp/resume_check")


def build_sheet(path: Path, rows: int = 25):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Title", "Scope", "Status", "Notes"])
    for i in range(rows):
        ws.append([f"Article {i + 1}", f"Scope for article {i + 1}", None, None])
    wb.save(path)


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    xlsx = TMP / "assignments.xlsx"
    build_sheet(xlsx)

    # Simulate the live run: rows 2..13 succeeded and were written to the sheet.
    batch = ExcelBatch(xlsx)
    for row_number in range(2, 14):
        batch.write_status(row_number, "Success", f"Saved to {row_number}_Article.pdf")
    for row_number in range(14, 20):
        batch.write_status(row_number, "Failed", "Exceeded overall article timeout (600s)")
    batch.save()

    root = tk.Tk()
    root.geometry("1000x700")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    tab = gui_app.BatchTab(nb)
    nb.add(tab, text="Batch")

    # Drive the tab exactly as choosing a file does.
    tab.excel_path = xlsx
    tab.output_dir = TMP
    tab._load_rows()
    root.update()

    rendered = {}
    for iid in tab.tree.get_children():
        vals = tab.tree.item(iid, "values")
        rendered[int(vals[gui_app.COL["row"]])] = {
            "tick": vals[gui_app.COL["include"]],
            "status": vals[gui_app.COL["status"]],
        }

    print("Rendered batch 1 (rows 2-26):")
    for row_number in sorted(rendered)[:20]:
        r = rendered[row_number]
        print(f"  row {row_number:>3}  tick={r['tick']}  status={r['status']}")

    # --- assertions --------------------------------------------------------
    for row_number in range(2, 14):
        assert rendered[row_number]["status"] == "Done", \
            f"row {row_number} should show Done, got {rendered[row_number]['status']}"
        assert rendered[row_number]["tick"] == gui_app.UNCHECKED, \
            f"row {row_number} is finished and must be unticked"
    print("\n✓ finished rows 2-13 render as Done and are unticked")

    for row_number in range(14, 20):
        assert rendered[row_number]["status"] == "Failed", rendered[row_number]
        assert rendered[row_number]["tick"] == gui_app.CHECKED, \
            f"row {row_number} failed and must stay ticked for resume"
    print("✓ failed rows 14-19 stay ticked so they re-run")

    for row_number in range(20, 27):
        assert rendered[row_number]["status"] == "Pending", rendered[row_number]
        assert rendered[row_number]["tick"] == gui_app.CHECKED
    print("✓ untouched rows 20-26 remain Pending and ticked")

    selected = [r for r in tab.pending_rows if tab.included.get(r[0], False)]
    selected_rows = sorted(r[0] for r in selected)
    assert selected_rows == list(range(14, 27)), selected_rows
    print(f"✓ Run Batch would process exactly rows {selected_rows[0]}-{selected_rows[-1]} "
          f"({len(selected_rows)} rows) — resumes at the failure point")

    label = tab.batch_choice_var.get()
    assert "12 done" in label, label
    print(f"✓ dropdown reflects real progress: {label!r}")

    root.destroy()
    shutil.rmtree(TMP)
    print("\nRESUME BEHAVIOUR: OK")


if __name__ == "__main__":
    main()
