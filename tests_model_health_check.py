"""Why a run used to burn 22 minutes to report a failure, and why it no longer can.

Everything here is offline: the SDK call is replaced by a stub that stalls, fails or
answers too briefly on demand, so the bounds being asserted are the app's own, not the
network's.

    .venv/bin/python tests_model_health_check.py
"""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_client
import model_health as health
from ai_client import AIClient, AIClientError

PASS, FAIL = "PASS", "FAIL"
results = []

BASE_CONFIG = {
    "ai_provider": {
        "base_url": "https://example.invalid/v1",
        "api_key_env": "TEST_KEY_FOR_HEALTH",
        "model": "primary/model:free",
        "request_timeout_seconds": 120,
        "max_retries": 2,
        "temperature": 0.7,
    },
    "free_models": [f"vendor/model-{i}:free" for i in range(1, 19)],
}


def check(name, condition, detail=""):
    results.append((PASS if condition else FAIL, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def make_client(handler, config=None):
    import os
    os.environ["TEST_KEY_FOR_HEALTH"] = "x"
    health.REGISTRY.reset()
    client = AIClient(config or BASE_CONFIG)
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return handler(kwargs)

    client._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    client._sleep = lambda s: None
    return client, seen


def reply(text: str):
    message = types.SimpleNamespace(content=text)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], error=None)


def timeout_always(_kwargs):
    raise ai_client.APITimeoutError(request=None)


# ----------------------------------------------------------- the chain is bounded

def test_chain_is_capped():
    """19 models x 3 attempts x 120s = 6,840s for ONE call. That was the stall."""
    client, seen = make_client(timeout_always)
    try:
        client.chat_completion("s", "u")
    except AIClientError:
        pass
    tried = {k["model"] for k in seen}
    cap = BASE_CONFIG["ai_provider"].get("max_models_per_call", 4)
    check("one call tries a handful of models, not the whole catalogue",
          len(tried) <= cap, f"{len(tried)} models tried, cap {cap}")
    check("the catalogue really is bigger than the cap",
          len(BASE_CONFIG["free_models"]) > cap, f"{len(BASE_CONFIG['free_models'])} known")


# ------------------------------------------------------------ the deadline binds

def test_never_asks_for_longer_than_the_article_has_left():
    client, seen = make_client(timeout_always)
    client.set_deadline(time.time() + 40)
    try:
        client.chat_completion("s", "u")
    except AIClientError:
        pass
    asked = [k.get("timeout") for k in seen if k.get("timeout")]
    check("per-request timeout is clamped to the remaining budget",
          asked and max(asked) <= 40, f"max asked {max(asked) if asked else None}s, budget 40s")
    check("config timeout would have been longer",
          BASE_CONFIG["ai_provider"]["request_timeout_seconds"] > 40)


def test_gives_up_when_the_budget_is_spent():
    """The budget must be spent on real attempts, then abandoned — not skipped outright.

    A budget below MIN_USEFUL_SECONDS bails before calling anything, which would make
    this pass for the wrong reason, so use one comfortably above it.
    """
    def slow(kwargs):
        time.sleep(min(kwargs.get("timeout") or 1, 2.0))
        raise ai_client.APITimeoutError(request=None)

    budget = ai_client.MIN_USEFUL_SECONDS + 10
    client, seen = make_client(slow)
    client.set_deadline(time.time() + budget)
    started = time.time()
    try:
        client.chat_completion("s", "u")
        check("a totally stalled provider still fails", False, "unexpected success")
    except AIClientError as exc:
        elapsed = time.time() - started
        check("real attempts were made before giving up", len(seen) >= 2, f"{len(seen)} calls")
        check("failure lands inside the article's budget, not long after",
              elapsed <= budget, f"{elapsed:.1f}s for a {budget}s budget")
        check("without the deadline this would have run far longer",
              budget < BASE_CONFIG["ai_provider"]["request_timeout_seconds"])


