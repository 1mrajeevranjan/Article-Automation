"""Loads/saves config.yaml and the AI_API_KEY line in .env — used by the GUI settings tab."""

import json
import urllib.request
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)


def load_api_key() -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("AI_API_KEY="):
            return line.split("=", 1)[1]
    return ""


def fetch_free_models(api_key: str, base_url: str, timeout: int = 15) -> list[str]:
    """Live free-model list from the provider's /models endpoint (OpenRouter-style).
    Returns [] on any failure — the caller falls back to the configured list."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp).get("data", [])
    return sorted(m["id"] for m in data if str(m.get("id", "")).endswith(":free"))


def save_api_key(key: str):
    """Rewrites .env keeping any other lines intact, replacing/adding AI_API_KEY only."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.startswith("AI_API_KEY="):
            lines[i] = f"AI_API_KEY={key}"
            found = True
            break
    if not found:
        lines.append(f"AI_API_KEY={key}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
