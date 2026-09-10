"""Empirically determines how to make OpenRouter requests succeed reliably:

1. Which upstream provider actually serves each :free model request
2. Whether the request-level `models: [...]` fallback array works
3. Whether `provider: {"ignore": [...]}` steers away from exhausted upstreams

Run: .venv/bin/python tests_routing_probe.py
"""

import json
import os
import time
import urllib.request

from dotenv import load_dotenv

load_dotenv()

KEY = os.environ["AI_API_KEY"]
URL = "https://openrouter.ai/api/v1/chat/completions"
PROMPT = "Write exactly two sentences about industrial data architecture."


def call(body: dict, timeout: int = 90):
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()[:200]
        return None, time.time() - t0, f"HTTP {e.code}: {body_txt}"
    except Exception as e:
        return None, time.time() - t0, f"{type(e).__name__}: {str(e)[:160]}"

    dt = time.time() - t0
    if data.get("error"):
        return None, dt, f"embedded: {json.dumps(data['error'])[:200]}"
    if not data.get("choices"):
        return None, dt, f"no choices: {json.dumps(data)[:200]}"
    return data, dt, None


def describe(data):
    provider = data.get("provider", "?")
    model = data.get("model", "?")
    text = data["choices"][0]["message"].get("content") or ""
    return f"provider={provider} model={model} words={len(text.split())}"


FREE_MODELS = [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "poolside/laguna-xs-2.1:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "liquid/lfm-2.5-2.6b:free",
    "cohere/north-mini-code:free",
    "dots-studio/dots-3-note-preview:free",
    "inclusionai/ling-3.0-flash-fin:free",
]

print("=" * 78)
print("TEST 1 — plain request per model: which provider serves it, does it work?")
print("=" * 78)
serving = {}
for m in FREE_MODELS:
    data, dt, err = call({"model": m, "messages": [{"role": "user", "content": PROMPT}]})
    if err:
        print(f"  FAIL {m:44s} {dt:5.1f}s  {err[:110]}")
    else:
        info = describe(data)
        serving[m] = data.get("provider")
        print(f"  OK   {m:44s} {dt:5.1f}s  {info}")

print()
print("=" * 78)
print("TEST 2 — request-level `models` fallback array (OpenRouter tries each in order)")
print("=" * 78)
data, dt, err = call({
    "models": FREE_MODELS,
    "messages": [{"role": "user", "content": PROMPT}],
})
if err:
    print(f"  FAIL  {dt:5.1f}s  {err[:300]}")
else:
    print(f"  OK    {dt:5.1f}s  {describe(data)}")
    print("  -> `models` fallback array WORKS; OpenRouter auto-selects a live model.")

print()
print("=" * 78)
print("TEST 3 — models fallback + ignore exhausted upstreams (nvidia)")
print("=" * 78)
data, dt, err = call({
    "models": FREE_MODELS,
    "messages": [{"role": "user", "content": PROMPT}],
    "provider": {"ignore": ["nvidia"], "sort": "throughput"},
})
if err:
    print(f"  FAIL  {dt:5.1f}s  {err[:300]}")
else:
    print(f"  OK    {dt:5.1f}s  {describe(data)}")

print()
print("=" * 78)
print("TEST 4 — repeat fallback array 5x to gauge reliability")
print("=" * 78)
ok = 0
for i in range(5):
    data, dt, err = call({
        "models": FREE_MODELS,
        "messages": [{"role": "user", "content": PROMPT}],
        "provider": {"ignore": ["nvidia"]},
    })
    if err:
        print(f"  run {i+1}: FAIL {dt:5.1f}s {err[:100]}")
    else:
        ok += 1
        print(f"  run {i+1}: OK   {dt:5.1f}s {describe(data)}")
print(f"\n  reliability: {ok}/5 succeeded")
