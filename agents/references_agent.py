import logging
import re

from ai_client import AIClient
from state import ArticleState
from agents._prompts import load_prompt, agent_model_temperature, year_fields

logger = logging.getLogger("references_agent")

SYSTEM_PROMPT = "You compile plausible, well-formatted IEEE-style reference lists for research papers. Follow the output format exactly."

_LINE_PATTERN = re.compile(r"^\[\d+\]\s*.+")
_YEAR_IN_CITATION = re.compile(r"\b(1[89]\d{2}|20\d{2}|21\d{2})\b")

TARGET_COUNT = 20
MAX_TOP_UP_PASSES = 2


def citation_year(reference: str) -> int | None:
    """The publication year of an IEEE citation — the last year-like number in it.

    Last, not first: titles carry years too ("Lessons from the 2008 Crisis," IEEE
    Trans., 2019), and it is the publication date that has to respect the cutoff.
    """
    matches = _YEAR_IN_CITATION.findall(reference)
    return int(matches[-1]) if matches else None


def within_cutoff(reference: str, year: int | None) -> bool:
    """Undated citations are kept: the model omitting a year is not evidence that the
    work postdates the cutoff, and dropping them would silently shrink the list."""
    if not year:
        return True
    found = citation_year(reference)
    return found is None or found <= year


def _renumber(references: list[str]) -> list[str]:
    """Filtering leaves gaps in [1]..[20]; IEEE numbering must stay contiguous."""
    return [re.sub(r"^\[\d+\]", f"[{i}]", ref) for i, ref in enumerate(references, start=1)]


def _parse(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if _LINE_PATTERN.match(line.strip())]


def run(state: ArticleState, config: dict, client: AIClient) -> ArticleState:
    template = load_prompt("references_agent")
    model, temperature = agent_model_temperature(config, "references_agent")
    user_prompt = template.format(title=state.title, scope=state.scope, **year_fields(state))

    raw = client.chat_completion(SYSTEM_PROMPT, user_prompt, model=model, temperature=temperature)
    kept, dropped = [], 0
    for ref in _parse(raw):
        if within_cutoff(ref, state.year):
            kept.append(ref)
        else:
            dropped += 1

    # The temporal rule is in the prompt, but a prompt is a request, not a guarantee —
    # models routinely slip a 2024 citation into a 2021 paper. Ask again for the
    # shortfall rather than shipping a list that violates the constraint or is short.
    passes = 0
    while state.year and len(kept) < TARGET_COUNT and passes < MAX_TOP_UP_PASSES:
        passes += 1
        missing = TARGET_COUNT - len(kept)
        logger.info("Row %s: %s reference(s) postdated %s — requesting %s replacement(s)",
                    state.row_number, dropped, state.year, missing)
        retry_prompt = (
            f"{user_prompt}\n\nIMPORTANT: your previous attempt included references dated "
            f"after {state.year}, which were discarded. Supply {missing} further IEEE "
            f"references on this topic, every one published in {state.year} or earlier. "
            f"Do not repeat any of these already-accepted references:\n"
            + "\n".join(kept)
        )
        try:
            more = client.chat_completion(SYSTEM_PROMPT, retry_prompt, model=model, temperature=temperature)
        except Exception as exc:  # noqa: BLE001 - a short list beats a failed article
            logger.warning("Row %s: reference top-up failed (%s) — keeping %s references",
                           state.row_number, exc, len(kept))
            break
        added = 0
        for ref in _parse(more):
            if len(kept) >= TARGET_COUNT:
                break
            if within_cutoff(ref, state.year):
                kept.append(ref)
                added += 1
        if not added:
            break

    if state.year:
        offenders = [r for r in kept if not within_cutoff(r, state.year)]
        assert not offenders, f"reference postdates cutoff {state.year}: {offenders[:1]}"

    state.references = _renumber(kept[:TARGET_COUNT])
    return state
