"""Checks the two correctness fixes and the layout changes:

  1. a row that finishes in ANY running batch updates the table immediately
     (it used to stay "Pending" long after the log said Success)
  2. the inspect line and batch dropdown counts track reality, not the load-time snapshot
  3. Auto model spread gives each parallel batch a different model
  4. layout: log lives in the right column, batches sit side by side, columns centred

Run: .venv/bin/python tests_live_ui_check.py
"""

import os
import shutil
import time
from pathlib import Path

import openpyxl

os.environ.setdefault("AI_API_KEY", "test-key")

from dotenv import load_dotenv

load_dotenv()

import batch_runner
import gui_app

import usage_tracker
usage_tracker.use_isolated_state()   # never count test calls against the real daily allowance

TMP = Path("/tmp/live_ui_check")


def make_sheet(path, rows=20):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Title", "Scope", "Status", "Notes"])
    for i in range(rows):
        ws.append([f"Article {i+1}", f"Scope {i+1}", None, None])
    wb.save(path)


def fake_run_article(row_number, title, scope, author, word_count, sections,
                     config, client, output_dir, file_label=None, year=None):
    from state import ArticleState
    time.sleep(0.03)
    st = ArticleState(row_number=row_number, title=title, scope=scope, author=author,
                      target_word_count=word_count, target_sections=sections)
    st.status = "Success"
    st.notes = f"Saved to {row_number}_x.pdf"
    return st


def test_row_status_updates_live():
    sheet = TMP / "live.xlsx"
    make_sheet(sheet, 20)

    app = gui_app.App()
    app.update()
    tab = app.sheet_tabs[0]
    tab.excel_path = sheet
    tab.output_dir = TMP
    tab.batch_size_var.set("10")
    tab._load_rows()
    app.update()

    # Batch 1 (rows 2-11) is the one on screen. Run batch 1 and watch the table.
    displayed = [int(tab.tree.item(i, "values")[1]) for i in tab.tree.get_children()]
    assert displayed[0] == 2, displayed[:3]
    assert tab.tree.item("2", "values")[gui_app.COL["status"]] == "Pending", tab.tree.item("2", "values")

    tab._start_one_batch(1, model="m1:free")

    # Poll until at least a few rows have landed, checking the table mid-flight.
    seen_done_midflight = False
    deadline = time.time() + 30
    while time.time() < deadline and app.job_manager.any_running():
        app.update()
        if tab.tree.item("2", "values")[gui_app.COL["status"]] == "Done":
            seen_done_midflight = True
            break
        time.sleep(0.05)

    assert seen_done_midflight, "row 2 still read Pending while the batch was running"
    print("finished rows flip to Done in the table while the batch is still running: OK")

    while app.job_manager.any_running() or tab._awaiting_final:
        app.update()
        time.sleep(0.05)
    app.update()

    label = tab.remaining_label.cget("text")
    dropdown = tab.batch_choice_var.get()
    assert "10 done" in label, f"inspect line stale: {label!r}"
    assert "10 done" in dropdown, f"dropdown stale: {dropdown!r}"
    print(f"progress figures track reality — inspect: {label!r}, dropdown: {dropdown!r}: OK")

    ticked = [r for r, keep in tab.included.items() if keep]
    assert not ticked, f"finished rows should untick themselves, still ticked: {ticked}"
    print("finished rows untick themselves so a re-run resumes correctly: OK")
    app.destroy()


def test_auto_model_spread():
    sheet = TMP / "spread.xlsx"
    make_sheet(sheet, 40)

    app = gui_app.App()
    app.update()
    tab = app.sheet_tabs[0]
    tab.excel_path = sheet
    tab.output_dir = TMP
    tab.batch_size_var.set("10")
    tab._load_rows()
    tab.parallel_model_var.set(gui_app.AUTO_MODEL)
    tab.parallel_spec_var.set("1-4")
    app.update()

    assigned = tab._models_for([1, 2, 3, 4])
    distinct = set(assigned.values())
    pool = gui_app.config_manager.load_config().get("free_models") or []
    expected = min(len(pool), 4)
    assert len(distinct) == expected, f"expected {expected} distinct models, got {assigned}"
    print(f"Auto spread assigned {len(distinct)} different models across 4 batches: "
          f"{ {k: v.split('/')[-1] for k, v in assigned.items()} }: OK")

    # An explicit choice must still pin every batch to that one model.
    tab.parallel_model_var.set("pinned:free")
    pinned = tab._models_for([1, 2, 3])
    assert set(pinned.values()) == {"pinned:free"}, pinned
    print("explicit model choice still pins every batch to it: OK")
    app.destroy()