def test_no_deadline_means_no_clamp():
    client, seen = make_client(timeout_always)
    try:
        client.chat_completion("s", "u")
    except AIClientError:
        pass
    asked = [k.get("timeout") for k in seen if k.get("timeout")]
    check("without an article budget the configured timeout is used",
          asked and max(asked) == 120, f"max asked {max(asked) if asked else None}")


# --------------------------------------------------------- the circuit breaker

def test_repeated_failure_benches_a_model():
    health.REGISTRY.reset()
    model = "vendor/flaky:free"
    for _ in range(health.STRIKES_BEFORE_BENCH):
        health.REGISTRY.observe_failure(model, "provider down")
    check("a model failing repeatedly is benched", health.REGISTRY.is_benched(model))
    check("a benched model is yellow, not green",
          health.REGISTRY.state(model) == health.LIMITED, health.REGISTRY.state(model))
    check("a benched model is excluded from the next chain",
          model not in health.REGISTRY.usable([model, "vendor/other:free"]))


def test_success_clears_the_strikes():
    health.REGISTRY.reset()
    model = "vendor/recovering:free"
    health.REGISTRY.observe_failure(model, "provider down")
    health.REGISTRY.observe_success(model, 12.0)
    check("one success returns a model to green",
          health.REGISTRY.state(model) == health.READY)
    check("and un-benches it", not health.REGISTRY.is_benched(model))
    check("measured latency is remembered", health.REGISTRY.latency(model) == 12.0)


def test_403_and_404_are_permanent():
    health.REGISTRY.reset()
    for model, reason in (("vendor/restricted:free", "not available via plain API"),
                          ("vendor/gone:free", "model not offered")):
        health.REGISTRY.observe_failure(model, reason, permanent=True)
        check(f"{reason} is red immediately, no second strike",
              health.REGISTRY.state(model) == health.BLOCKED)


def test_ready_models_are_tried_first():
    health.REGISTRY.reset()
    health.REGISTRY.observe_success("vendor/fast:free", 5.0)
    health.REGISTRY.observe_success("vendor/slow:free", 90.0)
    health.REGISTRY.observe_failure("vendor/bad:free", "provider down")
    health.REGISTRY.observe_failure("vendor/bad:free", "provider down")
    order = health.REGISTRY.usable(["vendor/slow:free", "vendor/bad:free", "vendor/fast:free"])
    check("the fastest ready model leads and the benched one is gone",
          order == ["vendor/fast:free", "vendor/slow:free"], str(order))


# ------------------------------------------------ a classifier is not a writer

def test_short_reply_rejects_the_model():
    """nvidia/nemotron-3.5-content-safety answers a 1,000-word request with 3 words."""
    calls = {"n": 0}

    def handler(kwargs):
        calls["n"] += 1
        if kwargs["model"] == "primary/model:free":
            return reply("Safe content.")           # 2 words for a 1,000-word request
        return reply("word " * 900)

    client, seen = make_client(handler)
    text = client.chat_completion("s", "u", min_words=1000)
    check("a 2-word answer is rejected and the next model serves",
          len(text.split()) > 500, f"{len(text.split())} words returned")
    check("the classifier is marked red, not merely benched",
          health.REGISTRY.state("primary/model:free") == health.BLOCKED,
          health.REGISTRY.state("primary/model:free"))
    check("its reason says what is actually wrong",
          "cannot write" in health.REGISTRY.reason("primary/model:free"),
          health.REGISTRY.reason("primary/model:free"))
    check("and it is dropped from later chains",
          "primary/model:free" not in health.REGISTRY.usable(["primary/model:free"]))


def test_a_full_length_reply_is_accepted():
    client, _ = make_client(lambda k: reply("word " * 800))
    text = client.chat_completion("s", "u", min_words=1000)
    check("800 words for a 1,000-word request is fine — the floor is a floor, not equality",
          len(text.split()) == 800)


def test_no_min_words_means_no_length_check():
    client, _ = make_client(lambda k: reply("tiny"))
    check("a call with no length requirement accepts a short reply",
          client.chat_completion("s", "u") == "tiny")


# ------------------------------------------------------ states, colours, labels

