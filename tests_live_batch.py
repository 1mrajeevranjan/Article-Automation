"""Live end-to-end test: generates several real articles through the full pipeline,
exactly as the GUI does, and reports per-article outcome + which model served it.

Run: .venv/bin/python tests_live_batch.py [count]
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

EXCEL = "/Users/rajeevranjan/Downloads/Article/Citations Articles List.xlsx"
OUT = Path("/tmp/live_batch_out")


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    config = config_manager.load_config()
    print(f"MODEL      : {config['ai_provider']['model']}")
    print(f"FALLBACKS  : {config.get('free_models')}")
    print(f"ROUTING    : {config['ai_provider'].get('provider_routing')}")
    print(f"STYLE      : {config.get('writing_style')}\n")

    client = AIClient(config)

    # Read the real assignments, take the first `count` rows — the same ones that failed.
    batch = ExcelBatch(Path(EXCEL))
    rows = list(batch.read_rows())[:count]

    results = []
    t_all = time.time()
    for row_number, title, scope, author, _status in rows:
        print(f"\n--- Row {row_number}: {title[:70]} ---")
        t0 = time.time()
        state = run_article(row_number, title, scope, author,
                            config["defaults"]["word_count"], config["defaults"]["sections"],
                            config, client, OUT)
        dt = time.time() - t0
        served = client.last_model
        print(f"    -> {state.status} in {dt:.1f}s via {served} | {state.body_word_count()} words, "
              f"{len(state.references)} refs")
        if not state.status.startswith("Success"):
            print(f"    NOTES: {state.notes}")
        results.append((row_number, state.status, dt, served))

    total = time.time() - t_all
    ok = sum(1 for r in results if r[1].startswith("Success"))

    print("\n" + "=" * 74)
    print(f"RESULT: {ok}/{len(results)} succeeded in {total:.1f}s "
          f"(avg {total / max(len(results),1):.1f}s/article)")
    for row_number, status, dt, served in results:
        print(f"  row {row_number:>3}  {status:<42} {dt:6.1f}s  {served}")

    pdfs = sorted(OUT.glob("*.pdf"))
    print(f"\nPDFs written: {len(pdfs)}")
    for p in pdfs:
        print(f"  {p.name}  ({p.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
