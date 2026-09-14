"""The daily allowance, and the probe bug that was spending it on nothing.

Two things are checked here, both of which caused "the models stopped responding":

  1. OpenRouter's free tier allows 50 requests per DAY across every free model
     combined. One 10-section article costs ~13, so a free key is worth about three
     articles. A 25-row batch needs ~325 and fails at row 2. The app must count, say so
     before the run, and refuse a run it cannot finish.
  2. The capability probe capped max_tokens at 3x the requested word count. Reasoning
     models spend that entire budget on hidden reasoning and emit empty content with
     finish_reason "length" — so seven working models were condemned as "cannot write
     prose", shrinking a 10-model pool to 3.

    .venv/bin/python tests_quota_check.py
"""

import json
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_client
import model_health as health
import usage_tracker
from usage_tracker import UsageTracker, affordability, requests_for_article

PASS, FAIL = "PASS", "FAIL"
results = []

# ai_client counts through the module-level TRACKER. Point it at a scratch file for the
# whole run so the user's real .usage.json is never touched by a test.
_ISOLATED_TRACKER = UsageTracker(Path(tempfile.mkdtemp()) / "usage.json")
ai_client.TRACKER = _ISOLATED_TRACKER


def check(name, condition, detail=""):
    results.append((PASS if condition else FAIL, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def fresh_tracker() -> UsageTracker:
    return UsageTracker(Path(tempfile.mkdtemp()) / "usage.json")


# ------------------------------------------------------------------- the arithmetic

def test_article_cost():
    check("a 10-section article costs ~13 requests",
          requests_for_article(10) == 14, str(requests_for_article(10)))
    check("a 5-section article costs less",
          requests_for_article(5) < requests_for_article(10))
    check("cost never drops below one request", requests_for_article(1) >= 1)


def test_free_tier_is_about_three_articles():
    tracker = fresh_tracker()
    check("the default assumption matches OpenRouter's documented free tier",
          tracker.limit() == 50, str(tracker.limit()))
    check("which is about three 10-section articles",
          tracker.articles_left(10) == 3, str(tracker.articles_left(10)))


def test_affordability_of_a_real_batch():
    tracker = fresh_tracker()
    needed, available, affordable = affordability(rows=25, sections=10, tracker=tracker)
    check("a 25-row batch needs far more than a day's allowance",
          needed > available, f"needs {needed}, has {available}")
    check("and only a few rows actually fit", affordable == 3, str(affordable))


# --------------------------------------------------------------------- the counter

def test_counting_and_remaining():
    tracker = fresh_tracker()
    start = tracker.remaining()
    tracker.record_request()
    tracker.record_request(5)
    check("every request is counted", tracker.used() == 6, str(tracker.used()))
    check("remaining falls by the same amount",
          tracker.remaining() == start - 6, str(tracker.remaining()))


def test_wire_numbers_override_the_local_estimate():
    """The 429 body is authoritative; the local count is only an estimate until then."""
    tracker = fresh_tracker()
    tracker.record_request(3)
    tracker.record_limit_headers({
        "X-RateLimit-Limit": "50",
        "X-RateLimit-Remaining": "12",
        "X-RateLimit-Reset": "1789430400000",
    })
    check("limit is taken from the provider", tracker.limit() == 50)
    check("used is corrected to limit minus remaining",
          tracker.used() == 38, str(tracker.used()))
    check("remaining matches the wire", tracker.remaining() == 12, str(tracker.remaining()))
    check("reset time comes from the wire",
          tracker.reset_at() == datetime.fromtimestamp(1789430400, tz=timezone.utc),
          str(tracker.reset_at()))


def test_lowercase_headers_are_accepted():
    tracker = fresh_tracker()
    tracker.record_limit_headers({"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "900"})
    check("header casing does not matter",
          (tracker.limit(), tracker.remaining()) == (1000, 900),
          f"{tracker.limit()}/{tracker.remaining()}")


def test_garbage_headers_are_ignored():
    tracker = fresh_tracker()
    tracker.record_request(4)
    tracker.record_limit_headers({"X-RateLimit-Limit": "not-a-number"})
    check("a malformed header leaves the count intact", tracker.used() == 4, str(tracker.used()))
    tracker.record_limit_headers(None)
    tracker.record_limit_headers({})
    check("missing headers are a no-op, not a crash", tracker.used() == 4)


def test_count_persists_across_launches():
    path = Path(tempfile.mkdtemp()) / "usage.json"
    first = UsageTracker(path)
    first.record_request(7)
    second = UsageTracker(path)
    check("a relaunch does not forget today's usage", second.used() == 7, str(second.used()))


def test_a_new_utc_day_resets_the_count():
    path = Path(tempfile.mkdtemp()) / "usage.json"
    tracker = UsageTracker(path)
    tracker.record_request(40)
    tracker.record_limit_headers({"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "10"})
    # Rewrite the stored day as yesterday, then reload.
    state = json.loads(path.read_text())
    state["date"] = "2000-01-01"
    path.write_text(json.dumps(state))

    reopened = UsageTracker(path)
    check("yesterday's usage does not count against today", reopened.used() == 0,
          str(reopened.used()))
    check("but the learned account limit survives the day boundary",
          reopened.limit() == 50, str(reopened.limit()))


def test_summary_is_readable():
    tracker = fresh_tracker()
    tracker.record_limit_headers({"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0"})
    text = tracker.summary()
    check("summary states the remaining count", "0 of 50" in text, text)
    check("summary translates it into articles", "article" in text, text)
    check("summary says when it comes back", "resets" in text, text)


# ---------------------------------------------- the probe that starved reasoning models

def _probe_reply(content: str, finish_reason: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content},
                                    "finish_reason": finish_reason}]}).encode()


class FakeHTTP:
    """Stands in for urlopen so the probe's own request shape can be inspected."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode())
        self.requests.append(body)
        payload = self.responder(body)

        class Ctx:
            def __enter__(self_inner):
                return types.SimpleNamespace(read=lambda: payload)

            def __exit__(self_inner, *a):
                return False

        return Ctx()


def run_probe(responder, models, deep):
    import os
    os.environ["TEST_QUOTA_KEY"] = "x"
    usage_tracker.TRACKER = _ISOLATED_TRACKER
    config = {"ai_provider": {"base_url": "https://example.invalid/v1",
                              "api_key_env": "TEST_QUOTA_KEY"}}
    fake = FakeHTTP(responder)
    real_open, real_load = ai_client.urllib.request.urlopen, ai_client.json.load
    ai_client.urllib.request.urlopen = fake
    ai_client.json.load = lambda f: json.loads(f.read())
    try:
        health.REGISTRY.reset()
        return ai_client.probe_models(models, config, deep=deep), fake
    finally:
        ai_client.urllib.request.urlopen = real_open
        ai_client.json.load = real_load


def test_deep_probe_does_not_cap_tokens():
    """The regression itself: max_tokens=240 starved reasoning models to empty output."""
    _, fake = run_probe(lambda body: _probe_reply("word " * 60, "stop"),
                        ["v/a:free"], deep=True)
    prose = [r for r in fake.requests if r["messages"][0]["content"] != "ping"]
    check("the prose probe sends no max_tokens at all",
          prose and all("max_tokens" not in r for r in prose),
          str([sorted(r) for r in prose]))
    ping = [r for r in fake.requests if r["messages"][0]["content"] == "ping"]
    check("the cheap ping still caps itself at one token",
          ping and all(r.get("max_tokens") == 1 for r in ping))


def test_truncated_output_is_not_called_incapable():
    """finish_reason 'length' with empty content is a budget artefact, not a verdict."""
    results_map, _ = run_probe(lambda body: _probe_reply("", "length"), ["v/reasoner:free"],
                               deep=True)
    ok, reason = results_map["v/reasoner:free"]
    check("a truncated reply does not claim the model cannot write",
          "cannot write" not in reason, reason)
    check("and it is not marked permanently red",
          health.REGISTRY.state("v/reasoner:free") != health.BLOCKED,
          health.REGISTRY.state("v/reasoner:free"))


def test_a_genuine_short_answer_is_still_caught():
    """The content-safety classifier: 3 words, finish_reason stop. That IS a verdict."""
    results_map, _ = run_probe(lambda body: _probe_reply("Safe content here.", "stop"),
                               ["v/classifier:free"], deep=True)
    ok, reason = results_map["v/classifier:free"]
    check("a model that chose to stop after 3 words is rejected", not ok, reason)
    check("with a reason that names the problem", "cannot write prose" in reason, reason)
    check("and it is red, because that will not change",
          health.REGISTRY.state("v/classifier:free") == health.BLOCKED)


def test_prose_stage_only_runs_for_models_that_answer():
    def responder(body):
        return _probe_reply("word " * 60, "stop")

    _, fake = run_probe(responder, ["v/a:free", "v/b:free", "v/c:free"], deep=True)
    pings = [r for r in fake.requests if r["messages"][0]["content"] == "ping"]
    prose = [r for r in fake.requests if r["messages"][0]["content"] != "ping"]
    check("every model is pinged cheaply first", len(pings) == 3, str(len(pings)))
    check("and only then asked to write", len(prose) == 3, str(len(prose)))


def test_prose_result_is_cached():
    """Re-deriving 'can this model write?' costs a generation per model, per batch."""
    _, fake = run_probe(lambda body: _probe_reply("word " * 60, "stop"),
                        ["v/a:free"], deep=True)
    first_prose = len([r for r in fake.requests if r["messages"][0]["content"] != "ping"])

    import os
    os.environ["TEST_QUOTA_KEY"] = "x"
    config = {"ai_provider": {"base_url": "https://example.invalid/v1",
                              "api_key_env": "TEST_QUOTA_KEY"}}
    fake2 = FakeHTTP(lambda body: _probe_reply("word " * 60, "stop"))
    real_open, real_load = ai_client.urllib.request.urlopen, ai_client.json.load
    ai_client.urllib.request.urlopen = fake2
    ai_client.json.load = lambda f: json.loads(f.read())
    try:
        ai_client.probe_models(["v/a:free"], config, deep=True)   # registry NOT reset
    finally:
        ai_client.urllib.request.urlopen = real_open
        ai_client.json.load = real_load

    second_prose = len([r for r in fake2.requests if r["messages"][0]["content"] != "ping"])
    check("the first check asks the model to write", first_prose == 1, str(first_prose))
    check("the second re-uses the answer instead of spending another request",
          second_prose == 0, str(second_prose))


def test_probes_are_counted_against_the_allowance():
    before = _ISOLATED_TRACKER.used()
    run_probe(lambda body: _probe_reply("word " * 60, "stop"), ["v/a:free", "v/b:free"],
              deep=True)
    after = _ISOLATED_TRACKER.used()
    check("health probes spend the same allowance articles do",
          after > before, f"{before} -> {after}")


def test_ttl_is_long_enough_to_matter():
    check("a prose result is cached for hours, not seconds",
          health.PROSE_CHECK_TTL >= 3600, str(health.PROSE_CHECK_TTL))


if __name__ == "__main__":
    test_article_cost()
    test_free_tier_is_about_three_articles()
    test_affordability_of_a_real_batch()
    test_counting_and_remaining()
    test_wire_numbers_override_the_local_estimate()
    test_lowercase_headers_are_accepted()
    test_garbage_headers_are_ignored()
    test_count_persists_across_launches()
    test_a_new_utc_day_resets_the_count()
    test_summary_is_readable()
    test_deep_probe_does_not_cap_tokens()
    test_truncated_output_is_not_called_incapable()
    test_a_genuine_short_answer_is_still_caught()
    test_prose_stage_only_runs_for_models_that_answer()
    test_prose_result_is_cached()
    test_probes_are_counted_against_the_allowance()
    test_ttl_is_long_enough_to_matter()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
