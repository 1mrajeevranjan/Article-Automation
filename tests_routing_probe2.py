"""Verifies provider pinning + a 3-item models fallback array, repeated for reliability."""

import json
import os
import time
import urllib.request

from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["AI_API_KEY"]
URL = "https://openrouter.ai/api/v1/chat/completions"
PROMPT = "Write exactly two sentences about industrial data architecture."


def call(body, timeout=90):
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return None, time.time() - t0, f"HTTP {e.code}: {e.read().decode()[:150]}"
    except Exception as e:
        return None, time.time() - t0, f"{type(e).__name__}: {str(e)[:140]}"
    dt = time.time() - t0
    if data.get("error"):
        return None, dt, f"embedded: {json.dumps(data['error'])[:170]}"
    if not data.get("choices"):
        return None, dt, f"no choices: {json.dumps(data)[:170]}"
    return data, dt, None


def show(tag, data, dt, err):
    if err:
        print(f"  {tag:38s} FAIL {dt:5.1f}s  {err[:110]}")
        return False
    txt = data["choices"][0]["message"].get("content") or ""
    print(f"  {tag:38s} OK   {dt:5.1f}s  provider={data.get('provider')} model={data.get('model')} words={len(txt.split())}")
    return True

GEMMA = "google/gemma-4-26b-a4b-it:free"
NEMO = "nvidia/nemotron-3-super-120b-a12b:free"

print("TEST A — provider slug for pinning gemma to Google AI Studio")
for slug in ["google-ai-studio", "google", "Google AI Studio", "google-vertex"]:
    data, dt, err = call({
        "model": GEMMA, "messages": [{"role": "user", "content": PROMPT}],
        "provider": {"only": [slug], "allow_fallbacks": False},
    })
    show(f'only=["{slug}"]', data, dt, err)

print("\nTEST B — 3-item models fallback array (the documented max)")
for combo in ([GEMMA, NEMO, "google/gemma-4-31b-it:free"],
              [NEMO, GEMMA, "poolside/laguna-xs-2.1:free"]):
    data, dt, err = call({"models": combo, "messages": [{"role": "user", "content": PROMPT}]})
    show(f"models={len(combo)} first={combo[0].split('/')[1][:18]}", data, dt, err)

print("\nTEST C — ignore nvidia upstream on gemma (10 runs, reliability check)")
ok = 0
providers = {}
for i in range(10):
    data, dt, err = call({
        "model": GEMMA, "messages": [{"role": "user", "content": PROMPT}],
        "provider": {"ignore": ["nvidia"]},
    })
    if show(f"run {i+1}", data, dt, err):
        ok += 1
        providers[data.get("provider")] = providers.get(data.get("provider"), 0) + 1
print(f"\n  reliability: {ok}/10   providers seen: {providers}")

print("\nTEST D — bare gemma, 10 runs (no provider hints) for comparison")
ok2 = 0
providers2 = {}
for i in range(10):
    data, dt, err = call({"model": GEMMA, "messages": [{"role": "user", "content": PROMPT}]})
    if show(f"run {i+1}", data, dt, err):
        ok2 += 1
        providers2[data.get("provider")] = providers2.get(data.get("provider"), 0) + 1
print(f"\n  reliability: {ok2}/10   providers seen: {providers2}")
