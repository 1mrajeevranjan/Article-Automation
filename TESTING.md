# Test & diagnostic tools

Run all from the project directory with `.venv/bin/python <script>`.

## When articles start failing — run this first

```bash
.venv/bin/python tests_probe_models.py
```

Probes every `:free` model on your account with a realistic prompt and prints which
ones actually work right now, how fast, and which return junk or leak reasoning text.
Ends with a `RECOMMENDED:` line. Put that model in Settings → Model.

Common failure it will show you:

| Error | Meaning | What to do |
|---|---|---|
| `Rate limit exceeded: free-models-per-day` | Account's daily free quota is spent | Wait for reset, add OpenRouter credits, or use a BYOK-backed model |
| `Upstream error from Nvidia: ResourceExhausted` | Routed to an exhausted upstream host | Already mitigated by `provider_routing.ignore` in config.yaml |
| `The operation was aborted (504)` | Transient upstream timeout | Retried automatically; no action needed |
| `only available on agentic harnesses` | Model not usable via plain API | Pick another model |

## Live end-to-end batch

```bash
.venv/bin/python tests_live_batch.py 3     # generate 3 real articles
```

Runs the real pipeline against the real Excel file and reports per-article status,
duration, which model served it, word count, reference count, and the PDFs written.

## Offline suites (no network, no quota used)

```bash
.venv/bin/python tests_client_logic.py       # fallback chain, daily-cap blacklist, cancellation
.venv/bin/python tests_manual_check.py       # excel round-trip, PDF render, full pipeline w/ fake client
.venv/bin/python tests_stop_check.py         # Stop halts mid-call and leaves rows resumable
.venv/bin/python tests_resume_check.py       # finished rows show Done + untick; resume at failure point
.venv/bin/python tests_parallel_check.py     # multi-job engine: concurrency safety, queueing, budget, cap
.venv/bin/python tests_parallel_gui_check.py # batch-spec parsing, job wiring, 5-job ceiling
.venv/bin/python tests_scroll_check.py       # Settings tab layout fits / scrolls
```

## Parallel Runs

Up to **5 jobs** run at once. A job = one Excel file + one model + an ordered list of
batches, worked through back to back (finishing one batch starts the next automatically).
Jobs may share a sheet or use entirely different sheets.

Two hazards the engine handles, both covered by `tests_parallel_check.py`:

| Hazard | Why it bites | Handling |
|---|---|---|
| Two jobs writing one sheet | openpyxl rewrites the whole workbook on save, so independent handles erase each other's statuses | One shared `ExcelBatch` per file path behind a lock (`WorkbookRegistry`) |
| Request burst | 5 jobs × parallel section writes overwhelm a free tier into 429s | One shared semaphore across all jobs: `ai_provider.global_max_concurrent_requests` (default 6) |

Practical note: jobs sharing the *same* model compete for that model's quota, so
parallelism pays off most when each job gets a different model. The tab warns when you
add a second job on a model already in use.

## Routing diagnostics

```bash
.venv/bin/python tests_routing_probe.py    # which upstream serves each model; models[] array limits
.venv/bin/python tests_routing_probe2.py   # provider pinning + reliability over 10 runs
```

Measured 2026-09-08: bare requests succeeded 8/10; with `provider: {"ignore": ["nvidia"]}`
they succeeded 10/10. That finding is what `ai_provider.provider_routing` in
`config.yaml` encodes.

## How the client survives a flaky free tier

`ai_client.py` does four things beyond a plain API call:

1. **Model fallback chain** — tries `ai_provider.model`, then each entry in `free_models`.
2. **Daily-cap blacklist** — a model that reports `free-models-per-day` is dropped for the
   rest of the process instead of being retried on all ~7 calls of every article.
3. **Error classification** — only transient failures (502/504/timeout/transient 429) are
   retried, with `Retry-After` honoured and capped at 30s.
4. **Cooperative cancellation** — Stop aborts before the next call and interrupts backoff
   sleeps, so a batch halts in seconds, not minutes.