def test_traffic_light_colours():
    check("ready is the macOS green", health.STATE_COLOUR[health.READY] == "#28C840")
    check("limited is the macOS yellow", health.STATE_COLOUR[health.LIMITED] == "#FEBC2E")
    check("blocked is the macOS red", health.STATE_COLOUR[health.BLOCKED] == "#FF5F57")


def test_reason_to_state_mapping():
    cases = [
        ("", health.READY),
        ("daily free quota used up", health.LIMITED),
        ("provider down", health.LIMITED),
        ("not available via plain API", health.BLOCKED),
        ("model not offered", health.BLOCKED),
        ("answers, but cannot write prose (3 words)", health.BLOCKED),
    ]
    for reason, expected in cases:
        got = health.state_for_reason(reason)
        check(f"'{reason or 'ok'}' -> {expected}", got == expected, got)


def test_quota_is_yellow_not_red():
    """The distinction the user asked for: a quota comes back, a restriction does not."""
    check("quota exhausted is yellow (recoverable)",
          health.state_for_reason("daily free quota used up") == health.LIMITED)
    check("permanently unavailable is red",
          health.state_for_reason("not available via plain API") == health.BLOCKED)


def test_snapshot_orders_ready_first():
    health.REGISTRY.reset()
    health.REGISTRY.observe_probe("v/red:free", False, "model not offered")
    health.REGISTRY.observe_probe("v/yellow:free", False, "daily free quota used up")
    health.REGISTRY.observe_success("v/green:free", 8.0)
    rows = health.REGISTRY.snapshot(["v/red:free", "v/yellow:free", "v/green:free"])
    check("snapshot puts what works at the top",
          [r["short"] for r in rows] == ["green", "yellow", "red"],
          str([r["short"] for r in rows]))
    check("each row carries its colour",
          [r["colour"] for r in rows] == [health.GREEN, health.YELLOW, health.RED])
    check("each row carries a human reason",
          rows[1]["reason"] == "daily free quota used up", rows[1]["reason"])


def test_probe_classification_of_a_stalling_upstream():
    check("an upstream error reads as a temporary outage, not a mystery string",
          ai_client._classify_reason(None, "Upstream error from Nvidia: Resource exhausted")
          == "provider down")
    check("a 500 is a provider outage",
          ai_client._classify_reason(500, "internal server error") == "provider down")


def test_message_when_nothing_can_write():
    health.REGISTRY.reset()
    for model in BASE_CONFIG["free_models"]:
        health.REGISTRY.observe_failure(model, "daily free quota used up", permanent=True)
    health.REGISTRY.observe_failure("primary/model:free", "daily free quota used up",
                                    permanent=True)
    client, seen = make_client(lambda k: reply("x"))
    health.REGISTRY.reset()
    for model in ["primary/model:free"] + BASE_CONFIG["free_models"]:
        health.REGISTRY.observe_failure(model, "daily free quota used up", permanent=True)
    try:
        client.chat_completion("s", "u")
        check("an all-blocked catalogue fails rather than pretending", False, "unexpected success")
    except AIClientError as exc:
        check("no API call is attempted when nothing can serve", not seen, f"{len(seen)} calls")
        check("the message says what to do about it",
              "quota" in str(exc).lower() and "openrouter.ai" in str(exc), str(exc)[:110])


if __name__ == "__main__":
    test_chain_is_capped()
    test_never_asks_for_longer_than_the_article_has_left()
    test_gives_up_when_the_budget_is_spent()
    test_no_deadline_means_no_clamp()
    test_repeated_failure_benches_a_model()
    test_success_clears_the_strikes()
    test_403_and_404_are_permanent()
    test_ready_models_are_tried_first()
    test_short_reply_rejects_the_model()
    test_a_full_length_reply_is_accepted()
    test_no_min_words_means_no_length_check()
    test_traffic_light_colours()
    test_reason_to_state_mapping()
    test_quota_is_yellow_not_red()
    test_snapshot_orders_ready_first()
    test_probe_classification_of_a_stalling_upstream()
    test_message_when_nothing_can_write()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
