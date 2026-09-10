"""Headless diagnostic for the Settings tab scroll region.

Builds the real SettingsTab, forces layout, and reports whether the canvas is
actually scrollable and whether the wheel handler moves it. No mainloop, no window
interaction needed.

Run: .venv/bin/python tests_scroll_check.py
"""

import tkinter as tk
from tkinter import ttk

from dotenv import load_dotenv

load_dotenv()

import gui_app


class FakeWheel:
    def __init__(self, delta):
        self.delta = delta


def main():
    root = tk.Tk()
    root.geometry("900x700")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    tab = gui_app.SettingsTab(notebook)
    notebook.add(tab, text="Settings")
    root.update_idletasks()
    root.update()

    canvas = tab.canvas
    bbox = canvas.bbox("all")
    canvas_h = canvas.winfo_height()
    inner_h = tab.inner.winfo_reqheight()
    scrollregion = canvas.cget("scrollregion")

    print(f"canvas height     : {canvas_h}")
    print(f"inner req height  : {inner_h}")
    print(f"canvas bbox('all'): {bbox}")
    print(f"scrollregion      : {scrollregion!r}")
    print(f"content taller than viewport: {inner_h > canvas_h}")

    before = canvas.yview()
    tab.scroll_wheel(FakeWheel(-3))   # macOS-style small negative delta = scroll down
    root.update()
    after = canvas.yview()

    print(f"yview before wheel: {before}")
    print(f"yview after  wheel: {after}")

    if not scrollregion or scrollregion == "0 0 0 0":
        print("\nBUG: scrollregion never set — canvas has nothing to scroll.")
    elif before == after:
        print("\nBUG: wheel handler ran but view did not move.")
    else:
        print("\nOK: direct handler call scrolls (view moved).")

    root.destroy()

    # --- Now test the REAL event-delivery path through the full App ---
    print("\n=== event delivery through App.bind_all ===")
    app = gui_app.App()
    app.update_idletasks()
    app.update()

    settings = None
    for child in app.winfo_children():
        if isinstance(child, ttk.Notebook):
            settings = app.nametowidget(child.tabs()[0])
            child.select(0)
            break
    app.update()

    canvas2 = settings.canvas
    before2 = canvas2.yview()

    # Generate a wheel event on a deep child widget (an Entry), like a real user hovering it
    target = None
    def find_entry(w):
        nonlocal target
        for c in w.winfo_children():
            if isinstance(c, (ttk.Entry, ttk.Combobox)) and target is None:
                target = c
            find_entry(c)
    find_entry(settings.inner)

    print(f"event target widget: {target}")
    if target is not None:
        target.event_generate("<MouseWheel>", delta=-3, x=10, y=10)
        app.update()
    after2 = canvas2.yview()

    print(f"yview before event : {before2}")
    print(f"yview after  event : {after2}")
    if before2 == after2:
        print("\nBUG CONFIRMED: bind_all does not deliver wheel events from child widgets.")
    else:
        print("\nOK: bind_all delivers wheel events correctly.")

    app.destroy()


if __name__ == "__main__":
    main()
