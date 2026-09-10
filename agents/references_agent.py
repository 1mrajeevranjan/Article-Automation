import re

from ai_client import AIClient
from state import ArticleState
from agents._prompts import load_prompt, agent_model_temperature

SYSTEM_PROMPT = "You compile plausible, well-formatted IEEE-style reference lists for research papers. Follow the output format exactly."

_LINE_PATTERN = re.compile(r"^\[\d+\]\s*.+")


def run(state: ArticleState, config: dict, client: AIClient) -> ArticleState:
    template = load_prompt("references_agent")
    user_prompt = template.format(title=state.title, scope=state.scope)
    model, temperature = agent_model_temperature(config, "references_agent")
    raw = client.chat_completion(SYSTEM_PROMPT, user_prompt, model=model, temperature=temperature)

    references = [line.strip() for line in raw.splitlines() if _LINE_PATTERN.match(line.strip())]
    state.references = references[:20]
    return state
