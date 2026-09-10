"""Desktop GUI for the article automation pipeline — edit settings and run batches
without touching the terminal. Entry point for the packaged .app (see setup.py)."""

import logging
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from dotenv import load_dotenv

import config_manager
from ai_client import AIClient, AIClientError, probe_models
from batch_runner import JobManager, MAX_PARALLEL_JOBS, partition_batches
from excel_io import ExcelBatch, MissingColumnsError
from orchestrator import run_article
from style_guides import STYLES

PDF_FONTS = ["Palatino", "Times-Roman", "Helvetica", "Courier"]
PDF_PAGE_SIZES = ["A4", "LETTER"]

CHECKED = "☑"
UNCHECKED = "☐"
AUTO_MODEL = "Auto — spread across models"

# The rows table's columns, named so nothing addresses them by a bare integer.
ROW_COLUMNS = ("include", "row", "title", "year", "word_count", "sections", "status")
COL = {name: i for i, name in enumerate(ROW_COLUMNS)}
TREE_COL = {name: f"#{i + 1}" for i, name in enumerate(ROW_COLUMNS)}   # Tk's 1-based ids

logger = logging.getLogger("gui_app")


def _log_font():
    """A monospaced face for the log, whichever the platform actually has."""
    try:
        from tkinter import font as tkfont

        available = set(tkfont.families())
        for candidate in ("SF Mono", "Menlo", "Monaco", "Courier New"):
            if candidate in available:
                return (candidate, 10)
    except Exception:  # noqa: BLE001 - falls back to the default face
        pass
    return None


class SettingsTab(ttk.Frame):
    """Scrollable — the form is taller than the window, and a Save button that's
    scrolled out of view is a Save button that never gets clicked."""

    def __init__(self, parent, on_models_changed=None):
        super().__init__(parent)
        self.config = config_manager.load_config()
        self.on_models_changed = on_models_changed
        self.vars = {}

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.inner = ttk.Frame(canvas, padding=16)
        inner_window = canvas.create_window((0, 0), window=self.inner, anchor="nw")

        def _on_inner_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(inner_window, width=event.width)

        self.inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # macOS sends small deltas (±1-10); Windows sends multiples of 120. Dividing by
        # 120 unconditionally truncates every macOS scroll to 0 — i.e. no scrolling at all.
        def _on_mousewheel(event):
            delta = event.delta
            if not delta:
                return
            if abs(delta) >= 120:          # Windows: multiples of 120
                steps = int(-delta / 120)
            else:                          # macOS: small, sometimes fractional deltas
                steps = int(-delta)
                if steps == 0:             # sub-unit trackpad scroll — still move one line
                    steps = -1 if delta > 0 else 1
            canvas.yview_scroll(steps, "units")

        self.canvas = canvas
        self.scroll_wheel = _on_mousewheel  # App binds this globally; see App.__init__
        canvas.bind("<MouseWheel>", _on_mousewheel)
        # Linux/X11 button-4/5 scroll events
        canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))

        self._build()

    def _section(self, parent, r, title):
        ttk.Label(parent, text=title, font=("", 12, "bold")).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(14, 6)
        )
        return r + 1

    def _row(self, parent, r, label, key, default, width=28, secret=False):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=str(default))
        entry = ttk.Entry(parent, textvariable=var, width=width, show="*" if secret else "")
        entry.grid(row=r, column=1, sticky="w", pady=3, padx=(8, 0))
        self.vars[key] = var
        return r + 1

    def _dropdown(self, parent, r, label, key, default, options, width=26):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=str(default))
        ttk.Combobox(parent, textvariable=var, values=options, width=width, state="readonly").grid(
            row=r, column=1, sticky="w", pady=3, padx=(8, 0)
        )
        self.vars[key] = var
        return r + 1

    def _build(self):
        """Two columns so the whole form fits a standard window — scrolling stays as a
        fallback for small screens rather than being required to reach Save."""
        c = self.config

        columns = ttk.Frame(self.inner)
        columns.grid(row=0, column=0, columnspan=2, sticky="nw")
        left = ttk.Frame(columns)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 40))
        right = ttk.Frame(columns)
        right.grid(row=0, column=1, sticky="nw")

        # ---------- left column ----------
        r = self._section(left, 0, "AI Provider")
        r = self._row(left, r, "API Key", "api_key", config_manager.load_api_key(), secret=True)
        r = self._row(left, r, "Base URL", "base_url", c["ai_provider"]["base_url"])

        ttk.Label(left, text="Model").grid(row=r, column=0, sticky="w", pady=3)
        model_var = tk.StringVar(value=c["ai_provider"]["model"])
        self.model_combo = ttk.Combobox(
            left, textvariable=model_var, width=28,
            values=c.get("free_models", [c["ai_provider"]["model"]]),
        )
        self.model_combo.grid(row=r, column=1, sticky="w", pady=3, padx=(8, 0))
        self.vars["model"] = model_var
        r += 1
        ttk.Button(left, text="Fetch free models", command=self._fetch_models).grid(
            row=r, column=1, sticky="w", padx=(8, 0), pady=(0, 2)
        )
        r += 1
        self.model_status = ttk.Label(left, text="", foreground="gray", wraplength=240)
        self.model_status.grid(row=r, column=1, sticky="w", padx=(8, 0))
        r += 1

        r = self._row(left, r, "Temperature", "temperature", c["ai_provider"]["temperature"])
        r = self._row(left, r, "Max Retries", "max_retries", c["ai_provider"]["max_retries"])

        r = self._section(left, r, "Writing Style")
        style_labels = [f"{key} — {val['label']}" for key, val in STYLES.items()]
        current_style = c.get("writing_style", "ieee_paper")
        current_label = next((s for s in style_labels if s.startswith(current_style)), style_labels[0])
        r = self._dropdown(left, r, "Style & Tone", "writing_style", current_label, style_labels)

        # ---------- right column ----------
        r = self._section(right, 0, "Word Count Validation")
        r = self._row(right, r, "Tolerance %", "tolerance_percent", c["validation"]["word_count_tolerance_percent"], width=12)
        r = self._row(right, r, "Max Correction Passes", "max_passes", c["validation"]["max_correction_passes"], width=12)
        r = self._row(right, r, "Abstract Min Words", "abstract_min", c["abstract"]["min_words"], width=12)
        r = self._row(right, r, "Abstract Max Words", "abstract_max", c["abstract"]["max_words"], width=12)

        r = self._section(right, r, "PDF Output")
        r = self._dropdown(right, r, "Font", "pdf_font", c["pdf"]["font"], PDF_FONTS, width=16)
        r = self._dropdown(right, r, "Page Size", "pdf_page_size", c["pdf"]["page_size"], PDF_PAGE_SIZES, width=16)

        r = self._section(right, r, "Defaults & Performance")
        r = self._row(right, r, "Default Word Count", "default_word_count", c.get("defaults", {}).get("word_count", 2000), width=12)
        r = self._row(right, r, "Default Sections", "default_sections", c.get("defaults", {}).get("sections", 5), width=12)
        r = self._row(right, r, "Excel Save Every N Rows", "save_every", c.get("excel_save_every_n_rows", 1), width=12)

        r = self._section(right, r, "Parallel Runs")
        r = self._row(right, r, "Concurrent AI requests (all jobs)", "global_concurrency",
                      c["ai_provider"].get("global_max_concurrent_requests", 6), width=12)
        r = self._row(right, r, "Sections in parallel (per article)", "article_concurrency",
                      c["ai_provider"].get("max_concurrent_requests", 2), width=12)
        r = self._row(right, r, "Halt after N failures in a row", "max_consecutive",
                      c.get("max_consecutive_failures", 3), width=12)
        r = self._row(right, r, "Article timeout (seconds)", "article_timeout",
                      c["ai_provider"].get("article_timeout_seconds", 300), width=12)
        ttk.Label(
            right,
            text=f"Up to {MAX_PARALLEL_JOBS} batches run at once. Launch them from a sheet's\n"
                 f"own tab; use '+ Sheet tab' for a different Excel file.",
            foreground="gray",
        ).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0))
        r += 1

        # ---------- save bar ----------
        save_frame = ttk.Frame(self.inner)
        save_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=(24, 20))
        ttk.Button(save_frame, text="Save Settings", command=self._save).pack(side="left")
        self.status_label = ttk.Label(save_frame, text="", foreground="green")
        self.status_label.pack(side="left", padx=(12, 0))

    def _fetch_models(self):
        """Pulls the live free-model list from the provider using the key in the form."""
        api_key = self.vars["api_key"].get().strip()
        base_url = self.vars["base_url"].get().strip()
        if not api_key:
            messagebox.showwarning("No API key", "Enter your API key first, then fetch models.")
            return

        self.model_status.config(text="Fetching...")
        self.update_idletasks()
        try:
            models = config_manager.fetch_free_models(api_key, base_url)
        except Exception as exc:
            self.model_status.config(text=f"Fetch failed: {exc}", foreground="red")
            return

        if not models:
            self.model_status.config(text="No :free models returned by provider", foreground="red")
            return

        # Persist immediately rather than waiting for Save: every sheet tab reads its
        # model list from config["free_models"], so a fetch that only updated this combo
        # left the sheet tabs offering the stale handful they were shipped with.
        self.config["free_models"] = models
        config_manager.save_config(self.config)

        self.model_combo.config(values=models)
        self.model_status.config(text=f"{len(models)} free models loaded — available in every sheet tab",
                                 foreground="green")
        if self.on_models_changed is not None:
            self.on_models_changed()

    def _save(self):
        try:
            v = self.vars
            c = self.config
            c["ai_provider"]["base_url"] = v["base_url"].get().strip()
            c["ai_provider"]["model"] = v["model"].get().strip()
            c["ai_provider"]["temperature"] = float(v["temperature"].get())
            c["ai_provider"]["max_retries"] = int(v["max_retries"].get())
            c["validation"]["word_count_tolerance_percent"] = int(v["tolerance_percent"].get())
            c["validation"]["max_correction_passes"] = int(v["max_passes"].get())
            c["abstract"]["min_words"] = int(v["abstract_min"].get())
            c["abstract"]["max_words"] = int(v["abstract_max"].get())
            c["pdf"]["font"] = v["pdf_font"].get()
            c["pdf"]["page_size"] = v["pdf_page_size"].get()
            c.setdefault("defaults", {})["word_count"] = int(v["default_word_count"].get())
            c["defaults"]["sections"] = int(v["default_sections"].get())
            c["excel_save_every_n_rows"] = max(1, int(v["save_every"].get()))
            c["writing_style"] = v["writing_style"].get().split(" — ")[0].strip()
            c["ai_provider"]["global_max_concurrent_requests"] = max(1, int(v["global_concurrency"].get()))
            c["ai_provider"]["max_concurrent_requests"] = max(1, int(v["article_concurrency"].get()))
            c["ai_provider"]["article_timeout_seconds"] = max(30, int(v["article_timeout"].get()))
            c["max_consecutive_failures"] = max(1, int(v["max_consecutive"].get()))

            config_manager.save_config(c)
            config_manager.save_api_key(v["api_key"].get().strip())
            self.status_label.config(text="Saved.")
        except ValueError as exc:
            messagebox.showerror("Invalid value", f"Check your numeric fields: {exc}")


