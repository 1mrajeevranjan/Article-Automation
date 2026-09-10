"""OpenAI-compatible client with model fallback, error classification and cancellation.

Why this is more than a thin wrapper: on OpenRouter's free tier a single model ID is
load-balanced across several upstream hosts, any of which can be rate-limited or down
at any moment. Empirically (tests_routing_probe2.py) a bare request succeeded 8/10
while the same request with upstream routing hints succeeded 10/10. On top of that,
each :free model has a per-day account cap that, once hit, never recovers within the
run — so retrying it is pure waste.

This client therefore:
  * tries a chain of models, not just one;
  * classifies errors so a daily-capped model is blacklisted for the process instead
    of being retried 3x on every one of the ~7 calls each article makes;
  * retries only what is actually transient (502/504/timeout/transient 429);
  * aborts promptly when the user presses Stop.
"""

import json
import logging
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI, APIError, APIStatusError, APITimeoutError

logger = logging.getLogger("ai_client")


class AIClientError(RuntimeError):
    pass


class CancelledError(AIClientError):
    """Raised when the user stops the batch mid-flight."""


# Substrings identifying a hard per-day quota that will not recover during this run.
_DAILY_CAP_MARKERS = ("free-models-per-day", "per-day", "daily limit")


def probe_models(models, config: dict, timeout: int = 20) -> dict:
    """Returns {model: (is_alive, reason)} — the reason matters to the user.

    "Unavailable" is not actionable; "out of today's free quota, add credits" and
    "upstream returned 504" call for completely different responses. Measured on the
    live catalogue, most failures are the former.
    """
    provider = config["ai_provider"]
    api_key = os.environ.get(provider["api_key_env"], "")
    if not api_key or not models:
        return {m: (True, "") for m in models}

    url = provider["base_url"].rstrip("/") + "/chat/completions"

    def probe(model: str):
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }).encode()
        request = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if payload.get("error"):
                return model, (False, _classify_reason(None, str(payload["error"])))
            if not payload.get("choices"):
                return model, (False, "empty response")
            return model, (True, "")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            return model, (False, _classify_reason(exc.code, detail))
        except Exception as exc:  # noqa: BLE001
            return model, (False, type(exc).__name__)

    results: dict = {}
    with ThreadPoolExecutor(max_workers=min(len(models), 10)) as pool:
        for model, outcome in pool.map(probe, models):
            results[model] = outcome
    return results


def _classify_reason(status_code, detail: str) -> str:
    """Turns a provider error into a short phrase a user can act on."""
    text = (detail or "").lower()
    if status_code == 429 or "rate limit" in text or "per-day" in text:
        return "daily free quota used up"
    if status_code == 403 or "only available on" in text:
        return "not available via plain API"
    if status_code in (502, 503, 504) or "aborted" in text or "timed out" in text:
        return "provider down"
    if status_code == 404:
        return "model not offered"
    return (detail or "unavailable")[:60]


def ping_models(models, config: dict, timeout: int = 20) -> list[str]:
    """The subset of `models` answering right now, in the caller's preference order.

    Thin wrapper over probe_models for callers that only need the live set.
    """
    results = probe_models(models, config, timeout=timeout)
    return [m for m in models if results.get(m, (True, ""))[0]]


