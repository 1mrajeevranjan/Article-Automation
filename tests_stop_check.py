"""Verifies Stop actually halts a run promptly instead of grinding through the batch.

Run: .venv/bin/python tests_stop_check.py
"""

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("AI_API_KEY", "test-key")

import orchestrator


class SlowClient:
    """Stands in for a provider that takes 5s per call, honouring cancellation the way
    AIClient does (checks the event before each call and during backoff)."""

    def __init__(self, cancel_event):
        self.cancel_event = cancel_event
        self.calls = 0

    def chat_completion(self, system_prompt, user_prompt, model=None, temperature=None):
        from ai_client import CancelledError
        if self.cancel_event.is_set():
            raise CancelledError("Stopped by user")
        self.calls += 1
        # interruptible work
        if self.cancel_event.wait(timeout=5):
            raise CancelledError("Stopped by user")
        if "###VERDICT###" in user_prompt:
            return "###VERDICT###\nwithin_range\n###SECTIONS###\nnone"
        if "###ABSTRACT###" in user_prompt or "###KEYWORDS###" in user_prompt:
            return "###ABSTRACT###\nabs\n###KEYWORDS###\n" + ", ".join("k" for _ in range(10))
        if "###INTRODUCTION###" in user_prompt:
            return "###INTRODUCTION###\nintro\n###CONCLUSION###\nconc"
        return "text " * 50


CONFIG = {
    "ai_provider": {"article_timeout_seconds": 600, "max_concurrent_requests": 2},
    "validation": {"word_count_tolerance_percent": 10, "max_correction_passes": 1},
    "abstract": {"min_words": 10, "max_words": 20},
    "agents": {},
    "writing_style": "ieee_paper",
}


def main():
    cancel = threading.Event()
    client = SlowClient(cancel)

    # Fire Stop 2 seconds in — mid-article, mid-call.
    threading.Timer(2.0, cancel.set).start()

    t0 = time.time()
    state = orchestrator.run_article(
        1, "Test Article", "Test scope", "", 2000, 5, CONFIG, client, Path("/tmp")
    )
    dt = time.time() - t0

    print(f"returned after   : {dt:.1f}s (Stop fired at 2.0s)")
    print(f"status           : {state.status}")
    print(f"notes            : {state.notes}")
    print(f"provider calls   : {client.calls}")

    assert state.status == "Stopped", f"expected Stopped, got {state.status}"
    assert dt < 8, f"took {dt:.1f}s to stop — too slow"
    print("\nSTOP BEHAVIOUR: OK (halts promptly, marks row Stopped, batch can resume)")


if __name__ == "__main__":
    main()