class BatchTab(ttk.Frame):
    """One Excel sheet: pick a batch and run it, or launch several of this sheet's
    batches at once. Open a second tab (App's '+ Sheet tab') for a different sheet."""

    def __init__(self, parent, job_manager=None, on_title_change=None, job_owner=None):
        super().__init__(parent, padding=16)
        self.excel_path: Path | None = None
        self.output_dir: Path | None = None
        self.all_rows = []       # every valid row in the sheet, in sheet order
        self.batches = []        # fixed partition of all_rows, decided once at load time
        self.current_batch_index = 0
        self.pending_rows = []   # rows of the currently displayed batch
        self.row_overrides = {}  # row_number -> (word_count, sections)
        self.included = {}       # row_number -> bool (Run? checkbox)
        self.batch_size_var = tk.StringVar(value="25")
        self.batch_choice_var = tk.StringVar()
        self.regen_queue = queue.Queue()
        self.regen_worker: threading.Thread | None = None
        self.stop_event: threading.Event | None = None

        # Shared across every sheet tab so the parallel-job ceiling is app-wide, and so
        # two tabs opened on the same file still share one workbook handle.
        self.job_manager = job_manager
        self.on_title_change = on_title_change
        # job_id -> event queue, so the shared manager can route each job's events back
        # to the sheet tab that launched it.
        self.job_owner = {} if job_owner is None else job_owner
        self.job_events = queue.Queue()
        self._job_polling = False
        self.parallel_spec_var = tk.StringVar(value="1")
        self.parallel_model_var = tk.StringVar()
        self.runner_rows: dict = {}   # batch number -> its Run/Stop/progress widgets
        self.job_batch: dict = {}     # job_id -> batch number
        self._awaiting_final: set = set()  # jobs whose job_done event is still pending
        self._dirty_counts = False         # a row landed; progress figures need refreshing
        self._build()

    def _build(self):
        # Two columns: controls on the left, the combined log given real height on the
        # right. Stacked vertically the log was squeezed to two visible lines.
        # A draggable split, not fixed widths: packing gave the controls priority and
        # squeezed the log to an unreadable ribbon. The sash starts wide enough to read
        # full log lines and the user can rebalance it.
        split = ttk.PanedWindow(self, orient="horizontal")
        split.pack(fill="both", expand=True)
        left_outer = ttk.Frame(split)
        left = ttk.Frame(left_outer, padding=(0, 0, 16, 0))   # breathing room before the sash
        left.pack(fill="both", expand=True)
        right = ttk.Frame(split)
        split.add(left_outer, weight=3)
        split.add(right, weight=2)
        self._split = split
        self.after(120, lambda: self._place_sash(0.60))
        # Reflow the runner grid whenever this pane's width changes — dragging the
        # sash to widen the log used to leave runners clipped instead of wrapping.
        self._relayout_pending = False
        left.bind("<Configure>", self._on_left_resize)

        log_frame = ttk.LabelFrame(right, text="Combined log (every batch)", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, state="disabled", wrap="word", width=54,
                           font=_log_font())
        self.log.pack(fill="both", expand=True, side="left")
        log_bar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_bar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_bar.set)
        self.log.tag_configure("success", foreground="#1a7f37")
        self.log.tag_configure("failure", foreground="#b3261e")
        ttk.Button(right, text="Clear log", command=self._clear_log).pack(anchor="e", pady=(8, 0))

        # A single label-column grid for Source/Batching/Run — labels share one width so
        # the fields that follow line up in one visual column instead of drifting per row.
        LABEL_WIDTH = 13
        form = ttk.Frame(left)
        form.pack(fill="x")

        def field_label(parent, text):
            ttk.Label(parent, text=text, width=LABEL_WIDTH, anchor="w").pack(side="left")

        source_row = ttk.Frame(form)
        source_row.pack(fill="x")
        field_label(source_row, "Source")
        self.excel_button = ttk.Button(source_row, text="Choose Excel File…", command=self._choose_excel)
        self.excel_button.pack(side="left")
        self.excel_label = ttk.Label(source_row, text="No file selected", foreground="gray")
        self.excel_label.pack(side="left", padx=(8, 0))

        folder_row = ttk.Frame(form)
        folder_row.pack(fill="x", pady=(6, 0))
        field_label(folder_row, "Save to")
        self.folder_button = ttk.Button(folder_row, text="Choose Output Folder…", command=self._choose_folder)
        self.folder_button.pack(side="left")
        self.folder_label = ttk.Label(folder_row, text="No folder selected", foreground="gray")
        self.folder_label.pack(side="left", padx=(8, 0))

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=12)

        size_row = ttk.Frame(form)
        size_row.pack(fill="x")
        field_label(size_row, "Batch size")
        ttk.Entry(size_row, textvariable=self.batch_size_var, width=6).pack(side="left")
        ttk.Button(size_row, text="Rebuild batches", command=self._rebuild_batches).pack(side="left", padx=(10, 0))

        model_row = ttk.Frame(form)
        model_row.pack(fill="x", pady=(6, 0))
        field_label(model_row, "Model")
        self.parallel_model_combo = ttk.Combobox(model_row, textvariable=self.parallel_model_var, width=36)
        self.parallel_model_combo.pack(side="left")

        run_row = ttk.Frame(form)
        run_row.pack(fill="x", pady=(6, 0))
        field_label(run_row, "Batches to run")
        ttk.Entry(run_row, textvariable=self.parallel_spec_var, width=12).pack(side="left")
        ttk.Label(run_row, text="e.g. 3 · 1-3 · 2,4,5", foreground="gray").pack(side="left", padx=(8, 0))

        action_row = ttk.Frame(form)
        action_row.pack(fill="x", pady=(10, 0))
        field_label(action_row, "")
        self.parallel_run_button = ttk.Button(action_row, text="Start These Batches",
                                              command=self._start_selected_batches)
        self.parallel_run_button.pack(side="left")
        ttk.Button(action_row, text="Stop All", command=self._stop_parallel).pack(side="left", padx=(8, 0))
        self.parallel_status = ttk.Label(action_row, text="Idle", foreground="gray")
        self.parallel_status.pack(side="left", padx=(14, 0))

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=12)

        # ---------------- per-batch runners: own play/stop toggle, progress ----------------
        runners_header = ttk.Frame(left)
        runners_header.pack(fill="x")
        ttk.Label(runners_header, text="Batch Runners", font=("", 12, "bold")).pack(side="left")
        ttk.Label(runners_header, text=f"up to {MAX_PARALLEL_JOBS} at once",
                 foreground="gray").pack(side="left", padx=(8, 0))
        self.runners_frame = ttk.Frame(left, padding=(0, 6, 0, 0))
        self.runners_frame.pack(fill="x")
        self.runners_empty = ttk.Label(
            self.runners_frame,
            text="No batches started — enter batch numbers above and press "
                 "\"Start These Batches\", or ▶ a single batch below.",
            foreground="gray")
        self.runners_empty.pack(anchor="w")
        # Runners are laid out in a grid so several fit side by side instead of one
        # tall column pushing everything else off-screen.
        self.runners_grid = ttk.Frame(self.runners_frame)
        self.runners_grid.pack(fill="x")

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=12)

        # ---------------- inspect / edit a single batch's rows ----------------
        inspect = ttk.Frame(left)
        inspect.pack(fill="x")
        field_label(inspect, "Inspect batch")
        self.batch_selector = ttk.Combobox(inspect, textvariable=self.batch_choice_var,
                                            width=40, state="readonly", values=[])
        self.batch_selector.pack(side="left")
        self.batch_selector.bind("<<ComboboxSelected>>", self._on_batch_selected)
        self.remaining_label = ttk.Label(inspect, text="", foreground="gray")
        self.remaining_label.pack(side="left", padx=(12, 0))

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.tree = ttk.Treeview(tree_frame, columns=ROW_COLUMNS, show="headings", height=10)
        # Everything centred, including Title, so the columns read as one aligned block.
        for col, label, width in [
            ("include", "Run?", 46), ("row", "Row", 48), ("title", "Title", 240),
            ("year", "Year", 54),
            ("word_count", "Word Count", 84), ("sections", "Sections", 68), ("status", "Status", 110),
        ]:
            self.tree.heading(col, text=label, anchor="center")
            self.tree.column(col, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", self._edit_cell)
        self.tree.bind("<Button-1>", self._on_single_click)
        self.tree.bind("<Button-2>", self._show_context_menu)
        self.tree.bind("<Control-Button-1>", self._show_context_menu)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Regenerate this article", command=self._regenerate_selected)

        hint_row = ttk.Frame(left)
        hint_row.pack(fill="x", pady=(4, 0))
        ttk.Label(
            hint_row,
            text="Click Run? to include/exclude · double-click Word Count/Sections to edit · right-click a row to regenerate",
        ).pack(side="left")
        ttk.Button(hint_row, text="Select all", command=lambda: self._set_all_included(True)).pack(side="right", padx=(6, 0))
        ttk.Button(hint_row, text="Deselect all", command=lambda: self._set_all_included(False)).pack(side="right")
        ttk.Button(hint_row, text="Refresh Defaults", command=self._refresh_defaults).pack(side="right", padx=(0, 6))

        self._refresh_parallel_models()

    # ------------------------------------------------- parallel batches (this sheet)

    def _refresh_parallel_models(self):
        cfg = config_manager.load_config()
        models = cfg.get("free_models") or [cfg["ai_provider"]["model"]]
        self.parallel_model_combo.config(values=[AUTO_MODEL] + list(models))
        if not self.parallel_model_var.get():
            # Default to spreading load: parallel batches on one model all die together
            # the moment that model's quota or concurrency cap is reached.
            self.parallel_model_var.set(AUTO_MODEL)

    def _models_for(self, batch_numbers: list[int], pool: list | None = None) -> dict:
        """Assigns a model to each batch, round-robin when set to Auto."""
        cfg = config_manager.load_config()
        chosen = self.parallel_model_var.get().strip()
        if chosen and chosen != AUTO_MODEL:
            return {n: chosen for n in batch_numbers}

        pool = list(pool or cfg.get("free_models") or [cfg["ai_provider"]["model"]])
        return {n: pool[i % len(pool)] for i, n in enumerate(batch_numbers)}

    def _live_model_pool(self) -> list:
        """Models that answer right now, in configured preference order."""
        cfg = config_manager.load_config()
        candidates = list(cfg.get("free_models") or [cfg["ai_provider"]["model"]])
        self.parallel_status.config(text="checking models…", foreground="gray")
        self.update_idletasks()
        results = probe_models(candidates, cfg)
        alive = [m for m in candidates if results.get(m, (True, ""))[0]]

        # "Skipping unavailable models" told the user nothing they could act on. Grouping
        # by cause does: a daily quota needs credits or a wait, a down provider needs
        # neither — it will come back on its own.
        by_reason: dict[str, list[str]] = {}
        for model in candidates:
            ok, reason = results.get(model, (True, ""))
            if not ok:
                by_reason.setdefault(reason, []).append(model.split("/")[-1])
        for reason, models in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            self._log(f"{len(models)} model(s) — {reason}: {', '.join(models)}")
        if "daily free quota used up" in by_reason:
            self._log("Daily free quota is an account limit, not a setting — it resets "
                      "at 00:00 UTC, or add credits at openrouter.ai to lift it.")
        if not alive:
            self._log("No configured model answered — using the Settings model anyway.")
            return [cfg["ai_provider"]["model"]]
        self._log("Live models: " + ", ".join(m.split("/")[-1] for m in alive))
        return alive

    def _tab_busy(self) -> bool:
        """True while this tab has a batch job or a regeneration in flight."""
        if self.regen_worker is not None and self.regen_worker.is_alive():
            return True
        if self.job_manager is None:
            return False
        return any(self.job_manager.is_running(jid) for jid in self.job_batch)

    # One toggle button covers the whole state space (idle/running) — showing a second,
    # permanently-disabled button for the action that can't currently be taken added
    # visual noise without adding a capability (HIG: simplicity, one control per action).
    _TOGGLE_PLAY = {"text": "▶", "fg": "#1a7f37", "font": ("", 15)}
    _TOGGLE_STOP = {"text": "■", "fg": "#b3261e", "font": ("", 13)}

    def _ensure_runner_row(self, batch_number: int):
        """One control strip per batch: a play/stop toggle and a progress bar."""
        if batch_number in self.runner_rows:
            return self.runner_rows[batch_number]

        self.runners_empty.pack_forget()

        frame = ttk.Frame(self.runners_grid, padding=(0, 3))

        ttk.Label(frame, text=f"Batch {batch_number}", width=9, anchor="w",
                  font=("", 11, "bold")).pack(side="left")
        # tk.Button, not ttk, because only the classic widget honours foreground
        # colour on aqua — needed for the green/red state colours.
        toggle = tk.Button(frame, width=3, highlightthickness=0, bd=0, relief="flat",
                           command=lambda n=batch_number: self._toggle_batch(n))
        toggle.pack(side="left", padx=(2, 8))
        toggle.bind("<Enter>", lambda _e: toggle.config(relief="raised"))
        toggle.bind("<Leave>", lambda _e: toggle.config(relief="flat"))
        toggle.configure(**self._TOGGLE_PLAY, activeforeground=self._TOGGLE_PLAY["fg"])

        bar = ttk.Progressbar(frame, mode="determinate", length=110)
        bar.pack(side="left", padx=(0, 8))
        status = ttk.Label(frame, text="idle", width=22, anchor="w", foreground="gray")
        status.pack(side="left")

        row = {"frame": frame, "toggle": toggle, "bar": bar,
               "status": status, "job_id": None, "running": False}
        self.runner_rows[batch_number] = row
        self._relayout_runners()
        return row

    def _set_toggle_running(self, row: dict, running: bool):
        row["running"] = running
        row["toggle"].configure(**(self._TOGGLE_STOP if running else self._TOGGLE_PLAY))
        row["toggle"].configure(activeforeground=(self._TOGGLE_STOP if running else self._TOGGLE_PLAY)["fg"])

    def _toggle_batch(self, batch_number: int):
        row = self.runner_rows.get(batch_number)
        if row and row["running"]:
            self._stop_one_batch(batch_number)
        else:
            self._start_one_batch(batch_number)

    def _on_left_resize(self, _event=None):
        if self._relayout_pending:
            return
        self._relayout_pending = True

        def go():
            self._relayout_pending = False
            self._relayout_runners()

        self.after(80, go)

    def _relayout_runners(self):
        """Column count follows the pane's actual width, so widening the log (which
        narrows this side) reflows runners to fewer columns instead of clipping them."""
        width = self.runners_grid.winfo_width()
        if width < 50:  # not laid out yet
            self.after(100, self._relayout_runners)
            return
        per_item = 260
        columns = max(1, width // per_item)
        for slot, (_, row) in enumerate(self.runner_rows.items()):
            row["frame"].grid_forget()
            row["frame"].grid(row=slot // columns, column=slot % columns,
                              sticky="w", padx=(0, 20))

    def _start_one_batch(self, batch_number: int, model: str | None = None):
        if self.job_manager is None:
            return
        if self.excel_path is None or self.output_dir is None:
            messagebox.showwarning("Missing selection",
                                   "Choose an Excel file and an output folder first.")
            return

        row = self._ensure_runner_row(batch_number)
        if row["job_id"] is not None and self.job_manager.is_running(row["job_id"]):
            self._log(f"[batch {batch_number}] already running")
            return

        if len(self.job_manager.jobs) >= MAX_PARALLEL_JOBS:
            # Reclaim slots held by jobs that already finished.
            for jid, job in list(self.job_manager.jobs.items()):
                if not self.job_manager.is_running(jid) and job.status in ("Done", "Stopped", "Failed"):
                    try:
                        self.job_manager.remove_job(jid)
                    except ValueError:
                        pass
        if len(self.job_manager.jobs) >= MAX_PARALLEL_JOBS:
            messagebox.showwarning(
                "Limit reached",
                f"{MAX_PARALLEL_JOBS} batches are already running (across all sheet tabs). "
                "Stop one before starting another.",
            )
            return

        cfg = config_manager.load_config()
        model = model or self._models_for([batch_number])[batch_number]
        defaults = cfg.get("defaults", {"word_count": 2500, "sections": 5})

        # If this batch is the one loaded in the table, honour its Run? ticks and edits.
        overrides, row_filter = {}, None
        if self.batches and 0 <= self.current_batch_index < len(self.batches):
            if self.current_batch_index + 1 == batch_number:
                overrides = dict(self.row_overrides)
                row_filter = {r for r, keep in self.included.items() if keep}

        job = self.job_manager.add_job(
            self.excel_path, self.output_dir, model, [batch_number],
            self._current_batch_size(), defaults["word_count"], defaults["sections"],
            row_overrides=overrides, row_filter=row_filter,
        )
        self.job_owner[job.job_id] = self.job_events
        self.job_batch[job.job_id] = batch_number
        row["job_id"] = job.job_id
        row["bar"].config(value=0, maximum=100)
        row["status"].config(text=f"starting on {model.split('/')[-1]}", foreground="black")
        self._set_toggle_running(row, True)

        self._awaiting_final.add(job.job_id)
        self.job_manager.start_job(job.job_id, cfg)
        self._log(f"[batch {batch_number}] started on {model}")
        self._ensure_job_polling()

    def _start_selected_batches(self):
        if self.job_manager is None:
            return
        if self.excel_path is None or self.output_dir is None:
            messagebox.showwarning("Missing selection",
                                   "Choose an Excel file and an output folder first.")
            return

        wanted = parse_batch_spec(self.parallel_spec_var.get(), len(self.batches))
        if not wanted:
            messagebox.showwarning(
                "No batches",
                f"'{self.parallel_spec_var.get()}' matched nothing. This sheet has "
                f"{len(self.batches)} batches at the current batch size.",
            )
            return

        auto = self.parallel_model_var.get().strip() == AUTO_MODEL
        pool = None
        if auto:
            pool = self._live_model_pool()
            # Running more batches than there are live models just makes them fight over
            # one endpoint — which is exactly how every batch ended up Failed before.
            if len(wanted) > len(pool):
                self._log(f"{len(pool)} model(s) answering, so starting {len(pool)} "
                          f"batch(es) now: {wanted[:len(pool)]}. Press ▶ on the rest "
                          f"as these finish.")
                wanted_now, wanted_later = wanted[:len(pool)], wanted[len(pool):]
            else:
                wanted_now, wanted_later = wanted, []
        else:
            wanted_now, wanted_later = wanted, []

        assigned = self._models_for(wanted_now, pool=pool)
        if auto:
            spread = ", ".join(f"b{n}→{assigned[n].split('/')[-1]}" for n in wanted_now)
            self._log(f"Auto model spread: {spread}")

        for batch_number in wanted_later:
            self._ensure_runner_row(batch_number)
            self.runner_rows[batch_number]["status"].config(text="queued — press ▶")

        for batch_number in wanted_now:
            self._ensure_runner_row(batch_number)
            running = sum(1 for jid in self.job_manager.jobs if self.job_manager.is_running(jid))
            if running >= MAX_PARALLEL_JOBS:
                self._log(f"[batch {batch_number}] queued — {MAX_PARALLEL_JOBS} already running; "
                          f"press ▶ when a slot frees up")
                continue
            self._start_one_batch(batch_number, model=assigned[batch_number])

    def _stop_one_batch(self, batch_number: int):
        row = self.runner_rows.get(batch_number)
        if not row or row["job_id"] is None or self.job_manager is None:
            return
        self.job_manager.stop_job(row["job_id"])
        row["status"].config(text="stopping...")
        row["toggle"].config(state="disabled")  # re-enabled by job_done, avoids a double-stop
        self._log(f"[batch {batch_number}] stop requested")

    def _stop_parallel(self):
        if self.job_manager is None:
            return
        for batch_number in list(self.runner_rows):
            self._stop_one_batch(batch_number)

    def _ensure_job_polling(self):
        if not self._job_polling:
            self._job_polling = True
            self.after(200, self._poll_job_events)

    def _runner_for_job(self, job_id):
        batch_number = self.job_batch.get(job_id)
        if batch_number is None:
            return None, None
        return batch_number, self.runner_rows.get(batch_number)

    def _apply_row_result(self, row_number: int, status: str):
        """Live-update one row in the table, whichever batch produced it."""
        iid = str(row_number)
        if not self.tree.exists(iid):
            return   # that row belongs to a batch that isn't on screen
        done = str(status).startswith("Success")
        values = list(self.tree.item(iid, "values"))
        values[COL["include"]] = UNCHECKED if done else CHECKED
        values[COL["status"]] = "Done" if done else status
        self.tree.item(iid, values=values)
        self.included[row_number] = not done

    def _refresh_progress_counts(self):
        """Recomputes the 'N done' figures shown in the dropdown and the inspect line."""
        if not self.batches:
            return
        self._refresh_batch_labels()
        statuses = self._fresh_statuses()
        done = sum(1 for r, *_ in self.pending_rows
                   if str(statuses.get(r, "")).startswith("Success"))
        to_run = len(self.pending_rows) - done
        self.remaining_label.config(
            text=f"Batch {self.current_batch_index + 1} of {len(self.batches)} · "
                 f"{done} done · {to_run} to run"
        )

    def _poll_job_events(self):
        try:
            while True:
                event = self.job_events.get_nowait()
                kind, job_id = event[0], event[1]
                batch_number, row = self._runner_for_job(job_id)
                job = self.job_manager.jobs.get(job_id) if self.job_manager else None

                if kind == "log":
                    self._log(f"[batch {batch_number}] {event[2]}")
                elif kind == "job_status" and row:
                    status, note = event[2], event[3]
                    row["status"].config(text=f"{status}{' — ' + note if note else ''}")
                elif kind == "row_done":
                    if row and job:
                        row["bar"].config(maximum=max(job.total_rows, 1),
                                          value=job.done_rows + job.failed_rows)
                        row["status"].config(
                            text=f"{job.done_rows}/{job.total_rows} done"
                                 f"{f', {job.failed_rows} failed' if job.failed_rows else ''}")
                    # Reflect the finished row in the table straight away. Waiting until
                    # every batch stopped left rows reading "Pending" long after the log
                    # had already reported them as Success.
                    self._apply_row_result(event[2], event[3])
                    self._dirty_counts = True
                elif kind == "job_done":
                    self._awaiting_final.discard(job_id)
                    if row:
                        self._set_toggle_running(row, False)
                        row["toggle"].config(state="normal")
                        if job:
                            row["status"].config(
                                text=f"{job.status} — {job.done_rows} done, {job.failed_rows} failed")
                            self._log(f"[batch {batch_number}] finished: {job.status} — "
                                      f"{job.done_rows} done, {job.failed_rows} failed")
        except queue.Empty:
            pass

        running = 0
        if self.job_manager is not None:
            running = sum(1 for jid in self.job_manager.jobs if self.job_manager.is_running(jid))
        self.parallel_status.config(
            text=f"{running} batch(es) running" if running else "idle",
            foreground="green" if running else "gray")

        # Counts come straight from the workbook, so recompute them whenever a row
        # landed rather than once at the very end.
        if self._dirty_counts:
            self._dirty_counts = False
            self._refresh_progress_counts()

        # Keep polling until every started job has delivered its job_done event, not
        # merely until the threads stop: the final events land after is_alive() flips
        # false, and dropping them left Run buttons stuck disabled.
        if running or self._awaiting_final or not self.job_events.empty():
            self.after(200, self._poll_job_events)
        else:
            self._job_polling = False
            if self.batches:
                self._refresh_batch_labels()
                self._load_batch(self.current_batch_index)

    def _place_sash(self, fraction: float):
        """Puts the divider at `fraction` of the current width, once geometry is known."""
        try:
            width = self._split.winfo_width()
            if width < 200:            # not laid out yet — try again shortly
                self.after(120, lambda: self._place_sash(fraction))
                return
            self._split.sashpos(0, int(width * fraction))
        except (tk.TclError, AttributeError):
            pass

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # Outcome markers, so a run's result is readable at a glance instead of parsed
    # word by word out of a wall of prose.
    _LOG_SUCCESS = ("✓", "success")
    _LOG_FAILURE = ("✗", "failure")

    def _classify(self, message: str):
        """(glyph, tag) for a log line, or (None, None) for neutral chatter."""
        lowered = message.lower()
        if "success" in lowered or "finished: done" in lowered:
            return self._LOG_SUCCESS
        if any(word in lowered for word in
               ("failed", "error", "stopped", "not responding", "exceeded", "unavailable")):
            return self._LOG_FAILURE
        return (None, None)

    def _log(self, message: str):
        glyph, tag = self._classify(message)
        self.log.config(state="normal")
        if glyph:
            self.log.insert("end", f"{glyph} ", tag)
            self.log.insert("end", message + "\n", tag)
        else:
            self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _choose_excel(self):
        selected = filedialog.askopenfilename(title="Select assignments Excel file", filetypes=[("Excel files", "*.xlsx")])
        if not selected:
            return
        self.excel_path = Path(selected)
        self.excel_label.config(text=self.excel_path.name)
        if self.on_title_change:
            self.on_title_change(self, self.excel_path.stem[:22])
        self._load_rows()

    def _choose_folder(self):
        selected = filedialog.askdirectory(title="Choose output folder")
        if not selected:
            return
        self.output_dir = Path(selected)
        self.folder_label.config(text=str(self.output_dir))

    def _load_rows(self):
        # Read through the shared registry, never a private handle: the batch jobs write
        # status through the registry's workbook, and a second handle on the same file
        # never sees those writes (that is why the inspect line read "0 done" while the
        # log was already reporting rows as Success).
        try:
            self.all_rows = self._read_rows_shared()
        except MissingColumnsError as exc:
            # A silent "Loaded 0 rows" hid exactly this: the sheet was fine, the
            # column names just weren't the ones being looked for.
            self.all_rows = []
            self._log(f"Could not read this sheet — {exc}")
            messagebox.showerror("Cannot read this sheet", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a broken file must not kill the tab
            self.all_rows = []
            logger.exception("Failed to read workbook")
            self._log(f"Could not open {self.excel_path.name}: {exc}")
            messagebox.showerror("Cannot open file", f"{self.excel_path.name}\n\n{exc}")
            return

        done = sum(1 for r in self.all_rows if str(r[4]).startswith("Success"))
        if not self.all_rows:
            self._log(f"{self.excel_path.name}: no article rows found "
                      f"(every row is missing a Title or a Scope).")
        else:
            self._log(f"Loaded {len(self.all_rows)} rows ({done} already done) "
                      f"from {self.excel_path.name}.")
        self._rebuild_batches()

    def _current_batch_size(self) -> int:
        try:
            return max(1, int(self.batch_size_var.get()))
        except ValueError:
            return 25

    def _read_rows_shared(self) -> list:
        """Rows via the shared workbook registry when available, else a direct read."""
        if self.excel_path is None:
            return []
        if self.job_manager is not None:
            return self.job_manager.registry.read_rows(self.excel_path)
        return list(ExcelBatch(self.excel_path).read_rows())

    def _fresh_statuses(self) -> dict:
        """Row statuses straight from the live workbook.

        The in-memory row snapshot is taken once when the file is chosen; rendering
        from it showed rows as 'Failed' even after they had completed and their PDFs
        were on disk. Always read status through here instead.
        """
        if self.excel_path is None:
            return {}
        return {r[0]: (r[4] or "") for r in self._read_rows_shared()}

    def _rebuild_batches(self):
        """Partitions EVERY row into fixed, numbered batches. Batch membership is decided
        here and never shifts afterwards — completed rows keep their slot rather than
        being dropped, which is what made batch 1 appear to 'skip' row 2."""
        if not self.all_rows:
            return
        if self._tab_busy():
            messagebox.showwarning("Batch running", "Wait for the current batch to finish first.")
            return

        size = self._current_batch_size()
        self.batches = [self.all_rows[i:i + size] for i in range(0, len(self.all_rows), size)]

        self.current_batch_index = 0
        self._refresh_batch_labels()
        self._log(f"Built {len(self.batches)} fixed batches of up to {size} rows.")
        self._load_batch(0)

    def _refresh_batch_labels(self):
        """Updates the 'N done' counts in the dropdown without re-partitioning."""
        statuses = self._fresh_statuses()
        labels = []
        for i, rows in enumerate(self.batches, start=1):
            first_row, last_row = rows[0][0], rows[-1][0]
            done = sum(1 for r in rows if str(statuses.get(r[0], "")).startswith("Success"))
            labels.append(f"Batch {i} · rows {first_row}-{last_row} · {len(rows)} rows · {done} done")
        self.batch_selector.config(values=labels)
        self.batch_choice_var.set(labels[self.current_batch_index])

    def _on_batch_selected(self, _event=None):
        label = self.batch_choice_var.get()
        try:
            index = int(label.split()[1]) - 1
        except (IndexError, ValueError):
            return
        self._load_batch(index)

    def _load_batch(self, index: int):
        if not (0 <= index < len(self.batches)):
            return
        self.current_batch_index = index
        self.pending_rows = self.batches[index]

        config = config_manager.load_config()
        defaults = config.get("defaults", {"word_count": 2000, "sections": 5})
        statuses = self._fresh_statuses()   # live, not the load-time snapshot

        self.tree.delete(*self.tree.get_children())
        self.row_overrides = {}
        self.included = {}
        for row in self.pending_rows:
            row_number = row.row_number
            wc, sec = defaults["word_count"], defaults["sections"]
            self.row_overrides[row_number] = (wc, sec)
            status = statuses.get(row_number, "")
            already_done = str(status).startswith("Success")
            # Completed rows stay visible but unticked, so Run Batch resumes at the
            # first row that still needs work instead of redoing finished ones.
            self.included[row_number] = not already_done
            display_status = "Done" if already_done else (status or "Pending")
            self.tree.insert("", "end", iid=str(row_number),
                             values=(UNCHECKED if already_done else CHECKED,
                                     row_number, row.title, row.year or "—",
                                     wc, sec, display_status))

        to_run = sum(1 for v in self.included.values() if v)
        done = len(self.included) - to_run
        self.remaining_label.config(
            text=f"Batch {index + 1} of {len(self.batches)} · {done} done · {to_run} to run"
        )

    def _set_all_included(self, included: bool):
        for row_number in list(self.included):
            self.included[row_number] = included
            values = list(self.tree.item(str(row_number), "values"))
            values[COL["include"]] = CHECKED if included else UNCHECKED
            self.tree.item(str(row_number), values=values)

    def _on_single_click(self, event):
        """Toggle the Run? checkbox when its cell is clicked."""
        if self.tree.identify_column(event.x) != TREE_COL["include"]:
            return
        item = self.tree.identify_row(event.y)
        if not item or self._tab_busy():
            return
        row_number = int(item)
        self.included[row_number] = not self.included.get(row_number, True)
        values = list(self.tree.item(item, "values"))
        values[COL["include"]] = CHECKED if self.included[row_number] else UNCHECKED
        self.tree.item(item, values=values)

    def _refresh_defaults(self):
        """Re-reads config.yaml from disk and re-applies default word count/sections
        to every row currently displayed — for when Settings were saved after rows
        were already loaded into this chunk."""
        if self._tab_busy():
            messagebox.showwarning("Batch running", "Wait for the current batch to finish first.")
            return

        config = config_manager.load_config()
        defaults = config.get("defaults", {"word_count": 2000, "sections": 5})
        wc, sec = defaults["word_count"], defaults["sections"]

        for row_number, *_ in self.pending_rows:
            self.row_overrides[row_number] = (wc, sec)
            values = list(self.tree.item(str(row_number), "values"))
            values[COL["word_count"]] = wc
            values[COL["sections"]] = sec
            self.tree.item(str(row_number), values=values)

        self._log(f"Refreshed defaults from Settings: word count={wc}, sections={sec}.")

    def _edit_cell(self, event):
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        # Only Word Count and Sections are editable; Title/Year come from the sheet.
        if not item or column not in (TREE_COL["word_count"], TREE_COL["sections"]):
            return
        col_index = int(column[1:]) - 1
        x, y, w, h = self.tree.bbox(item, column)
        current = self.tree.item(item, "values")[col_index]

        edit_var = tk.StringVar(value=current)
        entry = ttk.Entry(self.tree, textvariable=edit_var)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus()

        def commit(_event=None):
            try:
                new_value = int(edit_var.get())
            except ValueError:
                entry.destroy()
                return
            values = list(self.tree.item(item, "values"))
            values[col_index] = new_value
            self.tree.item(item, values=values)
            row_number = int(item)
            wc, sec = self.row_overrides[row_number]
            if col_index == COL["word_count"]:
                wc = new_value
            else:               # sections column
                sec = max(3, new_value)
            self.row_overrides[row_number] = (wc, sec)
            entry.destroy()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)

    def _show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def _next_regen_label(self, row_number: int) -> str:
        """Finds the next free X.N suffix by looking at what's already in the output folder."""
        existing = list(self.output_dir.glob(f"{row_number}.*_*.pdf"))
        used = set()
        for path in existing:
            prefix = path.name.split("_", 1)[0]
            _, _, suffix = prefix.partition(".")
            if suffix.isdigit():
                used.add(int(suffix))
        n = 1
        while n in used:
            n += 1
        return f"{row_number}.{n}"

    def _regenerate_selected(self):
        if self._tab_busy():
            messagebox.showwarning("Batch running", "Wait for the current batch to finish first.")
            return
        if self.output_dir is None:
            messagebox.showwarning("No output folder", "Choose an output folder first.")
            return

        selection = self.tree.selection()
        if not selection:
            return
        row_number = int(selection[0])
        row = next((r for r in self.pending_rows if r[0] == row_number), None)
        if row is None:
            messagebox.showwarning("Row unavailable", "That row isn't part of the current batch.")
            return

        title, scope, author, year = row.title, row.scope, row.author, row.year
        word_count, sections = self.row_overrides[row_number]
        label = self._next_regen_label(row_number)

        try:
            config = config_manager.load_config()
            self.stop_event = threading.Event()
            client = AIClient(config, cancel_event=self.stop_event)
        except AIClientError as exc:
            messagebox.showerror("Startup error", str(exc))
            return

        self._log(f"Regenerating row {row_number} as {label}: '{title}'...")

        def _regen_worker():
            try:
                state = run_article(row_number, title, scope, author, word_count, sections,
                                    config, client, self.output_dir, file_label=label,
                                    year=year)
                self.regen_queue.put((label, state.status, state.notes))
            except Exception as exc:  # noqa: BLE001 - always report, never hang
                logger.exception("Regeneration crashed")
                self.regen_queue.put((label, "Failed", str(exc)))

        self.regen_worker = threading.Thread(target=_regen_worker, daemon=True)
        self.regen_worker.start()
        self.after(300, self._poll_regen)

    def _poll_regen(self):
        try:
            label, status, notes = self.regen_queue.get_nowait()
        except queue.Empty:
            if self.regen_worker is not None and self.regen_worker.is_alive():
                self.after(300, self._poll_regen)
            return

        self.regen_worker = None
        self.stop_event = None
        self._log(f"Regenerated → {label}: {status} — {notes}")
        messagebox.showinfo("Regenerated", f"Saved as {label}\n{status}\n{notes}")



def parse_batch_spec(spec: str, available: int) -> list[int]:
    """'1,3-5' -> [1, 3, 4, 5]. 'all' -> every batch. Out-of-range numbers are dropped."""
    spec = (spec or "").strip().lower()
    if not spec or spec == "all":
        return list(range(1, available + 1))

    wanted: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit():
                wanted.extend(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            wanted.append(int(part))

    seen, ordered = set(), []
    for n in wanted:
        if 1 <= n <= available and n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


class App(tk.Tk):
    """Settings plus one Batch tab per Excel sheet.

    Batches of the SAME sheet run in parallel from inside that sheet's tab; a DIFFERENT
    sheet gets its own tab via '+ Sheet tab'. All tabs share one JobManager, so the
    5-job ceiling is app-wide and two tabs on one file share a single workbook handle.
    """

    def __init__(self):
        super().__init__()
        self.title("AI Article Automation")
        # Freely resizable with a floor that keeps the batch table usable (HIG 2.1).
        self.minsize(880, 620)
        self.geometry(config_manager.load_config().get("window_geometry") or "1120x800")

        load_dotenv(Path(__file__).resolve().parent / ".env")

        self.job_owner: dict = {}   # job_id -> the launching tab's event queue

        def dispatch(event):
            target = self.job_owner.get(event[1])
            if target is not None:
                target.put(event)

        self.job_manager = JobManager(config_manager.load_config(), dispatch)
        self.sheet_tabs: list[BatchTab] = []

        toolbar = ttk.Frame(self, padding=(10, 8, 10, 0))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="+ Sheet tab", command=self._add_sheet_tab).pack(side="left")
        ttk.Label(
            toolbar,
            text="  one tab per Excel sheet · run several batches of a sheet at once from its tab",
            foreground="gray",
        ).pack(side="left")
        self.jobs_label = ttk.Label(toolbar, text=f"0 / {MAX_PARALLEL_JOBS} parallel jobs")
        self.jobs_label.pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)
        self.settings_tab = SettingsTab(self.notebook, on_models_changed=self._refresh_model_lists)
        self.notebook.add(self.settings_tab, text="Settings")
        self._add_sheet_tab()

        # Wheel events get swallowed by whichever child widget is under the pointer
        # (Entry, Combobox, Treeview), so per-widget binds are unreliable. Bind once
        # application-wide and route to the Settings canvas while that tab is showing.
        def _global_wheel(event):
            if self.notebook.index(self.notebook.select()) != 0:
                return  # sheet tabs: let their Treeviews scroll natively
            self.settings_tab.scroll_wheel(event)

        self.bind_all("<MouseWheel>", _global_wheel)
        # Esc cancels the running work rather than doing nothing (HIG 5.3).
        self.bind_all("<Escape>", lambda _e: self._on_tab(lambda t: t._stop_parallel()))
        self._build_menubar()
        self.protocol("WM_DELETE_WINDOW", self._on_quit)
        self._tick_jobs_label()

    # ------------------------------------------------------------------ menu bar

    def _build_menubar(self):
        """A real macOS menu bar — the primary discovery surface on this platform, and
        the one thing no Mac app may omit. Every item carries a keyboard equivalent."""
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open Excel File…", accelerator="Cmd+O",
                              command=self._menu_open_excel)
        file_menu.add_command(label="Choose Output Folder…", accelerator="Cmd+Shift+O",
                              command=self._menu_choose_folder)
        file_menu.add_separator()
        file_menu.add_command(label="New Sheet Tab", accelerator="Cmd+T",
                              command=self._add_sheet_tab)
        file_menu.add_command(label="Close Sheet Tab", accelerator="Cmd+W",
                              command=self._menu_close_tab)
        file_menu.add_separator()
        file_menu.add_command(label="Reveal Output Folder in Finder", accelerator="Cmd+Shift+R",
                              command=self._menu_reveal_output)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Select All Rows", accelerator="Cmd+A",
                              command=lambda: self._on_tab(lambda t: t._set_all_included(True)))
        edit_menu.add_command(label="Deselect All Rows", accelerator="Cmd+Shift+A",
                              command=lambda: self._on_tab(lambda t: t._set_all_included(False)))
        edit_menu.add_separator()
        edit_menu.add_command(label="Refresh Defaults from Settings", accelerator="Cmd+Shift+D",
                              command=lambda: self._on_tab(lambda t: t._refresh_defaults()))
        menubar.add_cascade(label="Edit", menu=edit_menu)

        self.run_menu = tk.Menu(menubar, tearoff=0)
        self.run_menu.add_command(label="Start Selected Batches", accelerator="Cmd+R",
                                  command=lambda: self._on_tab(lambda t: t._start_selected_batches()))
        self.run_menu.add_command(label="Stop All Batches", accelerator="Cmd+.",
                                  command=lambda: self._on_tab(lambda t: t._stop_parallel()))
        self.run_menu.add_separator()
        self.run_menu.add_command(label="Rebuild Batches", accelerator="Cmd+B",
                                  command=lambda: self._on_tab(lambda t: t._rebuild_batches()))
        self.run_menu.add_command(label="Regenerate Selected Article", accelerator="Cmd+Shift+G",
                                  command=lambda: self._on_tab(lambda t: t._regenerate_selected()))
        menubar.add_cascade(label="Run", menu=self.run_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Settings", accelerator="Cmd+1",
                              command=lambda: self.notebook.select(0))
        view_menu.add_command(label="Next Sheet Tab", accelerator="Cmd+}",
                              command=lambda: self._cycle_tab(1))
        view_menu.add_command(label="Previous Sheet Tab", accelerator="Cmd+{",
                              command=lambda: self._cycle_tab(-1))
        menubar.add_cascade(label="View", menu=view_menu)

        window_menu = tk.Menu(menubar, tearoff=0, name="window")
        menubar.add_cascade(label="Window", menu=window_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Testing & Troubleshooting Guide",
                              command=self._menu_open_testing_guide)
        help_menu.add_command(label="Probe Models (which ones work now)",
                              command=self._menu_probe_hint)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

        # Native macOS wiring: Cmd+, opens Settings via the application menu.
        try:
            self.createcommand("tk::mac::ShowPreferences", lambda: self.notebook.select(0))
            self.createcommand("tk::mac::Quit", self._on_quit)
        except tk.TclError:
            pass

        for sequence, handler in [
            ("<Command-o>", self._menu_open_excel),
            ("<Command-O>", self._menu_choose_folder),
            ("<Command-t>", lambda: self._add_sheet_tab()),
            ("<Command-w>", self._menu_close_tab),
            ("<Command-R>", self._menu_reveal_output),
            ("<Command-a>", lambda: self._on_tab(lambda t: t._set_all_included(True))),
            ("<Command-A>", lambda: self._on_tab(lambda t: t._set_all_included(False))),
            ("<Command-D>", lambda: self._on_tab(lambda t: t._refresh_defaults())),
            ("<Command-r>", lambda: self._on_tab(lambda t: t._start_selected_batches())),
            ("<Command-period>", lambda: self._on_tab(lambda t: t._stop_parallel())),
            ("<Command-b>", lambda: self._on_tab(lambda t: t._rebuild_batches())),
            ("<Command-G>", lambda: self._on_tab(lambda t: t._regenerate_selected())),
            ("<Command-Key-1>", lambda: self.notebook.select(0)),
            ("<Command-braceright>", lambda: self._cycle_tab(1)),
            ("<Command-braceleft>", lambda: self._cycle_tab(-1)),
        ]:
            self.bind_all(sequence, lambda _e, fn=handler: (fn(), "break")[1])

        self._sync_menu_state()

    def _sync_menu_state(self):
        """Menu items must reflect current state (HIG 1.3): grey out what can't run."""
        tab = self._current_tab()
        has_sheet = tab is not None and tab.excel_path is not None and tab.output_dir is not None
        running = tab is not None and tab._tab_busy()
        try:
            self.run_menu.entryconfig("Start Selected Batches",
                                      state="normal" if has_sheet else "disabled")
            self.run_menu.entryconfig("Stop All Batches",
                                      state="normal" if running else "disabled")
            self.run_menu.entryconfig("Rebuild Batches",
                                      state="normal" if has_sheet else "disabled")
            self.run_menu.entryconfig("Regenerate Selected Article",
                                      state="normal" if has_sheet and not running else "disabled")
        except tk.TclError:
            pass
        self.after(700, self._sync_menu_state)

    # ------------------------------------------------------------- menu actions

    def _current_tab(self):
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except (tk.TclError, KeyError):
            return None
        return current if isinstance(current, BatchTab) else None

    def _on_tab(self, action):
        tab = self._current_tab()
        if tab is None:
            self.notebook.select(1) if self.sheet_tabs else None
            tab = self._current_tab()
        if tab is not None:
            action(tab)

    def _menu_open_excel(self):
        tab = self._current_tab() or (self.sheet_tabs[0] if self.sheet_tabs else None)
        if tab is None:
            return
        self.notebook.select(tab)
        tab._choose_excel()

    def _menu_choose_folder(self):
        self._on_tab(lambda t: t._choose_folder())

    def _menu_close_tab(self):
        tab = self._current_tab()
        if tab is None:
            return
        if tab._tab_busy():
            messagebox.showwarning("Batch running", "Stop this sheet's batches before closing the tab.")
            return
        if len(self.sheet_tabs) <= 1:
            messagebox.showinfo("Last tab", "At least one sheet tab stays open.")
            return
        self.notebook.forget(tab)
        self.sheet_tabs.remove(tab)

    def _menu_reveal_output(self):
        tab = self._current_tab()
        if tab is None or tab.output_dir is None:
            messagebox.showinfo("No output folder", "Choose an output folder first.")
            return
        subprocess.run(["open", str(tab.output_dir)], check=False)

    def _menu_open_testing_guide(self):
        guide = Path(__file__).resolve().parent / "TESTING.md"
        if guide.exists():
            subprocess.run(["open", str(guide)], check=False)

    def _menu_probe_hint(self):
        messagebox.showinfo(
            "Check which models work",
            "In Terminal, from the project folder:\n\n"
            "    .venv/bin/python tests_probe_models.py\n\n"
            "It lists every free model that works right now, with speed, and prints a "
            "recommendation. Put that model in Settings → Model.",
        )

    def _cycle_tab(self, step: int):
        tabs = self.notebook.tabs()
        if not tabs:
            return
        index = (self.notebook.index(self.notebook.select()) + step) % len(tabs)
        self.notebook.select(index)

    def _on_quit(self):
        """Persist window geometry, then stop cleanly."""
        try:
            cfg = config_manager.load_config()
            cfg["window_geometry"] = self.geometry()
            config_manager.save_config(cfg)
        except Exception:  # noqa: BLE001 - never block quitting on a config write
            logger.exception("Could not save window geometry")
        if self.job_manager.any_running():
            if not messagebox.askyesno("Batches running",
                                       "Batches are still running. Quit anyway?\n"
                                       "Finished rows are already saved."):
                return
            self.job_manager.stop_all()
        self.destroy()

    def _refresh_model_lists(self):
        """Settings fetched a new catalogue — every sheet tab offers it too."""
        for tab in self.sheet_tabs:
            tab._refresh_parallel_models()

    def _add_sheet_tab(self):
        if len(self.sheet_tabs) >= MAX_PARALLEL_JOBS:
            messagebox.showinfo(
                "Enough tabs",
                f"{MAX_PARALLEL_JOBS} sheet tabs is the practical ceiling — that already "
                f"matches the maximum number of parallel jobs.",
            )
            return
        tab = BatchTab(self.notebook, job_manager=self.job_manager,
                       on_title_change=self._rename_tab, job_owner=self.job_owner)
        self.sheet_tabs.append(tab)
        self.notebook.add(tab, text=f"Sheet {len(self.sheet_tabs)}")
        self.notebook.select(tab)

    def _rename_tab(self, tab, title: str):
        try:
            self.notebook.tab(tab, text=title)
        except tk.TclError:
            pass

    def _tick_jobs_label(self):
        running = sum(1 for jid in self.job_manager.jobs if self.job_manager.is_running(jid))
        self.jobs_label.config(
            text=f"{running} running · {len(self.job_manager.jobs)} / {MAX_PARALLEL_JOBS} parallel jobs"
        )
        self.after(1000, self._tick_jobs_label)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
