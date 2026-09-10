from ai_client import AIClient
from state import ArticleState
from agents._prompts import load_prompt, agent_model_temperature, style_fields, year_fields

SYSTEM_PROMPT = "You write article introductions and conclusions that accurately reflect the body content given to you."


def run(state: ArticleState, config: dict, client: AIClient, intro_words: int, conclusion_words: int) -> ArticleState:
    body_sections = "\n\n".join(
        f"### {heading}\n{text}" for heading, text in state.section_drafts.items()
    )
    template = load_prompt("intro_conclusion_agent")
    user_prompt = template.format(
        title=state.title,
        scope=state.scope,
        body_sections=body_sections,
        intro_words=intro_words,
        conclusion_words=conclusion_words,
        **style_fields(config),
        **year_fields(state),
    )
    model, temperature = agent_model_temperature(config, "intro_conclusion_agent")
    raw = client.chat_completion(SYSTEM_PROMPT, user_prompt, model=model,
                                 temperature=temperature,
                                 min_words=intro_words + conclusion_words)

    intro, conclusion = "", ""
    if "###CONCLUSION###" in raw:
        before, after = raw.split("###CONCLUSION###", 1)
        conclusion = after.strip()
        intro = before.replace("###INTRODUCTION###", "").strip()
    else:
        intro = raw.strip()

    state.intro = intro
    state.conclusion = conclusion
    return state
