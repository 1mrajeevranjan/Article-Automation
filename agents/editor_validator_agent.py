from ai_client import AIClient
from state import ArticleState
from agents._prompts import load_prompt, agent_model_temperature

SYSTEM_PROMPT = "You are a precise editorial validator. Output exactly the requested format, no extra commentary."


def evaluate(state: ArticleState, config: dict, client: AIClient, tolerance_percent: int):
    """Returns (verdict, sections_to_fix: list[str], current_word_count: int)."""
    target = state.target_word_count
    min_words = int(target * (1 - tolerance_percent / 100))
    max_words = int(target * (1 + tolerance_percent / 100))
    current = state.body_word_count()
    state.word_count_history.append(current)

    if min_words <= current <= max_words:
        return "within_range", [], current

    section_word_counts = "\n".join(
        f"{heading}: {len(text.split())} words" for heading, text in state.section_drafts.items()
    )
    template = load_prompt("editor_validator_agent")
    user_prompt = template.format(
        target_word_count=target,
        min_words=min_words,
        max_words=max_words,
        current_word_count=current,
        section_word_counts=section_word_counts,
    )
    model, temperature = agent_model_temperature(config, "editor_agent")
    raw = client.chat_completion(SYSTEM_PROMPT, user_prompt, model=model, temperature=temperature)

    verdict = "too_short" if current < min_words else "too_long"
    sections: list[str] = []
    if "###SECTIONS###" in raw:
        _, after = raw.split("###SECTIONS###", 1)
        names = [s.strip() for s in after.strip().split(",")]
        sections = [n for n in names if n and n.lower() != "none" and n in state.section_drafts]

    if not sections:
        # Fallback: fully local, no extra AI call — pick shortest/longest section by word count.
        by_len = sorted(state.section_drafts.items(), key=lambda kv: len(kv[1].split()))
        sections = [by_len[0][0]] if verdict == "too_short" else [by_len[-1][0]]

    return verdict, sections, current
