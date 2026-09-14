"""One place that knows how every model is behaving, and how to say so in colour.

Why this exists: the app used to learn a model was useless one call at a time, and
forget it again on the next call. Three things went wrong as a result, all visible in a
single run:

  * A model that stalls (accepts the connection, never answers) burned
    request_timeout x max_retries on *every* one of an article's ~13 calls. With a
    19-model fallback chain that is 6,840s of waiting against a 675s article budget —
    which is exactly why articles "failed" after 22 minutes with a vague message.
  * A model that hit its daily quota was blacklisted, but a model returning 504 every
    time was not, so it was retried forever.
  * nvidia/nemotron-3.5-content-safety answers a 1,000-word section request with 3
    words. It is a classifier, not a writer. It passed the health check, poisoned the
    Auto pool, and its output then drove the editor's correction loop in circles.

So health is tracked per model across the whole run: a model that fails is benched
rather than re-tried, and a model that cannot write is dropped from the pool entirely.

The three states map to the macOS traffic lights, which is what the user reads at a
glance:
    READY   green   answering now
    LIMITED yellow  quota used up or temporarily down — will come back
    BLOCKED red     not offered, restricted, or unreachable — will not come back today
"""

import threading
import time

# macOS traffic-light shades.
GREEN = "#28C840"
YELLOW = "#FEBC2E"
RED = "#FF5F57"

READY, LIMITED, BLOCKED, UNKNOWN = "ready", "limited", "blocked", "unknown"

STATE_COLOUR = {READY: GREEN, LIMITED: YELLOW, BLOCKED: RED, UNKNOWN: "#8E8E93"}
STATE_LABEL = {
    READY: "Ready",
    LIMITED: "Limited",
    BLOCKED: "Unavailable",
    UNKNOWN: "Not checked",
}

# Reasons produced by ai_client._classify_reason, mapped to a state. Quota and outages
# are yellow because they recover; "not offered" and "restricted" are red because no
# amount of retrying today will change them.
_REASON_STATE = {
    "daily free quota used up": LIMITED,
    "provider down": LIMITED,
    "not available via plain API": BLOCKED,
    "model not offered": BLOCKED,
}

# A model that answers but cannot produce prose is red, not yellow: that is what the
# model is, and waiting for a quota reset will not change it.
_BLOCKED_PREFIXES = ("answers, but cannot write",)

# A model is benched after this many consecutive hard failures within a run.
STRIKES_BEFORE_BENCH = 2
# How long a benched model sits out before it is worth another attempt.
BENCH_SECONDS = 900
# A reply this far below the requested length means the model cannot do the job.
MIN_LENGTH_RATIO = 0.35
# How long a "can this model write?" answer stays good. Whether a model is a writer or
# a classifier does not change hour to hour, and re-asking costs a full generation per
# model — 179s across the catalogue, at every batch start, for an unchanging answer.
PROSE_CHECK_TTL = 6 * 3600


def state_for_reason(reason: str) -> str:
    if not reason:
        return READY
    if reason.startswith(_BLOCKED_PREFIXES):
        return BLOCKED
    return _REASON_STATE.get(reason, LIMITED)


