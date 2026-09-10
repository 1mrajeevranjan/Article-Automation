from ai_client import AIClient
from state import ArticleState
from agents._prompts import load_prompt, agent_model_temperature, style_fields

SYSTEM_PROMPT = "You write concise academic abstracts and produce relevant keyword lists. Follow the output format exactly."


def run(state: ArticleState, config: dict, client: AIClient) -> ArticleState:
    body = "\n\n".join(
        [state.intro, *state.section_drafts.values(), state.conclusion]
    )
    abstract_cfg = config.get("abstract", {})
    min_words = abstract_cfg.get("min_words", 150)
    max_words = abstract_cfg.get("max_words", 250)

    template = load_prompt("abstract_keywords_agent")
    user_prompt = template.format(
        title=state.title,
        body=body,
        min_words=min_words,
        max_words=max_words,
        **style_fields(config),
    )
    model, temperature = agent_model_temperature(config, "abstract_agent")
    raw = client.chat_completion(SYSTEM_PROMPT, user_prompt, model=model, temperature=temperature)

    abstract, keywords = "", []
    if "###KEYWORDS###" in raw:
        before, after = raw.split("###KEYWORDS###", 1)
        abstract = before.replace("###ABSTRACT###", "").strip()
        keywords = [k.strip() for k in after.strip().split(",") if k.strip()][:10]
    else:
        abstract = raw.strip()

    state.abstract = abstract
    state.keywords = keywords
    return state
