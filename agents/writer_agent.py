from ai_client import AIClient
from state import ArticleState
from agents._prompts import load_prompt, agent_model_temperature, style_fields

SYSTEM_PROMPT = "You write focused, on-topic article sections. Follow the target word count and instructions closely."

_MODE_INSTRUCTIONS = {
    "write": "",
    "expand": "This section needs to be EXPANDED with more depth and detail while preserving its existing structure and meaning — do not pad with filler.",
    "condense": "This section needs to be CONDENSED, tightening the prose while preserving key content and structure.",
}


def run(
    state: ArticleState,
    config: dict,
    client: AIClient,
    section_heading: str,
    target_words: int,
    mode: str = "write",
) -> ArticleState:
    template = load_prompt("writer_agent")
    user_prompt = template.format(
        title=state.title,
        scope=state.scope,
        outline=", ".join(state.outline),
        section_heading=section_heading,
        target_words=target_words,
        mode_instruction=_MODE_INSTRUCTIONS.get(mode, ""),
        **style_fields(config),
    )
    model, temperature = agent_model_temperature(config, "writer_agent")
    text = client.chat_completion(SYSTEM_PROMPT, user_prompt, model=model, temperature=temperature)
    state.section_drafts[section_heading] = text.strip()
    return state
