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
![Tests](https://img.shields.io/badge/tests-15%20suites-brightgreen)

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
- **Model health, with a traffic light** — every model is shown green (answering),
  yellow (daily quota used up or a passing outage — it comes back) or red (restricted,
  not offered, or not a writer at all), in the macOS traffic-light shades. Press
  **Models…** or Cmd+Shift+M. See [Model reliability](#model-reliability).
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
- **Sheet columns carry meaning** — only `Title` and `Scope` drive the writing; a `Year`
  column becomes a hard knowledge and citation cutoff, and any number of author columns
  merge into the PDF byline. See [Excel Format](#excel-format).

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

| Column | Required | What it does | Accepted names |
|---|---|---|---|
| Title | yes | **Drives generation.** Shown in the rows table and used as the PDF title and filename. | Title, Article Title, Proposed Article Title, Paper Title, Headline, Topic |
| Scope | yes | **Drives generation.** The brief the article is written from. | Synopsis, Scope, Brief, Description, Summary, Abstract, Details, Outline |
| Year | no | Knowledge and citation cutoff — see below. | Year, Publication Year, Target Year, Cutoff Year |
| Author | no | Printed as the byline, between the title and the abstract. | Author Names, Author Name, Authors, Author, Co-Authors, Writer |
| Subject | no | **Ignored.** Recognised only so it can't be mistaken for the title. | Subject Area, Subject, Domain, Discipline, Category, Field |
| Status | auto-added | written back by the app: `Pending` / `Success` / `Failed` | |
| Notes | auto-added | error detail or output filename | |

Column order doesn't matter, and extra columns are ignored.

**Only Title and Scope feed the writing.** Everything else is metadata. This matters:
a sheet whose first column is `Subject Area` used to have that column matched as the
title, so every article was written about "Machine Learning" rather than its actual
subject. Header matching now scores an exact name above a partial one, so the real
`Title` column wins no matter what sits to its left.

**Year is a hard cutoff, in both directions.** When a row has a year, the article is
written as though the present year is that year — only research, methods and events
known by then — and **every reference must carry a publication year at or before it**.
That second half is enforced, not merely requested: citations dated after the cutoff are
discarded, replacements are requested, and the surviving list is renumbered so `[1]`…
`[20]` stays contiguous. Rows with an empty Year cell are unconstrained.

**Authors can span several columns.** `Author Name 1`, `Author Name 2`, `Co-Authors` are
all collected, split on `;` `,` and "and", de-duplicated, and joined into one byline.

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

free_models:                       # full provider catalogue, working models first
  - google/gemma-4-26b-a4b-it:free
  - nvidia/nemotron-3-super-120b-a12b:free
  # ...every :free model the provider offers
```

Everything here is also editable from the app's **Settings** tab — the file is the
source of truth either way.

**Settings → Fetch free models** pulls the provider's live `:free` catalogue, writes it
to `free_models`, and pushes it into every open sheet tab, so the same complete list is
selectable everywhere. Which of them will actually write an article is a separate
question — see below.

## The daily request allowance (read this first)

Most reports of "the models stopped responding" are this, and no code change fixes it.
OpenRouter's free tier allows a fixed number of requests **per day, across every free
model combined** — the numbers come straight off a 429:

```
X-RateLimit-Limit: 50
X-RateLimit-Remaining: 0
limit_source: openrouter_free_tier_daily
"Add 10 credits to unlock 1000 free model requests per day"
```

One article costs roughly one request per section plus six (outline, intro/conclusion,
editor, abstract, references, one correction pass). So:

| Key | Requests/day | 10-section articles/day |
|---|---|---|
| Free, no credits | 50 | **~3** |
| 10+ credits added | 1000 | ~71 |

A 25-row batch needs ~350 requests. On a free key it does not fail at row 25 — it fails
at row 2, and looks exactly like the models breaking. Because the limit is per *account*
and shared across every model, having 19 models in the pool does not help at all: they
all draw on the same 50.

The app therefore counts every request it makes (health probes included), persists the
count across launches, corrects it from the provider's own headers whenever a 429
arrives, and shows the remainder beside the Run button and in the Models window. Before
a run it checks whether the day's remainder can cover the work and refuses, with the
arithmetic, rather than burning what is left. That check only blocks once the provider
has actually stated a limit — a paid key is never stopped on a guess.

## Model reliability

Free-tier models fail constantly, and the app's job is to make that cheap rather than to
pretend otherwise. Four mechanisms do that, all of them measured rather than assumed:

**A per-article deadline reaches every request.** An article has a wall-clock budget; it
now propagates into the client, so no single request is ever given more time than the
article has left. Without this, one stalling model could spend
`request_timeout x max_retries` on each of an article's ~13 calls — with a 19-model
chain that is 6,840s of waiting against a 675s budget, which is exactly why articles
"failed" after 22 minutes with a vague message. Measured on a total stall: 108s to a
clear failure, against 675s of budget.

**The fallback chain is short and health-ranked.** `max_models_per_call` (default 4)
caps how many models one call may walk, ordered fastest-known-good first. Trying all 19
is not resilience; it is a self-inflicted stall.

**A circuit breaker benches repeat offenders.** Two consecutive hard failures and a
model sits out for 15 minutes instead of being retried on every call. HTTP 403/404 skip
the second chance — no retry changes a restriction.

**Health checks ask for prose, not a ping — carefully.** A 1-token ping cannot tell a
writer from a classifier: `nvidia/nemotron-3.5-content-safety` passes the ping and then
answers a 1,000-word section request with three words. The prose probe catches that, but
it must not cap `max_tokens`: reasoning models spend their whole budget on hidden
reasoning before emitting a word, so a cap returns empty content with
`finish_reason: "length"` and wrongly condemns them. Seven working models were marked
"cannot write prose" that way. Truncation is now read as a budget artefact, not a
verdict; probes run in two stages (cheap ping for all, prose only for the survivors) and
the prose answer is cached for six hours, because probing is not free — it spends the
same daily allowance the articles need.

### Reading the traffic light

| Colour | Meaning | What to do |
|---|---|---|
| 🟢 Green `#28C840` | Answering now, with measured speed | Nothing — use it |
| 🟡 Yellow `#FEBC2E` | Daily free quota used up, or the provider is having an outage | Wait — quotas reset at 00:00 UTC, or add credits at openrouter.ai |
| 🔴 Red `#FF5F57` | Not offered to this account, restricted to other harnesses, or not a writer | Pick a different model; this one will not work today |

A daily quota is an account limit at the provider, not a setting in this app. On a free
key most of the catalogue sits yellow most of the time — that is the tier, not a bug.

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

15 test suites, no test framework dependency — each is a standalone script printing
pass/fail with a plain-English description of what it proved.

```bash
.venv/bin/python tests_client_logic.py       # AI client: fallback, quota handling, cancellation
.venv/bin/python tests_manual_check.py       # Excel round-trip, PDF render, fake-client pipeline
.venv/bin/python tests_parallel_check.py     # concurrency safety: shared writes, request budget
.venv/bin/python tests_multisheet_check.py   # 5 sheets → 5 jobs → 5 destinations, header edge cases
.venv/bin/python tests_resume_check.py       # stop/resume from the exact failure point
.venv/bin/python tests_hig_check.py          # macOS menu bar / keyboard-shortcut conformance
.venv/bin/python tests_models_everywhere_check.py  # model catalogue reaches every tab, timing log, PDF has no disclaimer
.venv/bin/python tests_sheet_rules_check.py  # column mapping, year cutoff, author byline
.venv/bin/python tests_model_health_check.py # fail-fast bounds, circuit breaker, traffic-light states
.venv/bin/python tests_quota_check.py        # daily allowance accounting, pre-flight refusal, probe cost
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
  is active, the References section is produced by a language model. The citations follow
  IEEE formatting but are **not verified against real publications**: authors, titles,
  venues, years and DOIs may not exist. **Verify or replace every citation before any
  academic submission.** The PDF deliberately carries no printed disclaimer — it is a
  submission draft, so the caveat lives here and in the app rather than on the page. That
  choice moves the responsibility to you; it does not reduce it.
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
