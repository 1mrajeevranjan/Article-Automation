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


def year_fields(state) -> dict:
    """Prompt fragments enforcing the sheet's Year column as a knowledge cutoff.

    A paper dated 2021 that cites 2024 work, or discusses methods published after its
    own date, is not merely untidy — it is wrong. Empty strings when the sheet has no
    Year column, so the prompts read normally in that case.
    """
    year = getattr(state, "year", None)
    if not year:
        return {"year_constraint": "", "year_reference_rule": ""}

    return {
        "year_constraint": (
            f"TEMPORAL CONSTRAINT — the article is dated {year}. Write as though the "
            f"present year is {year}: rely only on research, methods, systems, datasets "
            f"and events published or known on or before {year}. Do not mention, cite or "
            f"allude to anything that appeared after {year}, and do not describe "
            f"post-{year} developments as current or forthcoming facts."
        ),
        "year_reference_rule": (
            f"- EVERY reference must carry a publication year of {year} or earlier. "
            f"Nothing dated after {year} is acceptable under any circumstances."
        ),
    }


def agent_model_temperature(config: dict, agent_key: str):
    agent_cfg = config.get("agents", {}).get(agent_key) or {}
    model = agent_cfg.get("model")  # None -> AIClient falls back to provider default
    temperature = agent_cfg.get("temperature")
    return model, temperature
