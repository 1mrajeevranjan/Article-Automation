"""The Models window: every model, its traffic-light state, and why.

A dropdown of 19 names tells the user nothing about which of them will actually write
an article. This does, in the one glance the macOS traffic lights already train people
to read: green answers now, yellow will come back (quota or a passing outage), red will
not (restricted, not offered, or not a writer at all).
"""

import threading
import tkinter as tk
from tkinter import ttk

import config_manager
import model_health as health
from ai_client import probe_models

DOT = 11          # diameter of the status dot, px
ROW_PAD = 6


class ModelPanel(tk.Toplevel):
    """Non-modal, one per app — reopening raises the existing window."""

    _instance: "ModelPanel | None" = None

    @classmethod
    def show(cls, parent, on_pick=None):
        if cls._instance is not None and cls._instance.winfo_exists():
            cls._instance.deiconify()
            cls._instance.lift()
            cls._instance.focus_force()
            cls._instance.on_pick = on_pick or cls._instance.on_pick
            return cls._instance
        cls._instance = cls(parent, on_pick=on_pick)
        return cls._instance

    def __init__(self, parent, on_pick=None):
        super().__init__(parent)
        self.on_pick = on_pick
        self.title("Models")
        self.geometry("560x520")
        self.minsize(460, 320)
        self.transient(parent)
        self._checking = False

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x")
        ttk.Label(header, text="Model availability",
                  font=("", 15, "bold")).pack(side="left")
        self.check_button = ttk.Button(header, text="Check now", command=self.refresh)
        self.check_button.pack(side="right")

        self.summary = ttk.Label(container, text="", foreground="gray", wraplength=500,
                                 justify="left")
        self.summary.pack(fill="x", pady=(4, 10))

        legend = ttk.Frame(container)
        legend.pack(fill="x", pady=(0, 10))
        for colour, text in ((health.GREEN, "Ready"),
                             (health.YELLOW, "Quota or outage — comes back"),
                             (health.RED, "Unavailable to this account")):
            item = ttk.Frame(legend)
            item.pack(side="left", padx=(0, 14))
            _dot(item, colour).pack(side="left")
            ttk.Label(item, text=text, foreground="gray").pack(side="left", padx=(5, 0))

        canvas_frame = ttk.Frame(container)
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(canvas_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.rows_frame = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>",
                             lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        self.bind("<Escape>", lambda _e: self.withdraw())
        self.protocol("WM_DELETE_WINDOW", self.withdraw)

        self.render()
        self.refresh()

    # ------------------------------------------------------------------ rendering

    def _on_wheel(self, event):
        if not self.winfo_exists() or self.focus_displayof() is None:
            return
        # macOS trackpads send small deltas; dividing by 120 truncates them to zero.
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def render(self):
        for child in self.rows_frame.winfo_children():
            child.destroy()

        models = config_manager.load_config().get("free_models") or []
        rows = health.REGISTRY.snapshot(models)

        counts = {state: 0 for state in (health.READY, health.LIMITED,
                                         health.BLOCKED, health.UNKNOWN)}
        for row in rows:
            counts[row["state"]] += 1

        for row in rows:
            self._render_row(row)

        ready, limited, blocked = (counts[health.READY], counts[health.LIMITED],
                                   counts[health.BLOCKED])
        parts = [f"{ready} ready"]
        if limited:
            parts.append(f"{limited} limited")
        if blocked:
            parts.append(f"{blocked} unavailable")
        note = " · ".join(parts)
        if limited:
            note += ("    Free-tier daily quotas reset at 00:00 UTC; adding credits at "
                     "openrouter.ai lifts them.")
        self.summary.config(text=note)

    def _render_row(self, row: dict):
        line = ttk.Frame(self.rows_frame)
        line.pack(fill="x", pady=ROW_PAD // 2)

        _dot(line, row["colour"]).pack(side="left", padx=(2, 9))

        text = ttk.Frame(line)
        text.pack(side="left", fill="x", expand=True)
        ttk.Label(text, text=row["short"], font=("", 12)).pack(anchor="w")

        detail = row["reason"] or row["label"]
        if row["state"] == health.READY and row["latency"]:
            detail = f"Ready · {row['latency']:.0f}s for a section"
        elif row["state"] == health.READY:
            detail = "Ready"
        ttk.Label(text, text=detail, foreground="gray").pack(anchor="w")

        if self.on_pick is not None and row["state"] != health.BLOCKED:
            ttk.Button(line, text="Use",
                       command=lambda m=row["model"]: self._pick(m)).pack(side="right")

    def _pick(self, model: str):
        if self.on_pick:
            self.on_pick(model)
        self.withdraw()

    # -------------------------------------------------------------------- probing

    def refresh(self):
        """Re-probe in the background so the window stays responsive."""
        if self._checking:
            return
        self._checking = True
        self.check_button.config(text="Checking…", state="disabled")
        self.summary.config(text="Checking every model with a real writing request…")

        config = config_manager.load_config()
        models = list(config.get("free_models") or [])

        def worker():
            try:
                # deep=True asks for real prose: the only way to tell a writer from a
                # classifier that answers a ping and then produces three words.
                probe_models(models, config, deep=True)
            except Exception:  # noqa: BLE001 - the window must never die on a probe
                pass
            finally:
                self.after(0, self._probe_done)

        threading.Thread(target=worker, daemon=True).start()

    def _probe_done(self):
        self._checking = False
        if not self.winfo_exists():
            return
        self.check_button.config(text="Check now", state="normal")
        self.render()


def _dot(parent, colour: str) -> tk.Canvas:
    """A filled circle in the exact macOS traffic-light shade."""
    canvas = tk.Canvas(parent, width=DOT, height=DOT, highlightthickness=0,
                       bg=parent.winfo_toplevel().cget("bg"))
    canvas.create_oval(1, 1, DOT - 1, DOT - 1, fill=colour, outline="")
    return canvas
