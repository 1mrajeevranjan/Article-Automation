"""Probes every free model on the configured provider with a realistic section-sized
request, and reports which ones actually work right now, with latency.

Run: .venv/bin/python tests_probe_models.py
"""

import os
import time

from dotenv import load_dotenv
from openai import OpenAI

import config_manager

PROMPT = (
    "Write a 400-word technical section titled 'Data Contracts in Manufacturing Systems' "
    "for a research paper. Formal academic tone. Output only the section body."
)


def main():
    load_dotenv()
    config = config_manager.load_config()
    provider = config["ai_provider"]
    api_key = os.environ[provider["api_key_env"]]
    base_url = provider["base_url"]

    models = config_manager.fetch_free_models(api_key, base_url)
    print(f"Probing {len(models)} free models (60s timeout each)\n")

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=60)
    results = []

    for model in models:
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT}],
                temperature=0.7,
            )
            dt = time.time() - t0
            if not resp.choices:
                err = getattr(resp, "error", None)
                print(f"  FAIL  {model:52s} embedded error: {err}")
                results.append((model, None, f"embedded: {err}", 0, False))
                continue
            text = resp.choices[0].message.content or ""
            words = len(text.split())
            leak = text.lstrip()[:60].lower()
            leaks_reasoning = any(
                leak.startswith(p) for p in ("okay", "here's a thinking", "let me", "first, i", "i need to")
            )
            flag = " [LEAKS REASONING]" if leaks_reasoning else ""
            print(f"  OK    {model:52s} {dt:6.1f}s  {words:4d} words{flag}")
            results.append((model, dt, f"{words} words{flag}", words, leaks_reasoning))
        except Exception as exc:
            dt = time.time() - t0
            msg = str(exc)[:90].replace("\n", " ")
            print(f"  FAIL  {model:52s} {dt:6.1f}s  {msg}")
            results.append((model, None, msg, 0, False))

    # A usable writer must actually write: speed alone would recommend a moderation
    # classifier that returns 3 words. Require output in a sane range of the 400 asked for.
    MIN_WORDS, MAX_WORDS = 150, 1200
    working = [
        r for r in results
        if r[1] is not None and not r[4] and MIN_WORDS <= r[3] <= MAX_WORDS
    ]
    working.sort(key=lambda r: r[1])

    rejected = [
        r for r in results
        if r[1] is not None and (r[4] or not (MIN_WORDS <= r[3] <= MAX_WORDS))
    ]

    print("\n" + "=" * 70)
    if working:
        print("USABLE WRITERS (fastest first, clean output, sane length):")
        for model, dt, info, words, _ in working:
            print(f"  {dt:6.1f}s  {model}  ({info})")
        print(f"\nRECOMMENDED: {working[0][0]}")
    else:
        print("No usable free models right now — all failed, leak reasoning, or return junk length.")

    if rejected:
        print("\nResponded but NOT usable for article writing:")
        for model, dt, info, words, leaks in rejected:
            why = "leaks reasoning text" if leaks else f"{words} words — not a writing model"
            print(f"  {model}  ({why})")


if __name__ == "__main__":
    main()
