"""How many provider requests today, against how many the account allows.

This exists because the single most common failure — "the models stopped responding" —
is not a model failure at all. OpenRouter's free tier allows a fixed number of requests
per day **across every free model combined**:

    X-RateLimit-Limit: 50
    limit_source: openrouter_free_tier_daily
    "Add 10 credits to unlock 1000 free model requests per day"

One 10-section article costs roughly 13 requests, so a free key is worth about three
articles a day. A 25-row batch needs ~325 and cannot possibly finish. Nothing in the
code can change that; what the code *can* do is count, say so before the run instead of
after, and stop wasting the remainder on health probes.

The count is kept locally because the provider exposes no "requests remaining" endpoint
(/api/v1/key returns limit: null on a free key). When a 429 does arrive it carries the
authoritative limit and reset, so the local count is corrected from the wire whenever
the wire says anything.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("usage_tracker")

STATE_PATH = Path(__file__).resolve().parent / ".usage.json"

# OpenRouter's documented free-tier allowance, used until the wire tells us otherwise.
DEFAULT_FREE_DAILY_LIMIT = 50
# Requests one article costs: outline, N section writes, intro/conclusion, editor,
# abstract, references. Measured at ~13 for a 10-section article.
REQUESTS_PER_ARTICLE = 13


def _today() -> str:
    """The provider resets at 00:00 UTC, so the day boundary must be UTC too."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _next_reset() -> datetime:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


class UsageTracker:
    """Thread-safe, process-wide, persisted across launches."""

    def __init__(self, path: Path = STATE_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._state = self._load()

    # ------------------------------------------------------------------ storage

    def _load(self) -> dict:
        blank = {"date": _today(), "used": 0, "limit": None, "reset_ms": None}
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a missing or corrupt file is not an error
            return blank
        if state.get("date") != _today():
            # A new UTC day: the provider's counter reset, so ours must too. The learned
            # limit survives, since that is a property of the account, not of the day.
            return {**blank, "limit": state.get("limit")}
        return {**blank, **state}

    def _save(self):
        try:
            self._path.write_text(json.dumps(self._state), encoding="utf-8")
        except OSError:
            logger.debug("Could not persist usage count", exc_info=True)

    def _roll_day(self):
        if self._state["date"] != _today():
            self._state = {"date": _today(), "used": 0,
                           "limit": self._state.get("limit"), "reset_ms": None}

    # ---------------------------------------------------------------- recording

    def record_request(self, count: int = 1):
        """Every provider call counts, whether it succeeds, fails or is a health probe."""
        with self._lock:
            self._roll_day()
            self._state["used"] += count
            self._save()

    def record_limit_headers(self, headers: dict):
        """Correct the local count from the provider's own numbers when they arrive.

        These only appear on a 429, which is exactly the moment the local estimate
        matters least and the truth matters most.
        """
        if not headers:
            return
        with self._lock:
            self._roll_day()
            limit = headers.get("X-RateLimit-Limit") or headers.get("x-ratelimit-limit")
            remaining = (headers.get("X-RateLimit-Remaining")
                         or headers.get("x-ratelimit-remaining"))
            reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
            try:
                if limit is not None:
                    self._state["limit"] = int(limit)
                if remaining is not None and self._state["limit"] is not None:
                    self._state["used"] = max(0, self._state["limit"] - int(remaining))
                if reset is not None:
                    self._state["reset_ms"] = int(reset)
            except (TypeError, ValueError):
                return
            self._save()

    # ----------------------------------------------------------------- querying

    def limit(self) -> int:
        with self._lock:
            return self._state.get("limit") or DEFAULT_FREE_DAILY_LIMIT

    def limit_known(self) -> bool:
        """True once the provider itself has stated the limit.

        Until then the 50 above is an assumption about a free key, and a paid key gets
        1000 — so refusing to start a run on the assumed number would be wrong. Warn on
        a guess; only block on a fact.
        """
        with self._lock:
            return self._state.get("limit") is not None

    def used(self) -> int:
        with self._lock:
            self._roll_day()
            return self._state["used"]

    def remaining(self) -> int:
        return max(0, self.limit() - self.used())

    def reset_at(self) -> datetime:
        with self._lock:
            ms = self._state.get("reset_ms")
        if ms:
            try:
                return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                pass
        return _next_reset()

    def articles_left(self, sections: int = 10) -> int:
        return self.remaining() // requests_for_article(sections)

    def summary(self) -> str:
        left, total = self.remaining(), self.limit()
        hours = max(0, (self.reset_at() - datetime.now(timezone.utc)).total_seconds() / 3600)
        return (f"{left} of {total} daily free requests left "
                f"(~{self.articles_left()} article(s)) · resets in {hours:.0f}h")

    def reset(self):
        """For tests."""
        with self._lock:
            self._state = {"date": _today(), "used": 0, "limit": None, "reset_ms": None}


def requests_for_article(sections: int = 10) -> int:
    """One call per middle section plus outline, intro/conclusion, editor, abstract and
    references — and one correction pass of headroom, which usually happens."""
    return max(1, sections - 2) + 6


def affordability(rows: int, sections: int, tracker: "UsageTracker") -> tuple[int, int, int]:
    """(requests needed, requests available, rows that actually fit)."""
    per_article = requests_for_article(sections)
    needed = rows * per_article
    available = tracker.remaining()
    return needed, available, available // per_article


TRACKER = UsageTracker()


def use_isolated_state():
    """Point the process-wide counter at a scratch file.

    Offline suites drive the real client with fake transports; without this they would
    count those fake calls against the user's actual daily allowance and leave the app
    under-reporting what the day has left.
    """
    global TRACKER
    import tempfile
    TRACKER = UsageTracker(Path(tempfile.mkdtemp()) / "usage.json")

    import ai_client
    import batch_runner
    ai_client.TRACKER = TRACKER
    batch_runner.TRACKER = TRACKER
    try:
        import gui_app
        gui_app.TRACKER = TRACKER
    except Exception:  # noqa: BLE001 - headless callers may not import the GUI
        pass
    return TRACKER
