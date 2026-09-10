"""Live resume test: runs the next N rows that are NOT already Success — the same
selection the GUI makes when you press Run Batch after a failure.

SAFETY: by default this works on a COPY of the sheet and writes PDFs to a temp folder,
so it can never clobber real progress or race the running app for the same file.
Pass --real to write to the actual sheet/output folder (only do this with the app closed).

Run: .venv/bin/python tests_live_resume.py [count] [--real]
"""

import logging
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import config_manager
from ai_client import AIClient
from excel_io import ExcelBatch
from orchestrator import run_article

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

REAL_EXCEL = Path("/Users/rajeevranjan/Downloads/Article/Citations Articles List.xlsx")
REAL_OUT = Path("/Users/rajeevranjan/Downloads/Article")
SANDBOX = Path("/tmp/resume_live_sandbox")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_real = "--real" in sys.argv
    count = int(args[0]) if args else 2

    if use_real:
        EXCEL, OUT = REAL_EXCEL, REAL_OUT
        print("!! WRITING TO THE REAL SHEET — make sure the app is closed !!\n")
    else:
        SANDBOX.mkdir(parents=True, exist_ok=True)
        EXCEL = SANDBOX / REAL_EXCEL.name
        OUT = SANDBOX
        shutil.copy2(REAL_EXCEL, EXCEL)
        print(f"Sandbox mode: copy of the sheet at {EXCEL}\n"
              f"(pass --real to write to the actual sheet)\n")

    config = config_manager.load_config()
    print(f"MODEL    : {config['ai_provider']['model']}")
    print(f"CHAIN    : {config.get('free_models')}")
    print(f"TIMEOUT  : {config['ai_provider']['article_timeout_seconds']}s/article")

    batch = ExcelBatch(EXCEL)
    all_rows = list(batch.read_rows())
    done = [r for r in all_rows if str(r[4]).startswith("Success")]
    todo = [r for r in all_rows if not str(r[4]).startswith("Success")]

    print(f"\nSheet: {len(all_rows)} rows | {len(done)} already Success | {len(todo)} to do")
    print(f"Resuming at row {todo[0][0]} — running {count} row(s)\n")

    client = AIClient(config)
    results = []
    save_every = max(1, config.get("excel_save_every_n_rows", 1))
    since_save = 0

    for row_number, title, scope, author, _status in todo[:count]:
        print(f"--- Row {row_number}: {title[:66]} ---")
        t0 = time.time()
        state = run_article(row_number, title, scope, author,
                            config["defaults"]["word_count"], config["defaults"]["sections"],
                            config, client, OUT)
        dt = time.time() - t0
        batch.write_status(row_number, state.status, state.notes)
        since_save += 1
        if since_save >= save_every:
            batch.save()
            since_save = 0
        print(f"    -> {state.status} in {dt:.1f}s via {client.last_model} "
              f"| {state.body_word_count()} words, {len(state.references)} refs\n")
        results.append((row_number, state.status, dt))

    if since_save:
        batch.save()

    ok = sum(1 for r in results if r[1].startswith("Success"))
    print("=" * 70)
    print(f"RESUME RESULT: {ok}/{len(results)} succeeded")
    for row_number, status, dt in results:
        print(f"  row {row_number:>3}  {status:<44} {dt:6.1f}s")

    reread = {r[0]: r[4] for r in ExcelBatch(EXCEL).read_rows()}
    print("\nPersisted status after run:")
    for row_number, _s, _d in results:
        print(f"  row {row_number:>3}  -> {reread.get(row_number)}")


if __name__ == "__main__":
    main()
