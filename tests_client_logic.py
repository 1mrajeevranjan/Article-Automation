"""Unit tests for AIClient fallback / blacklist / cancellation logic. No network.

Run: .venv/bin/python tests_client_logic.py
"""

import os
import threading
import types

os.environ.setdefault("AI_API_KEY", "test-key")

import ai_client
import model_health
from ai_client import AIClient, AIClientError, CancelledError


BASE_CONFIG = {
    "ai_provider": {
        "base_url": "https://example.invalid/v1",
        "api_key_env": "AI_API_KEY",
        "model": "primary/model:free",
        "temperature": 0.7,
        "max_retries": 1,
        "request_timeout_seconds": 5,
        "provider_routing": {"ignore": ["nvidia"]},
    },
    "free_models": ["primary/model:free", "second/model:free", "nvidia/third:free"],
}


class FakeResponse:
    def __init__(self, text=None, error=None):
        if text is None:
            self.choices = []
            self.error = error
        else:
            msg = types.SimpleNamespace(content=text)
            self.choices = [types.SimpleNamespace(message=msg)]
            self.error = None


def make_client(handler, cancel_event=None):
    """Builds an AIClient whose underlying SDK call is replaced by `handler(model)`."""
    model_health.REGISTRY.reset()
    client = AIClient(BASE_CONFIG, cancel_event=cancel_event)
    calls = []

    def create(**kwargs):
        model = kwargs["model"]
        calls.append((model, kwargs.get("extra_body")))
        return handler(model)

    client._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )
    client._sleep = lambda s: None  # no real backoff in tests
    return client, calls


def test_falls_back_to_second_model():
    def handler(model):
        if model == "primary/model:free":
            raise ai_client.APIError("boom", request=None, body=None)
        return FakeResponse("second model output")

    client, calls = make_client(handler)
    out = client.chat_completion("sys", "user")
    assert out == "second model output", out
    assert client.last_model == "second/model:free", client.last_model
    print("fallback to next model on failure: OK")


def test_daily_cap_blacklists_model():
    """A per-day cap must not be retried — not within a call, not on later calls."""
    hits = {"primary": 0}

    def handler(model):
        if model == "primary/model:free":
            hits["primary"] += 1
            raise ai_client.APIStatusError(
                "rate limited",
                response=types.SimpleNamespace(
                    text='{"error":{"message":"Rate limit exceeded: free-models-per-day"}}',
                    headers={},
                    status_code=429,
                    request=None,
                ),
                body=None,
            )
        return FakeResponse("ok from second")

    client, _ = make_client(handler)
    assert client.chat_completion("s", "u") == "ok from second"
    first_hits = hits["primary"]
    assert first_hits == 1, f"daily cap should not be retried, got {first_hits} attempts"

    client.chat_completion("s", "u")
    assert hits["primary"] == 1, "capped model must be skipped entirely on later calls"
    print("daily-cap blacklist (no retry, skipped later): OK")


def test_transient_error_is_retried():
    attempts = {"n": 0}

    def handler(model):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse(error={"message": "The operation was aborted", "code": 504})
        return FakeResponse("recovered")

    client, _ = make_client(handler)
    assert client.chat_completion("s", "u") == "recovered"
    assert attempts["n"] == 2, attempts
    print("transient 504 retried then recovered: OK")


def test_502_is_not_mistaken_for_daily_cap():
    """Regression: a transient 502 whose body mentions a rate limit must NOT blacklist
    a healthy model — that knocked gemma out mid-article and stalled the whole run."""
    attempts = {"n": 0}

    def handler(model):
        attempts["n"] += 1
        if model == "primary/model:free" and attempts["n"] == 1:
            raise ai_client.APIStatusError(
                "bad gateway",
                response=types.SimpleNamespace(
                    # body deliberately contains quota-ish wording, as OpenRouter's does
                    text='{"error":{"message":"Provider returned error: limit per-day upstream","code":502}}',
                    headers={}, status_code=502, request=None,
                ),
                body=None,
            )
        return FakeResponse("recovered on retry")

    client, _ = make_client(handler)
    out = client.chat_completion("s", "u")

    assert out == "recovered on retry", out
    assert "primary/model:free" not in client._exhausted, \
        "502 must never blacklist a model as per-day-capped"
    assert client.last_model == "primary/model:free", client.last_model
    print("502 not misread as daily cap (model stays usable): OK")


def test_embedded_429_still_blacklists():
    """The genuine article — an embedded 429 quota error — must still blacklist."""
    def handler(model):
        if model == "primary/model:free":
            return FakeResponse(error={"message": "Rate limit exceeded: free-models-per-day",
                                       "code": 429})
        return FakeResponse("from fallback")

    client, _ = make_client(handler)
    assert client.chat_completion("s", "u") == "from fallback"
    assert "primary/model:free" in client._exhausted
    print("embedded 429 quota still blacklists: OK")


def test_provider_routing_skips_own_vendor():
    client, calls = make_client(lambda m: FakeResponse("x"))

    body_google = client._extra_body("google/gemma:free")
    body_nvidia = client._extra_body("nvidia/nemotron:free")

    assert body_google == {"provider": {"ignore": ["nvidia"]}}, body_google
    assert body_nvidia == {}, f"nvidia model must not ignore its own vendor: {body_nvidia}"
    print("provider routing drops self-vendor ignore: OK")


def test_cancellation_stops_immediately():
    event = threading.Event()
    event.set()

    client, calls = make_client(lambda m: FakeResponse("should not happen"), cancel_event=event)
    try:
        client.chat_completion("s", "u")
    except CancelledError:
        assert not calls, "no API call should be made once cancelled"
        print("cancellation short-circuits before calling API: OK")
        return
    raise AssertionError("expected CancelledError")


def test_all_models_exhausted_message():
    def handler(model):
        raise ai_client.APIStatusError(
            "rate limited",
            response=types.SimpleNamespace(
                text='{"error":{"message":"Rate limit exceeded: free-models-per-day"}}',
                headers={}, status_code=429, request=None,
            ),
            body=None,
        )

    client, _ = make_client(handler)
    try:
        client.chat_completion("s", "u")
    except AIClientError as exc:
        assert "per-day" in str(exc).lower() or "quota" in str(exc).lower(), str(exc)
        print("all-exhausted yields actionable message: OK")
        return
    raise AssertionError("expected AIClientError")


if __name__ == "__main__":
    test_falls_back_to_second_model()
    test_daily_cap_blacklists_model()
    test_transient_error_is_retried()
    test_502_is_not_mistaken_for_daily_cap()
    test_embedded_429_still_blacklists()
    test_provider_routing_skips_own_vendor()
    test_cancellation_stops_immediately()
    test_all_models_exhausted_message()
    print("\nAll client-logic tests passed.")
