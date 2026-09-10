import argparse
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from ai_client import AIClient, AIClientError
from excel_io import ExcelBatch
from orchestrator import run_article, prompt_article_targets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("run_log.txt"), logging.StreamHandler()],
)
logger = logging.getLogger("main")


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_tk_root():
    """Lazily create one hidden Tk root for the whole run instead of spinning up/tearing
    down the GUI subsystem per dialog — cuts repeated init/destroy overhead (CPU/battery)."""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        return root
    except Exception:
        return None


def resolve_excel_path(cli_arg: str | None, tk_root) -> Path:
    if cli_arg:
        path = Path(cli_arg)
    else:
        path = None
        if tk_root is not None:
            try:
                from tkinter import filedialog

                selected = filedialog.askopenfilename(
                    parent=tk_root, title="Select article assignments Excel file",
                    filetypes=[("Excel files", "*.xlsx")],
                )
                path = Path(selected) if selected else None
            except Exception:
                path = None

        if not path:
            path = Path(input("Path to the Excel (.xlsx) file: ").strip())

    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)
    return path


def resolve_output_folder(total_articles: int, tk_root) -> Path:
    print(f"This will save {total_articles} article PDFs. Where would you like them saved?")
    while True:
        raw = None
        if tk_root is not None:
            try:
                from tkinter import filedialog

                raw = filedialog.askdirectory(parent=tk_root, title="Choose output folder")
            except Exception:
                raw = None

        if not raw:
            raw = input("Output folder path: ").strip()

        folder = Path(raw)
        if not folder.exists():
            confirm = input(f"'{folder}' does not exist. Create it? [y/N]: ").strip().lower()
            if confirm != "y":
                continue
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"Could not create folder: {exc}")
                continue

        test_file = folder / ".write_test"
        try:
            test_file.write_text("ok")
            test_file.unlink()
        except OSError:
            print(f"No write permission for '{folder}'. Choose another folder.")
            continue

        return folder


def show_summary_popup(total: int, success: int, failed: int, excel_path: Path, tk_root):
    message = (
        f"Batch Complete\n"
        f"Total articles in Excel: {total}\n"
        f"Successfully written: {success}\n"
        f"Failed: {failed}\n"
        f'See "{excel_path.name}" for details.'
    )
    if tk_root is not None:
        try:
            from tkinter import messagebox

            messagebox.showinfo("Batch Complete", message, parent=tk_root)
            return
        except Exception:
            pass
    print(message)


def main():
    parser = argparse.ArgumentParser(description="AI-powered batch article writing automation")
    parser.add_argument("excel_path", nargs="?", help="Path to the .xlsx assignments file")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(Path(args.config))

    try:
        client = AIClient(config)
    except AIClientError as exc:
        print(f"Startup error: {exc}")
        sys.exit(1)

    tk_root = _get_tk_root()

    excel_path = resolve_excel_path(args.excel_path, tk_root)
    batch = ExcelBatch(excel_path)

    rows = list(batch.read_rows())
    total = len(rows)
    if total == 0:
        print("No valid rows to process (all rows missing Title/Scope, or file is empty).")
        sys.exit(0)

    pending_rows = [r for r in rows if r[4] != "Success"]
    if len(pending_rows) < total:
        print(f"{total - len(pending_rows)} row(s) already marked Success — skipping those.")

    output_dir = resolve_output_folder(len(pending_rows), tk_root)

    # Full-workbook rewrite on every row is the biggest disk/CPU cost in a long batch.
    # Default stays 1 (save every row = max crash-resumability); raise in config.yaml
    # to trade a little resumability for fewer disk writes on large batches.
    save_every = max(1, config.get("excel_save_every_n_rows", 1))
    rows_since_save = 0

    success_count = 0
    try:
        for i, (row_number, title, scope, author, existing_status) in enumerate(pending_rows, start=1):
            word_count, sections = prompt_article_targets(i, len(pending_rows), title, scope, author)
            state = run_article(row_number, title, scope, author, word_count, sections, config, client, output_dir)
            batch.write_status(row_number, state.status, state.notes)

            rows_since_save += 1
            is_last_row = i == len(pending_rows)
            if rows_since_save >= save_every or is_last_row:
                batch.save()
                rows_since_save = 0

            if state.status.startswith("Success"):
                success_count += 1
                print(f"  -> {state.status}")
            else:
                print(f"  -> Failed: {state.notes}")
    finally:
        if rows_since_save > 0:
            batch.save()  # flush on any early exit (Ctrl-C, etc.) so status isn't lost

    failed_count = len(pending_rows) - success_count
    show_summary_popup(total, success_count, failed_count, excel_path, tk_root)
    logger.info("Batch complete: %s total, %s success, %s failed", total, success_count, failed_count)

    if tk_root is not None:
        tk_root.destroy()


if __name__ == "__main__":
    main()
