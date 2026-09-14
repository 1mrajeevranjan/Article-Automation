"""macOS HIG conformance checks for what Tkinter can actually deliver.

Covered: menu bar presence and structure, keyboard equivalents on every menu command,
dynamic enable/disable, window minimum size, geometry persistence, multi-selection.

Out of scope for Tk (documented in HIG_NOTES.md rather than silently skipped):
vibrancy/materials, SF Symbols, VoiceOver labels, Quick Look, Spotlight, Share menu.

Run: .venv/bin/python tests_hig_check.py
"""

import os
import shutil
import tkinter as tk
from pathlib import Path

import openpyxl

os.environ.setdefault("AI_API_KEY", "test-key")

from dotenv import load_dotenv

load_dotenv()

import config_manager
import gui_app

import usage_tracker
usage_tracker.use_isolated_state()   # never count test calls against the real daily allowance

TMP = Path("/tmp/hig_check")


def menu_of(app):
    return app.nametowidget(app.cget("menu"))


def real_entries(menu):
    """Indices of entries that actually carry a label.

    X11 menus start with a tearoff entry that macOS menus do not have, and asking a
    tearoff for its -label raises TclError. Every walk of a menu has to skip those, or
    the same test passes on a Mac and explodes on a Linux CI runner.
    """
    return [i for i in range(menu.index("end") + 1)
            if menu.type(i) not in ("separator", "tearoff")]


def labels(menu):
    return [menu.entrycget(i, "label") for i in real_entries(menu)]


def items(menu):
    """(label, accelerator, type) for each entry."""
    return [(menu.entrycget(i, "label"),
             menu.entrycget(i, "accelerator"),
             menu.type(i)) for i in real_entries(menu)]


def test_menu_walk_survives_x11_tearoffs():
    """Guards the walker itself, since macOS never produces the entry that broke it.

    X11 adds a tearoff as entry 0 of every menu; asking it for -label raises TclError,
    which is how this suite passed on a Mac and failed on a Linux CI runner.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        menu = tk.Menu(root, tearoff=1)
        menu.add_command(label="Open", accelerator="Cmd+O")
        menu.add_separator()
        menu.add_command(label="Close", accelerator="Cmd+W")

        kinds = [menu.type(i) for i in range(menu.index("end") + 1)]
        assert "separator" in kinds, kinds
        assert labels(menu) == ["Open", "Close"], labels(menu)
        assert [text for text, _, _ in items(menu)] == ["Open", "Close"]
        if "tearoff" in kinds:
            print(f"menu walk skips the X11 tearoff and separator {kinds}: OK")
        else:
            print(f"menu walk skips separators {kinds} "
                  f"(no tearoff on this platform): OK")
    finally:
        root.destroy()


def test_menu_bar_present_and_standard():
    app = gui_app.App()
    app.update()
    names = labels(menu_of(app))

    for required in ("File", "Edit", "View", "Window", "Help"):
        assert required in names, f"menu bar missing '{required}': {names}"
    print(f"menu bar present with standard menus {names}: OK")
    app.destroy()


def test_every_menu_command_has_a_shortcut():
    """HIG 1.2 — menu items that perform an action need a keyboard equivalent."""
    app = gui_app.App()
    app.update()
    mb = menu_of(app)

    missing = []
    for i in real_entries(mb):
        label = mb.entrycget(i, "label")
        if label in ("Window", "Help"):     # system-managed / informational
            continue
        submenu = app.nametowidget(mb.entrycget(i, "menu"))
        for text, accel, kind in items(submenu):
            if kind == "command" and not accel:
                missing.append(f"{label} ▸ {text}")

    assert not missing, "menu commands without keyboard shortcuts: " + ", ".join(missing)
    print("every actionable menu item has a keyboard equivalent: OK")
    app.destroy()


def test_menu_items_reflect_state():
    """HIG 1.3 — items that cannot run must be disabled."""
    app = gui_app.App()
    app.update()

    # No sheet chosen yet: running is impossible, so Run items are greyed.
    assert str(app.run_menu.entrycget("Start Selected Batches", "state")) == "disabled", \
        "Start should be disabled before a sheet and output folder are chosen"
    print("Run menu disabled while no sheet/output folder is selected: OK")

    sheet = TMP / "s.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Title", "Scope", "Status", "Notes"])
    for i in range(6):
        ws.append([f"A{i}", f"S{i}", None, None])
    wb.save(sheet)

    tab = app.sheet_tabs[0]
    tab.excel_path = sheet
    tab.output_dir = TMP
    tab._load_rows()
    app.update()
    app._sync_menu_state()
    app.update()

    assert str(app.run_menu.entrycget("Start Selected Batches", "state")) == "normal", \
        "Start should enable once a sheet and folder are set"
    assert str(app.run_menu.entrycget("Stop All Batches", "state")) == "disabled", \
        "Stop should stay disabled while nothing is running"
    print("Run menu enables Start (and keeps Stop disabled) once a sheet is loaded: OK")
    app.destroy()


def test_window_minimum_size_and_geometry_persistence():
    original = config_manager.load_config()
    try:
        app = gui_app.App()
        app.update()
        assert app.minsize() == (880, 620), app.minsize()
        print(f"window minimum size {app.minsize()} keeps the UI usable: OK")

        app.geometry("1000x700+120+90")
        app.update()
        app._on_quit()

        saved = config_manager.load_config().get("window_geometry", "")
        assert saved.startswith("1000x700"), f"geometry not persisted, got {saved!r}"
        print(f"window geometry persisted across quit ({saved}): OK")

        app2 = gui_app.App()
        app2.update()
        assert app2.geometry().startswith("1000x700"), app2.geometry()
        print(f"window reopened at the remembered size ({app2.geometry()}): OK")
        app2.destroy()
    finally:
        config_manager.save_config(original)


def test_multi_selection_supported():
    """HIG 6.6 — Cmd+Click and Shift+Click selection in tables."""
    app = gui_app.App()
    app.update()
    tab = app.sheet_tabs[0]
    mode = str(tab.tree.cget("selectmode"))
    assert mode == "extended", f"table selectmode is {mode!r}, need 'extended' for Cmd/Shift+Click"
    print("rows table supports Cmd+Click / Shift+Click multi-selection: OK")
    app.destroy()


def test_escape_and_native_preferences_wired():
    app = gui_app.App()
    app.update()
    assert app.bind_all("<Escape>"), "Esc must cancel running work"
    commands = app.tk.call("info", "commands", "tk::mac::ShowPreferences")
    assert "ShowPreferences" in str(commands), "Cmd+, should open Settings via the app menu"
    print("Esc bound to stop; Cmd+, wired to Settings through the macOS app menu: OK")
    app.destroy()


if __name__ == "__main__":
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    gui_app.messagebox.showwarning = lambda *a, **k: None
    gui_app.messagebox.showinfo = lambda *a, **k: None
    gui_app.messagebox.showerror = lambda *a, **k: None
    gui_app.messagebox.askyesno = lambda *a, **k: True

    try:
        test_menu_walk_survives_x11_tearoffs()
        test_menu_bar_present_and_standard()
        test_every_menu_command_has_a_shortcut()
        test_menu_items_reflect_state()
        test_window_minimum_size_and_geometry_persistence()
        test_multi_selection_supported()
        test_escape_and_native_preferences_wired()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\nAll macOS HIG checks passed.")
