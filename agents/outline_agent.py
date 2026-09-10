from ai_client import AIClient
from state import ArticleState
from agents._prompts import load_prompt, agent_model_temperature, style_fields

SYSTEM_PROMPT = "You produce clean, logically ordered article outlines. Follow instructions exactly."


def run(state: ArticleState, config: dict, client: AIClient) -> ArticleState:
    num_middle = state.middle_section_count()
    template = load_prompt("outline_agent")
    user_prompt = template.format(
        title=state.title,
        scope=state.scope,
        num_middle_sections=num_middle,
        **style_fields(config),
    )
    model, temperature = agent_model_temperature(config, "outline_agent")
    raw = client.chat_completion(SYSTEM_PROMPT, user_prompt, model=model, temperature=temperature)

    headings = [line.strip("-* \t") for line in raw.splitlines() if line.strip()]
    headings = headings[:num_middle]
    while len(headings) < num_middle:
        headings.append(f"Additional Discussion {len(headings) + 1}")

    state.outline = headings
    return state
