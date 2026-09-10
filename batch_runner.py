"""Multi-job batch engine: runs up to MAX_PARALLEL_JOBS batch queues concurrently.

A *job* owns one Excel file, one model, and an ordered queue of batch numbers. It works
through them back to back, so finishing one batch automatically starts the next.

Two things make concurrency safe rather than merely fast:

* **One workbook handle per file.** openpyxl rewrites the entire workbook on save, so two
  independent handles on the same sheet silently erase each other's statuses. Every job
  touching a given file shares a single ExcelBatch behind a lock (WorkbookRegistry).
* **A global request budget.** Five jobs each writing sections concurrently would multiply
  into a request burst that free tiers answer with 429s. All jobs share one semaphore, so
  total in-flight calls stay bounded no matter how many jobs run.

No Tk here — the GUI subscribes by passing an event callback, and the engine is testable
headlessly (see tests_parallel_check.py).
"""

import copy
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ai_client import AIClient, AIClientError, ping_models
from excel_io import ExcelBatch
from orchestrator import run_article

logger = logging.getLogger("batch_runner")

MAX_PARALLEL_JOBS = 5


def _mmss(seconds: float) -> str:
    """Durations here run from a few seconds to several minutes — 4m12s reads faster
    than 252.3s at the exact point the user is scanning the log for the slow stage."""
    seconds = int(round(seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s"


class WorkbookRegistry:
    """Shares one ExcelBatch (and one lock) per resolved file path."""

    def __init__(self):
        self._books: dict[str, tuple[ExcelBatch, threading.RLock]] = {}
        self._guard = threading.Lock()

    def _entry(self, path) -> tuple[ExcelBatch, threading.RLock]:
        key = str(Path(path).resolve())
        with self._guard:
            entry = self._books.get(key)
            if entry is None:
                entry = (ExcelBatch(Path(path)), threading.RLock())
                self._books[key] = entry
            return entry

    def read_rows(self, path) -> list:
        batch, lock = self._entry(path)
        with lock:
            return list(batch.read_rows())

    def statuses(self, path) -> dict:
        return {r[0]: (r[4] or "") for r in self.read_rows(path)}

    def write_status(self, path, row_number: int, status: str, notes: str, save: bool = True):
        batch, lock = self._entry(path)
        with lock:
            batch.write_status(row_number, status, notes)
            if save:
                batch.save()

    def save(self, path):
        batch, lock = self._entry(path)
        with lock:
            batch.save()

    def forget(self, path):
        key = str(Path(path).resolve())
        with self._guard:
            self._books.pop(key, None)


def partition_batches(rows: list, batch_size: int) -> list[list]:
    size = max(1, batch_size)
    return [rows[i:i + size] for i in range(0, len(rows), size)]


@dataclass
class Job:
    job_id: int
    excel_path: Path
    output_dir: Path
    model: str
    batch_numbers: list[int]          # 1-based, in the order they should run
    batch_size: int = 25
    word_count: int = 2500
    sections: int = 5
    status: str = "Queued"            # Queued | Running | Stopped | Failed | Done
    current_batch: int | None = None
    total_rows: int = 0
    done_rows: int = 0
    failed_rows: int = 0
    note: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event)
    # Optional per-row tuning from the rows table: row -> (word_count, sections)
    row_overrides: dict = field(default_factory=dict)
    # Optional explicit row allow-list (the table's Run? ticks). None = every unfinished row.
    row_filter: set | None = None

    @property
    def label(self) -> str:
        batches = ",".join(str(b) for b in self.batch_numbers)
        return f"Job {self.job_id} · {self.excel_path.name} · batch {batches} · {self.model.split('/')[-1]}"


class JobRunner(threading.Thread):
    """Runs one job's batch queue to completion, emitting events as it goes."""

    def __init__(self, job: Job, config: dict, registry: WorkbookRegistry,
                 emit, request_semaphore: threading.Semaphore | None = None,
                 running_jobs=None):
        super().__init__(daemon=True, name=f"job-{job.job_id}")
        self.job = job
        self.registry = registry
        self.emit = emit
        self.request_semaphore = request_semaphore
        self.running_jobs = running_jobs or (lambda: 1)

        # Per-job model override — the whole point of parallel jobs is spreading load
        # across models, so each job gets its own client and its own fallback ordering.
        self.config = copy.deepcopy(config)
        self.config["ai_provider"]["model"] = job.model
        chain = [job.model] + [m for m in (config.get("free_models") or []) if m != job.model]
        self.config["free_models"] = chain

    # ------------------------------------------------------------------ helpers

    def _log(self, message: str):
        self.emit(("log", self.job.job_id, message))

    def _set_status(self, status: str, note: str = ""):
        self.job.status = status
        if note:
            self.job.note = note
        self.emit(("job_status", self.job.job_id, status, note))

    # --------------------------------------------------------------------- main

    def _tune_for_contention(self):
        """Share the global request budget fairly and give articles room to finish.

        Every job used to ask for `max_concurrent_requests` slots regardless of how many
        jobs were running: 5 jobs × 2 = 10 demands against a 6-slot budget. Requests then
        queued on the semaphore, and that queue time counted against the article's
        wall-clock deadline — so articles "timed out" while the provider was healthy.
        That is why one batch succeeded and five failed.
        """
        provider = self.config["ai_provider"]
        parallel = max(1, self.running_jobs())
        budget = max(1, provider.get("global_max_concurrent_requests", 6))

        fair_share = max(1, budget // parallel)
        provider["max_concurrent_requests"] = min(
            provider.get("max_concurrent_requests", 2), fair_share)

        # Scale the budget by contention rather than replacing it: the per-article
        # budget is derived from the article's own size (see orchestrator
        # article_timeout_for), and overwriting it here re-introduced the flat number
        # that made big 10-section articles time out on healthy models.
        provider["contention_factor"] = parallel
        if provider.get("article_timeout_seconds"):
            provider["article_timeout_seconds"] = min(
                int(provider["article_timeout_seconds"]) * parallel, 2400)

        if parallel > 1:
            self._log(f"{parallel} batches running — using "
                      f"{provider['max_concurrent_requests']} section worker(s), "
                      f"article budget scaled ×{parallel}")

    def _verify_model(self):
        """Swap off a model that isn't answering before spending rows discovering it.

        A pinned model that is down (measured: gemma-4-31b:free returns 504 in ~10s,
        every time) otherwise burns the full retry budget on every row of the batch. One
        ~1.5s parallel ping up front replaces that with an immediate, visible switch.
        """
        provider = self.config["ai_provider"]
        chain = [self.job.model] + [m for m in (self.config.get("free_models") or [])
                                    if m != self.job.model]
        try:
            alive = ping_models(chain, self.config)
        except Exception:  # noqa: BLE001 - a failed probe must not stop the run
            return

        if not alive:
            self._log("no configured model answered the health check — trying anyway")
            return

        if self.job.model not in alive:
            replacement = alive[0]
            self._log(f"{self.job.model.split('/')[-1]} is not responding — "
                      f"switching this batch to {replacement.split('/')[-1]}")
            self.job.model = replacement
            provider["model"] = replacement

        # Keep only live models in the fallback chain, preferred order preserved.
        self.config["free_models"] = [m for m in chain if m in alive]

    def run(self):
        job = self.job
        self._tune_for_contention()
        self._verify_model()
        try:
            client = AIClient(self.config, cancel_event=job.stop_event,
                              request_semaphore=self.request_semaphore)
        except AIClientError as exc:
            self._set_status("Failed", str(exc))
            self.emit(("job_done", job.job_id))
            return

        self._set_status("Running")
        max_consecutive = max(1, self.config.get("max_consecutive_failures", 3))

        try:
            all_rows = self.registry.read_rows(job.excel_path)
        except Exception as exc:  # noqa: BLE001 - a bad file must not kill the thread
            self._set_status("Failed", f"Cannot read sheet: {exc}")
            self.emit(("job_done", job.job_id))
            return

        batches = partition_batches(all_rows, job.batch_size)

        # Count the work up front so progress means something.
        statuses = self.registry.statuses(job.excel_path)
        planned = []
        for batch_number in job.batch_numbers:
            if not (1 <= batch_number <= len(batches)):
                self._log(f"batch {batch_number} does not exist in this sheet — skipped")
                continue
            for row in batches[batch_number - 1]:
                if str(statuses.get(row[0], "")).startswith("Success"):
                    continue
                if job.row_filter is not None and row[0] not in job.row_filter:
                    continue
                planned.append((batch_number, row))
        job.total_rows = len(planned)

        if not planned:
            self._set_status("Done", "nothing left to run")
            self.emit(("job_done", job.job_id))
            return

        consecutive_failures = 0
        for batch_number, row in planned:
            if job.stop_event.is_set():
                self._set_status("Stopped", "stopped by user")
                break

            if job.current_batch != batch_number:
                job.current_batch = batch_number
                self._log(f"starting batch {batch_number}")
                self.emit(("job_batch", job.job_id, batch_number))

            row_number, title, scope, author, _status = row
            self._log(f"row {row_number}: {title[:58]}")

            words, sections = job.row_overrides.get(row_number, (job.word_count, job.sections))
            state = run_article(row_number, title, scope, author, words, sections,
                                self.config, client, job.output_dir)

            if state.status == "Stopped":
                self._set_status("Stopped", "stopped by user")
                break

            self.registry.write_status(job.excel_path, row_number, state.status, state.notes)

            if state.status.startswith("Success"):
                job.done_rows += 1
                consecutive_failures = 0
                served = getattr(client, "last_model", None)
                total = sum(seconds for _, seconds in state.stage_timings)
                self._log(f"row {row_number}: {state.status} in {_mmss(total)}"
                          + (f" [via {served}]" if served else ""))
                if state.stage_timings:
                    self._log("    " + " · ".join(
                        f"{stage} {_mmss(seconds)}" for stage, seconds in state.stage_timings))
            else:
                job.failed_rows += 1
                consecutive_failures += 1
                self._log(f"row {row_number}: {state.status} — {state.notes}")

            self.emit(("row_done", job.job_id, row_number, state.status))

            if consecutive_failures >= max_consecutive:
                self._set_status(
                    "Failed",
                    f"halted after {consecutive_failures} consecutive failures — "
                    f"switch this job's model and start it again",
                )
                break
        else:
            self._set_status("Done", f"{job.done_rows} done, {job.failed_rows} failed")

        self.emit(("job_done", job.job_id))


class JobManager:
    """Owns the registry, the shared request budget and the running jobs."""

    def __init__(self, config: dict, emit):
        self.config = config
        self.emit = emit
        self.registry = WorkbookRegistry()
        self.jobs: dict[int, Job] = {}
        self.runners: dict[int, JobRunner] = {}
        self._next_id = 1
        budget = max(1, config.get("ai_provider", {}).get("global_max_concurrent_requests", 6))
        self.request_semaphore = threading.Semaphore(budget)

    # ------------------------------------------------------------------- jobs

    def add_job(self, excel_path, output_dir, model, batch_numbers, batch_size,
                word_count, sections, row_overrides=None, row_filter=None) -> Job:
        if len(self.jobs) >= MAX_PARALLEL_JOBS:
            raise ValueError(f"At most {MAX_PARALLEL_JOBS} parallel jobs are supported.")
        job = Job(
            job_id=self._next_id,
            excel_path=Path(excel_path),
            output_dir=Path(output_dir),
            model=model,
            batch_numbers=list(batch_numbers),
            batch_size=batch_size,
            word_count=word_count,
            sections=sections,
            row_overrides=dict(row_overrides or {}),
            row_filter=set(row_filter) if row_filter is not None else None,
        )
        self.jobs[job.job_id] = job
        self._next_id += 1
        return job

    def remove_job(self, job_id: int):
        if self.is_running(job_id):
            raise ValueError("Stop the job before removing it.")
        self.jobs.pop(job_id, None)
        self.runners.pop(job_id, None)

    def start_job(self, job_id: int, config: dict | None = None):
        job = self.jobs[job_id]
        if self.is_running(job_id):
            return
        job.stop_event = threading.Event()
        job.done_rows = job.failed_rows = 0
        job.current_batch = None
        runner = JobRunner(job, config or self.config, self.registry,
                           self.emit, self.request_semaphore,
                           running_jobs=self._running_count)
        self.runners[job_id] = runner
        runner.start()

    def start_all(self, config: dict | None = None):
        for job_id, job in self.jobs.items():
            if job.status in ("Queued", "Stopped", "Failed") and not self.is_running(job_id):
                self.start_job(job_id, config)

    def stop_job(self, job_id: int):
        job = self.jobs.get(job_id)
        if job:
            job.stop_event.set()

    def stop_all(self):
        for job in self.jobs.values():
            job.stop_event.set()

    def _running_count(self) -> int:
        """Jobs currently executing, including ones just handed to a thread."""
        live = sum(1 for r in self.runners.values() if r.is_alive())
        starting = sum(1 for j in self.jobs.values()
                       if j.status in ("Queued", "Running") and not j.stop_event.is_set())
        return max(live, min(starting, len(self.jobs)))

    def is_running(self, job_id: int) -> bool:
        runner = self.runners.get(job_id)
        return bool(runner and runner.is_alive())

    def any_running(self) -> bool:
        return any(r.is_alive() for r in self.runners.values())