def test_runner_grid_reflows_with_pane_width():
    """Regression: dragging the sash to widen the log narrowed the left pane, and the
    fixed 2-column runner grid clipped batches instead of wrapping to 1 column."""
    app = gui_app.App()
    app.geometry("1400x850")
    app.update()
    tab = app.sheet_tabs[0]
    for n in (1, 2, 3, 4):
        tab._ensure_runner_row(n)

    # Wide pane: expect 2 columns.
    tab._split.sashpos(0, 900)
    for _ in range(15):
        app.update()
        time.sleep(0.03)
    tab._relayout_runners()
    app.update()
    wide_cols = len({row["frame"].grid_info()["column"] for row in tab.runner_rows.values()})
    assert wide_cols >= 2, f"expected multiple columns when wide, got {wide_cols}"

    # Narrow the pane (log takes most of the width) — runners must wrap to 1 column,
    # not get clipped off the right edge.
    tab._split.sashpos(0, 220)
    for _ in range(15):
        app.update()
        time.sleep(0.03)
    tab._relayout_runners()
    app.update()
    narrow_cols = {row["frame"].grid_info()["column"] for row in tab.runner_rows.values()}
    assert narrow_cols == {0}, f"expected a single column when narrow, got columns {narrow_cols}"
    print(f"runner grid reflows from {wide_cols} columns wide to 1 column narrow: OK")
    app.destroy()


def test_layout():
    app = gui_app.App()
    app.update()
    tab = app.sheet_tabs[0]

    # The log lives in the second (right-hand) pane of the draggable split, and that
    # pane must be wide enough to actually read a log line.
    panes = tab._split.panes()
    assert len(panes) == 2, panes
    right_pane = str(tab.log.master.master)
    assert right_pane == str(panes[1]), f"log should sit in the right pane: {right_pane} vs {panes}"

    app.geometry("1400x850")
    for _ in range(20):
        app.update()
        time.sleep(0.05)
    log_px = tab.log.winfo_width()
    assert log_px > 380, f"log pane only {log_px}px wide — too narrow to read"
    print(f"combined log occupies the right pane and is {log_px}px wide (draggable): OK")

    for n in (1, 2, 3, 4):
        tab._ensure_runner_row(n)
    app.update()
    positions = {n: (row["frame"].grid_info()["row"], row["frame"].grid_info()["column"])
                 for n, row in tab.runner_rows.items()}
    assert positions[1][0] == positions[2][0], f"batches 1 and 2 should share a row: {positions}"
    assert positions[1][1] != positions[2][1], positions
    print(f"batch runners laid out side by side: {positions}: OK")

    for col in ("include", "row", "title", "word_count", "sections", "status"):
        anchor = str(tab.tree.column(col, "anchor"))
        assert anchor == "center", f"column {col} anchor is {anchor}, expected center"
    print("every table column centred, including Title: OK")

    toggle = tab.runner_rows[1]["toggle"]
    assert str(toggle.cget("text")) == "▶", "idle batch should show play"
    assert toggle.cget("fg") == "#1a7f37", "play state should be green"
    tab._set_toggle_running(tab.runner_rows[1], True)
    assert str(toggle.cget("text")) == "■", "running batch should show stop"
    assert toggle.cget("fg") == "#b3261e", "stop state should be red"
    tab._set_toggle_running(tab.runner_rows[1], False)
    print("single toggle switches between green ▶ play and red ■ stop: OK")
    app.destroy()


if __name__ == "__main__":
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    gui_app.messagebox.showwarning = lambda *a, **k: None
    gui_app.messagebox.showinfo = lambda *a, **k: None
    gui_app.messagebox.showerror = lambda *a, **k: None
    gui_app.messagebox.askyesno = lambda *a, **k: True

    original = batch_runner.run_article
    batch_runner.run_article = fake_run_article
    try:
        test_row_status_updates_live()
        test_auto_model_spread()
        test_layout()
        test_runner_grid_reflows_with_pane_width()
    finally:
        batch_runner.run_article = original
        shutil.rmtree(TMP, ignore_errors=True)

    print("\nAll live-UI checks passed.")