class AIClient:
    def __init__(self, config: dict, cancel_event: threading.Event | None = None,
                 request_semaphore: threading.Semaphore | None = None):
        provider_cfg = config["ai_provider"]
        key_env = provider_cfg["api_key_env"]
        api_key = os.environ.get(key_env)
        if not api_key:
            raise AIClientError(
                f"Environment variable '{key_env}' is not set. "
                f"Copy .env.example to .env and fill in your key, or export it directly."
            )

        # Explicit timeout — the SDK defaults to 600s/request, which turns one hung
        # call into a 10-minute silent stall instead of a fast, retryable failure.
        timeout = provider_cfg.get("request_timeout_seconds", 60)
        self._client = OpenAI(base_url=provider_cfg["base_url"], api_key=api_key, timeout=timeout)

        self._default_model = provider_cfg["model"]
        self._default_temperature = provider_cfg.get("temperature", 0.7)
        self._max_retries = provider_cfg.get("max_retries", 2)
        # Extra provider-routing hints (OpenRouter): e.g. {"ignore": ["nvidia"]}
        self._provider_routing = provider_cfg.get("provider_routing") or {}

        # Fallback chain: configured model first, then the rest of the known-free list.
        fallbacks = [m for m in (config.get("free_models") or []) if m != self._default_model]
        self._model_chain = [self._default_model] + fallbacks

        self._cancel_event = cancel_event
        # Shared across every parallel job so total in-flight calls stay bounded;
        # without it, 5 jobs each writing sections concurrently burst into 429s.
        self._request_semaphore = request_semaphore
        self._exhausted: set[str] = set()   # models that hit their per-day cap this run
        self._lock = threading.Lock()
        self.last_model: str | None = None

    # ------------------------------------------------------------------ helpers

    def _check_cancelled(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise CancelledError("Stopped by user")

    def _sleep(self, seconds: float):
        """Interruptible sleep so Stop doesn't wait out a long backoff."""
        if self._cancel_event is not None:
            if self._cancel_event.wait(timeout=seconds):
                raise CancelledError("Stopped by user")
        else:
            time.sleep(seconds)

    def _mark_exhausted(self, model: str):
        with self._lock:
            if model not in self._exhausted:
                self._exhausted.add(model)
                logger.warning("Model %s hit its per-day free quota — skipping it for this run", model)

    def _is_exhausted(self, model: str) -> bool:
        with self._lock:
            return model in self._exhausted

    def _extra_body(self, model: str) -> dict:
        """Provider hints, minus any that would exclude the model's own vendor.

        Ignoring 'nvidia' steers gemma away from NVIDIA's exhausted free workers, but
        applying the same filter to nvidia/nemotron-* would 404 ("no allowed providers
        are available for the selected model"), so drop the clash for those requests.
        """
        if not self._provider_routing:
            return {}

        routing = dict(self._provider_routing)
        vendor = model.split("/", 1)[0].lower()

        ignored = [p for p in routing.get("ignore", []) if p.lower() != vendor]
        if ignored:
            routing["ignore"] = ignored
        else:
            routing.pop("ignore", None)

        return {"provider": routing} if routing else {}

    # ------------------------------------------------------------------- public

    def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        temperature = self._default_temperature if temperature is None else temperature

        # An agent-pinned model is tried first, then the shared chain as backup.
        if model:
            chain = [model] + [m for m in self._model_chain if m != model]
        else:
            chain = list(self._model_chain)

        attempted = [m for m in chain if not self._is_exhausted(m)]
        if not attempted:
            raise AIClientError(
                "Every configured model has hit its per-day free quota. "
                "Wait for the quota to reset, add credits, or configure a different model."
            )

        failures: list[str] = []
        for candidate in attempted:
            self._check_cancelled()
            try:
                text = self._call_one_model(candidate, system_prompt, user_prompt, temperature)
                if candidate != self._default_model:
                    logger.info("Served by fallback model %s", candidate)
                self.last_model = candidate
                return text
            except CancelledError:
                raise
            except AIClientError as exc:
                failures.append(f"{candidate}: {exc}")
                continue

        raise AIClientError("All models failed — " + " | ".join(failures))

    # ------------------------------------------------------------------ internal

    def _call_one_model(self, model: str, system_prompt: str, user_prompt: str, temperature: float) -> str:
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            self._check_cancelled()
            try:
                if self._request_semaphore is not None:
                    self._request_semaphore.acquire()
                try:
                    response = self._client.chat.completions.create(
                        model=model,
                        temperature=temperature,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        extra_body=self._extra_body(model),
                    )
                finally:
                    if self._request_semaphore is not None:
                        self._request_semaphore.release()
            except APIStatusError as exc:
                detail = self._error_text(exc)
                status_code = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if self._is_daily_cap(detail, status_code):
                    self._mark_exhausted(model)
                    raise AIClientError("per-day free quota reached") from exc
                last_error = exc
                if attempt < self._max_retries:
                    self._sleep(self._retry_delay(exc, attempt))
                continue
            except (APIError, APITimeoutError) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    self._sleep(2 ** attempt)
                continue

            # Some providers return HTTP 200 with an embedded error and null choices.
            if not response.choices:
                embedded = getattr(response, "error", None)
                detail = str(embedded)
                embedded_code = None
                if isinstance(embedded, dict):
                    embedded_code = embedded.get("code")
                if self._is_daily_cap(detail, embedded_code):
                    self._mark_exhausted(model)
                    raise AIClientError("per-day free quota reached")
                last_error = AIClientError(f"empty response ({detail})")
                if attempt < self._max_retries:
                    self._sleep(2 ** attempt)
                continue

            content = response.choices[0].message.content or ""
            if not content.strip():
                last_error = AIClientError("model returned empty text")
                if attempt < self._max_retries:
                    self._sleep(2 ** attempt)
                continue

            return content

        raise AIClientError(f"failed after {self._max_retries + 1} attempts: {last_error}")

    @staticmethod
    def _error_text(exc: APIStatusError) -> str:
        try:
            return str(exc.response.text)
        except Exception:
            return str(exc)

    @staticmethod
    def _is_daily_cap(detail: str, status_code=None) -> bool:
        """A per-day quota is only ever reported as 429.

        Matching on message text alone misfired: a transient 502 whose body happened to
        mention a rate limit blacklisted a healthy model mid-article, so the rest of the
        run fell through to slower fallbacks for no reason. Require the 429 as well.
        """
        if status_code is not None and int(status_code) != 429:
            return False
        low = (detail or "").lower()
        return any(marker in low for marker in _DAILY_CAP_MARKERS)

    def _retry_delay(self, exc: APIStatusError, attempt: int) -> float:
        retry_after = None
        try:
            if exc.response is not None:
                retry_after = exc.response.headers.get("Retry-After")
        except Exception:
            retry_after = None
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return 2 ** attempt
