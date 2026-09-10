"""Per-batch runner controls: each batch gets its own Run button, Stop button and
progress bar, and starting one never blocks starting another.

This replaces the old single-batch Run button, whose guard ("a single-batch run is
already going in this tab") blocked parallel launches outright.

Run: .venv/bin/python tests_runbutton_check.py
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

TMP = Path("/tmp/runbutton_check")


def make_sheet(path, rows=30):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Title", "Scope", "Status", "Notes"])
    for i in range(rows):
        ws.append([f"Article {i+1}", f"Scope {i+1}", None, None])
    wb.save(path)


def slow_run_article(row_number, title, scope, author, word_count, sections,
                     config, client, output_dir, file_label=None, year=None):
    from state import ArticleState
    time.sleep(0.05)
    st = ArticleState(row_number=row_number, title=title, scope=scope, author=author,
                      target_word_count=word_count, target_sections=sections)
    st.status = "Success"
    st.notes = f"Saved to {row_number}_x.pdf"
    return st


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    sheet = TMP / "s.xlsx"
    make_sheet(sheet, 30)

    gui_app.messagebox.showwarning = lambda *a, **k: None
    gui_app.messagebox.showinfo = lambda *a, **k: None
    gui_app.messagebox.showerror = lambda *a, **k: None

    original = batch_runner.run_article
    batch_runner.run_article = slow_run_article
    try:
        app = gui_app.App()
        app.update()
        tab = app.sheet_tabs[0]
        tab.excel_path = sheet
        tab.output_dir = TMP
        tab.batch_size_var.set("10")
        tab._load_rows()
        tab.parallel_model_var.set("model-x:free")
        app.update()

        # Start batch 1 on its own, then batch 2 while 1 is still going. The old code
        # raised "a single-batch run is already going in this tab" here.
        tab._start_one_batch(1)
        app.update()
        tab._start_one_batch(2)
        app.update()

        r1, r2 = tab.runner_rows[1], tab.runner_rows[2]
        print(f"batch 1 toggle: {r1['toggle'].cget('text')}  batch 2 toggle: {r2['toggle'].cget('text')}")

        assert r1["job_id"] is not None and r2["job_id"] is not None, "both batches must start"
        assert r1["running"] and r2["running"], "both toggles should read as running"
        assert r1["toggle"].cget("text") == "■" and r2["toggle"].cget("text") == "■", \
            "a running batch's toggle must show the stop glyph"
        assert len(app.job_manager.jobs) == 2, app.job_manager.jobs
        print("two batches started concurrently, each with its own play/stop toggle: OK")

        # Stop ONLY batch 1; batch 2 must keep going.
        tab._stop_one_batch(1)
        # Wait for the UI to finish draining events, not merely for the threads to stop:
        # the closing job_done events arrive after is_alive() flips false.
        deadline = time.time() + 30
        while time.time() < deadline and (app.job_manager.any_running() or tab._awaiting_final):
            app.update()
            time.sleep(0.05)

        job1 = app.job_manager.jobs[r1["job_id"]]
        job2 = app.job_manager.jobs[r2["job_id"]]
        print(f"batch 1 status: {job1.status} · batch 2 status: {job2.status}")
        assert job2.status == "Done", f"batch 2 should finish, got {job2.status}"
        print("stopping batch 1 left batch 2 running to completion: OK")

        # Toggle returns to play state, and progress bar advanced.
        app.update()
        assert not r2["running"], "toggle should read as idle once the batch finishes"
        assert r2["toggle"].cget("text") == "▶", "finished batch's toggle must show play again"
        assert r2["bar"].cget("value") > 0, "progress bar should have advanced"
        print(f"progress bar for batch 2 reached {r2['bar'].cget('value')}/"
              f"{r2['bar'].cget('maximum')}, toggle back to play: OK")

        log_text = tab.log.get("1.0", "end")
        assert "[batch 1]" in log_text and "[batch 2]" in log_text, log_text[-400:]
        print("combined log tags entries by batch: OK")

        app.destroy()
    finally:
        batch_runner.run_article = original
        shutil.rmtree(TMP, ignore_errors=True)

    print("\nPER-BATCH RUNNER CONTROLS: OK")


if __name__ == "__main__":
    main()
