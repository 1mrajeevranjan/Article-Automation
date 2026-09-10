# AI Article Automation

A desktop macOS app that turns an Excel list of article assignments into finished,
formatted PDFs — written by a multi-agent AI pipeline, run in parallel across multiple
sheets and models, with resumable batch tracking and a native menu bar.

Point it at a spreadsheet with `Title` and `Scope` columns, pick a folder, and it
generates each article as a structured, styled PDF: outline → sections written in
parallel → intro/conclusion → word-count validation and correction → abstract & keywords
→ references (IEEE style, optional) → rendered PDF — while writing status back into the
Excel file row by row so any run can be stopped and resumed exactly where it left off.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Tests](https://img.shields.io/badge/tests-11%20suites-brightgreen)

## Features

- **Multi-agent writing pipeline** — outline, writer, editor/validator, abstract &
  keywords, and references are separate agents with their own prompts and (optionally)
  their own models, coordinated by an orchestrator that owns the word-count correction
  loop and per-article timeout.
- **Parallel batches, multiple sheets** — run up to 5 batches at once, from the same
  Excel sheet or five different sheets, each with its own model and its own output
  folder. A shared request budget keeps concurrent batches from starving each other.
- **Resumable by design** — status is written back into the Excel file after every row.
  Stop mid-batch, close the app, reopen days later — finished rows are never redone.
- **Model fallback and live health checks** — a configurable chain of free/paid models
  with automatic failover, and an "Auto" mode that pings every candidate before a run and
  routes work only to the ones actually responding right now.
- **Two writing styles, grounded in real style guides** — IEEE Research Paper (IEEE
  Editorial Style Manual: formal, third-person, numbered references) or Blog Post
  (HubSpot voice/tone: educational, conversational, second-person).
- **Regeneration** — not happy with one article? Regenerate it without touching the rest
  of the batch; it's saved as `12.1_Title.pdf`, `12.2_Title.pdf`, and so on.
- **Native macOS app** — real menu bar (File/Edit/Run/View/Window/Help), full keyboard
  shortcuts, window geometry persists across launches, resizable panes.
- **Flexible spreadsheet reading** — finds the header row even when it isn't row 1,
  accepts common column-name variants (`Synopsis`/`Brief`/`Description` all resolve to
  `Scope`), and skips repeated header rows in sheets grouped by author/section.

## Requirements

- macOS (uses AppleScript/`open`/Tk's `aqua` theme; the CLI core is cross-platform)
- Python 3.10+
- An OpenAI-compatible API key (OpenRouter, NVIDIA NIM, or any compatible provider)

## Quick Start

```bash
git clone <this-repo>
cd article_automation

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# edit .env and set AI_API_KEY=<your key>
```

Then either:

**GUI (recommended)** — double-click `AI Article Automation.app`, or from source:

```bash
.venv/bin/python gui_app.py
```

**CLI** — for scripted/headless runs against a single sheet:

```bash
.venv/bin/python main.py path/to/assignments.xlsx
```

## Excel Format

One row per article. Column names are matched case-insensitively and by common synonym
— you don't need to rename your sheet to fit the tool.

| Column | Required | Accepted names |
|---|---|---|
| Title | yes | Title, Article Title, Proposed Article Title, Topic, Paper Title, Headline |
| Scope | yes | Scope, Synopsis, Brief, Description, Summary, Abstract, Details, Outline |
| Author | no | Author, Writer, Authors |
| Status | auto-added | written back by the app: `Pending` / `Success` / `Failed` |
| Notes | auto-added | error detail or output filename |

The header row doesn't have to be row 1 — it's located automatically within the first 10
rows, so a title/author line above the header is fine. A repeated header row (common in
sheets grouped by author or section) is detected and skipped rather than read as an
article.

## Configuration (`config.yaml`)

```yaml
ai_provider:
  base_url: https://openrouter.ai/api/v1
  api_key_env: AI_API_KEY          # env var name, not the key itself
  model: google/gemma-4-26b-a4b-it:free
  max_concurrent_requests: 2       # parallel section writes within ONE article
  global_max_concurrent_requests: 6 # ceiling across ALL parallel batches combined
  article_timeout_seconds: 300     # hard per-article ceiling; a stuck one fails, batch continues
  provider_routing:
    ignore: [nvidia]               # steer away from a known-unreliable upstream

max_consecutive_failures: 3        # halt a batch after N failures in a row, don't grind on

writing_style: ieee_paper          # ieee_paper | blog_post
pdf:
  font: Palatino                   # Book Antiqua substitute (Microsoft-licensed, absent on macOS)
  page_size: A4

defaults:
  word_count: 10500
  sections: 10                     # includes Introduction + Conclusion

free_models:                       # fallback chain, in preference order
  - google/gemma-4-26b-a4b-it:free
  - nvidia/nemotron-3-super-120b-a12b:free
```

Everything here is also editable from the app's **Settings** tab — the file is the
source of truth either way.

## Architecture

```
main.py / gui_app.py
        │
        ▼
  orchestrator.py ── per-article pipeline, word-count correction loop, timeout
        │
        ▼
   agents/
     outline_agent          → section headings
     writer_agent            → one section (run in parallel across sections)
     intro_conclusion_agent  → written after the body exists
     editor_validator_agent  → owns the correction loop
     abstract_keywords_agent → summary + 10 keywords
     references_agent        → IEEE citations (ieee_paper style only)
     pdf_output_agent         → renders + writes Excel status
        │
        ▼
    ai_client.py ── model fallback chain, daily-quota blacklist, cooperative cancellation
        │
        ▼
  batch_runner.py ── multi-job engine: shared workbook registry, shared request budget
```

- **`ai_client.py`** — not a thin wrapper. It classifies errors (only genuinely
  transient ones are retried), blacklists a model for the rest of the run the moment it
  reports a daily quota, and supports cooperative cancellation so Stop takes effect in
  seconds, not after the current retry loop exhausts.
- **`batch_runner.py`** — the parallel engine. A `WorkbookRegistry` gives every job
  touching one Excel file a single shared handle (openpyxl rewrites the whole workbook on
  save; two independent handles on one file silently clobber each other's writes). A
  shared `Semaphore` caps total in-flight AI requests across every running batch.
- **`gui_app.py`** — Tkinter, rendered through the native `aqua` theme. One tab per open
  sheet; each tab can run several of its own batches in parallel or hand batches off to
  other sheets' tabs, all coordinated through one `JobManager`.

## Testing

11 test suites, no test framework dependency — each is a standalone script printing
pass/fail with a plain-English description of what it proved.

```bash
.venv/bin/python tests_client_logic.py       # AI client: fallback, quota handling, cancellation
.venv/bin/python tests_manual_check.py       # Excel round-trip, PDF render, fake-client pipeline
.venv/bin/python tests_parallel_check.py     # concurrency safety: shared writes, request budget
.venv/bin/python tests_multisheet_check.py   # 5 sheets → 5 jobs → 5 destinations, header edge cases
.venv/bin/python tests_resume_check.py       # stop/resume from the exact failure point
.venv/bin/python tests_hig_check.py          # macOS menu bar / keyboard-shortcut conformance
# ...and 5 more — see TESTING.md
```

When articles start failing, run this first:

```bash
.venv/bin/python tests_probe_models.py
```

It checks every configured model against the live API and reports which ones are
actually answering right now, with latency — the fastest real diagnostic when a provider
is degraded.

Full test index, what each one guards against, and the reasoning behind the resilience
design: see **[TESTING.md](TESTING.md)**.

## Known Limitations

- **macOS accessibility** — Tkinter exposes no VoiceOver/accessibility API. This is a
  real gap for accessibility-dependent users; see **[HIG_NOTES.md](HIG_NOTES.md)** for
  the full list of what native AppKit could do here that Tk cannot (vibrancy, Quick Look,
  Spotlight indexing, Share menu, App Intents).
- **References are AI-generated, not real citations** — when `writing_style: ieee_paper`
  is active, the References section is clearly disclaimed in the PDF as illustrative
  structure only. Verify or replace every citation before any academic submission.
- **Free-tier model reliability varies** — daily quotas and upstream outages are real and
  outside this app's control. The fallback chain and health-check probe exist because of
  this, not as decoration.

## Security

- API keys are read from environment variables only (`.env`, gitignored) — never written
  to `config.yaml` or logged.
- Rotate any key that has ever been pasted into a chat, ticket, or screen share; treat it
  as compromised the moment it left your terminal.

## License

Internal/personal project — no license file present. Add one before distributing.