class ModelHealth:
    """Thread-safe health record shared by every job in the process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}

    # ---------------------------------------------------------------- recording

    def _record(self, model: str) -> dict:
        return self._records.setdefault(model, {
            "state": UNKNOWN,
            "reason": "",
            "strikes": 0,
            "benched_until": 0.0,
            "latency": None,
            "can_write": None,     # None = untested, False = replies far too short
            "checked_at": 0.0,
            "prose_checked_at": 0.0,
        })

    def observe_probe(self, model: str, alive: bool, reason: str, prose: bool = False):
        """Result of a health check. Never un-benches a model failing real calls."""
        with self._lock:
            record = self._record(model)
            record["checked_at"] = time.time()
            record["reason"] = reason
            if prose:
                record["prose_checked_at"] = time.time()
                # Only an actual short answer proves a model cannot write. A timeout or
                # a quota error during the prose stage says nothing about capability, so
                # leave it untested rather than condemning it.
                if alive:
                    record["can_write"] = True
                elif reason.startswith(_BLOCKED_PREFIXES):
                    record["can_write"] = False
                else:
                    record["prose_checked_at"] = 0.0
            if alive:
                if record["can_write"] is False:
                    record["state"] = BLOCKED
                    record["reason"] = "answers, but cannot write full sections"
                elif record["benched_until"] > time.time():
                    record["state"] = LIMITED
                    record["reason"] = reason or "benched after repeated failures"
                else:
                    record["state"] = READY
                    record["strikes"] = 0
            else:
                record["state"] = state_for_reason(reason)

    def observe_success(self, model: str, seconds: float):
        with self._lock:
            record = self._record(model)
            record.update(state=READY, reason="", strikes=0, benched_until=0.0,
                          latency=seconds, checked_at=time.time())

    def observe_failure(self, model: str, reason: str, permanent: bool = False):
        """A real call failed. Enough of these and the model sits out."""
        with self._lock:
            record = self._record(model)
            record["strikes"] += 1
            record["reason"] = reason
            record["checked_at"] = time.time()
            if permanent:
                record["state"] = BLOCKED
                record["benched_until"] = time.time() + BENCH_SECONDS
            elif record["strikes"] >= STRIKES_BEFORE_BENCH:
                record["state"] = LIMITED
                record["benched_until"] = time.time() + BENCH_SECONDS
            else:
                record["state"] = LIMITED

    def observe_short_reply(self, model: str, words: int, wanted: int):
        """A model answering 1,000-word requests with 3 words is not a writer.

        Marked permanently rather than benched: this is what the model *is*, not a
        transient condition, and no later attempt will produce a different answer.
        """
        with self._lock:
            record = self._record(model)
            record.update(
                can_write=False, state=BLOCKED,
                reason=f"answers, but cannot write full sections ({words} words for {wanted})",
                benched_until=time.time() + BENCH_SECONDS, checked_at=time.time(),
            )

    def needs_prose_check(self, model: str) -> bool:
        """True unless we recently established whether this model can write."""
        with self._lock:
            record = self._records.get(model)
            if not record or record["can_write"] is None:
                return True
            return (time.time() - record["prose_checked_at"]) > PROSE_CHECK_TTL

    def known_non_writer(self, model: str) -> bool:
        with self._lock:
            record = self._records.get(model)
            return bool(record and record["can_write"] is False)

    def reset(self):
        """Forget everything. For tests, and for a user asking to re-check from scratch."""
        with self._lock:
            self._records.clear()

    # ------------------------------------------------------------------ querying

    def is_benched(self, model: str) -> bool:
        with self._lock:
            record = self._records.get(model)
            return bool(record and record["benched_until"] > time.time())

    def state(self, model: str) -> str:
        with self._lock:
            record = self._records.get(model)
            if not record:
                return UNKNOWN
            if record["benched_until"] > time.time() and record["state"] == READY:
                return LIMITED
            return record["state"]

    def reason(self, model: str) -> str:
        with self._lock:
            record = self._records.get(model)
            return record["reason"] if record else ""

    def latency(self, model: str) -> float | None:
        with self._lock:
            record = self._records.get(model)
            return record["latency"] if record else None

    def usable(self, models) -> list:
        """Models worth trying right now, best first: ready before limited, and among
        ready ones the fastest measured first."""
        def rank(model):
            state = self.state(model)
            order = {READY: 0, UNKNOWN: 1, LIMITED: 2, BLOCKED: 3}[state]
            return (order, self.latency(model) or 999)

        return [m for m in sorted(models, key=rank)
                if self.state(m) != BLOCKED and not self.is_benched(m)]

    def snapshot(self, models) -> list[dict]:
        """Everything the UI needs to draw the model list, in display order."""
        rows = []
        for model in models:
            state = self.state(model)
            rows.append({
                "model": model,
                "short": model.split("/")[-1].removesuffix(":free"),
                "state": state,
                "colour": STATE_COLOUR[state],
                "label": STATE_LABEL[state],
                "reason": self.reason(model),
                "latency": self.latency(model),
            })
        order = {READY: 0, UNKNOWN: 1, LIMITED: 2, BLOCKED: 3}
        rows.sort(key=lambda r: (order[r["state"]], r["latency"] or 999, r["short"]))
        return rows


# Process-wide registry: every job and the UI read the same health.
REGISTRY = ModelHealth()
