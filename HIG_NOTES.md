# macOS HIG conformance — what this app does, and what it can't

The UI is Tkinter (Tk 9.0.4) rendering through the native **aqua** theme, so controls are
real macOS controls at system metrics and the system font (`.AppleSystemUIFont`) is used
throughout. Tkinter cannot reach several Cocoa-only surfaces; those are listed honestly
below rather than quietly skipped.

Conformance is enforced by `tests_hig_check.py` (9 checks), not by inspection.

## Implemented

| HIG rule | Status |
|---|---|
| 1.1 Standard menus | File · Edit · Run · View · Window · Help, plus the system app menu |
| 1.2 Shortcut on every menu action | Enforced by test — a menu command without an accelerator fails the build |
| 1.3 Menus reflect state | Run items grey out with no sheet loaded; Stop only enables while running |
| 1.4 Contextual menus | Right-click a row → Regenerate |
| 1.5 App menu / Settings | `tk::mac::ShowPreferences` wired, so **Cmd+,** opens Settings from the app menu |
| 2.1 Resizable, sensible minimum | Freely resizable, floor 880×620 |
| 2.5 Remember window state | Size and position persist to `config.yaml` on quit |
| 2.6 Traffic lights | Standard title bar, untouched |
| 5.1 Cmd shortcuts | See table below |
| 5.3 Esc cancels | Esc stops this tab's running batches |
| 6.6 Multi-selection | Rows table is `extended`: Cmd+Click and Shift+Click |
| 7.3 Don't interrupt | Progress is inline per batch; alerts only for genuine failures and quit-while-running |
| 9.1 System fonts | `.AppleSystemUIFont` at system sizes, no hardcoded faces |
| 9.4 Dark Mode | `NSRequiresAquaSystemAppearance=false` so the bundle follows system appearance |
| 9.6 Spacing | 8pt grid (8/16 padding) |

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Cmd+O | Open Excel file |
| Cmd+Shift+O | Choose output folder |
| Cmd+T | New sheet tab |
| Cmd+W | Close sheet tab |
| Cmd+Shift+R | Reveal output folder in Finder |
| Cmd+A / Cmd+Shift+A | Select / deselect all rows |
| Cmd+Shift+D | Refresh defaults from Settings |
| Cmd+R | Start selected batches |
| Cmd+. or Esc | Stop all batches in this tab |
| Cmd+B | Rebuild batches |
| Cmd+Shift+G | Regenerate selected article |
| Cmd+1 | Settings |
| Cmd+{ / Cmd+} | Previous / next sheet tab |
| Cmd+, | Settings (via the macOS app menu) |

## Not achievable in Tkinter

These need AppKit/SwiftUI. They are genuinely absent, not approximated:

- **Vibrancy and materials** (9.2) — no `NSVisualEffectView`; backgrounds are solid.
- **Source-list sidebar** (4.2) — Tk has no sidebar style; navigation uses a tab bar instead.
- **SF Symbols** (3.5) — toolbar/menu items are text-only.
- **VoiceOver labels** (11.1) — Tk exposes no accessibility API, so screen-reader support
  is effectively absent. This is the most serious gap.
- **Reduce Motion / Reduce Transparency / Increase Contrast / Bold Text** (9.5, 11.3–11.7) —
  the settings aren't readable from Tk.
- **Quick Look, Spotlight indexing, Share menu, Services, App Intents** (8.2–8.6).
- **Customizable unified toolbar** (3.1, 3.2) — controls sit in the window, not the title bar.
- **Document proxy icon / edited dot** (2.4) — not a document-based app.

If native-grade polish and accessibility matter, the fix is a SwiftUI rewrite of the
front end against the same Python engine (`batch_runner.py`, `orchestrator.py`), not more
Tk work — those APIs simply aren't reachable from here.
