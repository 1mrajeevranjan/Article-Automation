from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def style_fields(config: dict) -> dict:
    """Style-driven prompt fragments, so every agent writes in the selected voice."""
    from style_guides import get_style

    style = get_style(config)
    return {
        "style_voice": style["voice"],
        "style_section_guidance": style["section_guidance"],
        "style_abstract_guidance": style["abstract_guidance"],
    }


def agent_model_temperature(config: dict, agent_key: str):
    agent_cfg = config.get("agents", {}).get(agent_key) or {}
    model = agent_cfg.get("model")  # None -> AIClient falls back to provider default
    temperature = agent_cfg.get("temperature")
    return model, temperature
