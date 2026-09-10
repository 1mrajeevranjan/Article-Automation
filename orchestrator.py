import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_client import AIClient, AIClientError, CancelledError
from state import ArticleState
from style_guides import get_style
from agents import outline_agent, writer_agent, intro_conclusion_agent, editor_validator_agent, abstract_keywords_agent, references_agent, pdf_output_agent

logger = logging.getLogger("orchestrator")


def _prompt_int(prompt: str, minimum: int, min_message: str) -> int:
    while True:
        raw = input(prompt).strip()
        if raw.isdigit() and int(raw) >= minimum:
            return int(raw)
        print(min_message)


def prompt_article_targets(index: int, total: int, title: str, scope: str, author: str):
    print(f"\nArticle {index} of {total}")
    print(f"Title: {title}")
    print(f"Scope: {scope}")
    if author:
        print(f"Author: {author}")

    word_count = _prompt_int(
        "Target word count: ", 1, "Please enter a positive integer."
    )
    sections = _prompt_int(
        "Number of sections (including Introduction + Conclusion, minimum 3): ",
        3,
        "Minimum is 3 (Introduction + at least 1 middle section + Conclusion). Try again.",
    )
    return word_count, sections


def _section_word_targets(target_word_count: int, num_middle: int):
    total_weight = 1.5 + num_middle  # intro 0.75 + conclusion 0.75 + middle sections @1.0
    unit = target_word_count / total_weight
    intro_words = round(unit * 0.75)
    conclusion_words = round(unit * 0.75)
    middle_words = round(unit)
    return intro_words, conclusion_words, middle_words


def run_article(
    row_number: int,
    title: str,
    scope: str,
    author: str,
    word_count: int,
    sections: int,
    config: dict,
    client: AIClient,
    output_dir: Path,
    file_label: str | None = None,
) -> ArticleState:
    """Public entry point — enforces a hard wall-clock ceiling so one stuck article
    (e.g. a provider call that hangs past its own timeout/retry budget) can never
    freeze the whole batch. Delegates the real pipeline to _run_article_inner.

    Uses a raw daemon Thread rather than ThreadPoolExecutor: a pool's context-manager
    __exit__ calls shutdown(wait=True), which blocks until the submitted work actually
    finishes — silently defeating the timeout. A plain daemon thread lets this function
    return the instant the deadline passes, and never blocks process/app shutdown if
    the abandoned call is still running years... er, minutes later."""
    timeout = config.get("ai_provider", {}).get("article_timeout_seconds", 600)
    result: dict = {}

    def _worker():
        result["state"] = _run_article_inner(
            row_number, title, scope, author, word_count, sections, config, client, output_dir, file_label
        )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        logger.error("Row %s (%s) exceeded %ss overall timeout — abandoning, batch continues", row_number, title, timeout)
        state = ArticleState(
            row_number=row_number, title=title, scope=scope, author=author,
            target_word_count=word_count, target_sections=sections,
        )
        state.status = "Failed"
        state.notes = (f"Exceeded overall article timeout ({timeout}s) — the provider "
                       f"stalled, or too many batches were sharing the request budget")
        return state

    return result["state"]


def _run_article_inner(
    row_number: int,
    title: str,
    scope: str,
    author: str,
    word_count: int,
    sections: int,
    config: dict,
    client: AIClient,
    output_dir: Path,
    file_label: str | None = None,
) -> ArticleState:
    state = ArticleState(
        row_number=row_number,
        title=title,
        scope=scope,
        author=author,
        target_word_count=word_count,
        target_sections=sections,
    )

    stage_t0 = time.time()

    def _mark(stage: str):
        nonlocal stage_t0
        now = time.time()
        logger.info("Row %s: %s done in %.1fs", row_number, stage, now - stage_t0)
        stage_t0 = now

    try:
        state = outline_agent.run(state, config, client)
        _mark(f"outline ({len(state.outline)} sections)")

        intro_words, conclusion_words, middle_words = _section_word_targets(
            word_count, state.middle_section_count()
        )
        max_workers = max(1, config.get("ai_provider", {}).get("max_concurrent_requests", 5))

        # Middle sections don't depend on each other (each only needs title/scope/outline),
        # so write them concurrently instead of one blocking API round-trip at a time.
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(
                lambda heading: writer_agent.run(state, config, client, heading, middle_words, mode="write"),
                state.outline,
            ))
        _mark("middle sections")

        state = intro_conclusion_agent.run(state, config, client, intro_words, conclusion_words)
        _mark("intro/conclusion")

        tolerance = config.get("validation", {}).get("word_count_tolerance_percent", 10)
        max_passes = config.get("validation", {}).get("max_correction_passes", 3)

        for pass_num in range(max_passes):
            verdict, sections_to_fix, current = editor_validator_agent.evaluate(state, config, client, tolerance)
            _mark(f"editor pass {pass_num + 1} (verdict={verdict}, words={current})")
            if verdict == "within_range":
                break
            mode = "expand" if verdict == "too_short" else "condense"

            def _fix(heading, mode=mode):
                existing_words = len(state.section_drafts[heading].split())
                target = existing_words + round(existing_words * 0.3) if mode == "expand" \
                    else max(existing_words - round(existing_words * 0.3), 50)
                writer_agent.run(state, config, client, heading, target, mode=mode)

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                list(pool.map(_fix, sections_to_fix))
            _mark(f"correction pass {pass_num + 1} rewrite")

        state = abstract_keywords_agent.run(state, config, client)
        _mark("abstract/keywords")

        if get_style(config)["include_references"]:
            state = references_agent.run(state, config, client)
            _mark("references")

        state = pdf_output_agent.run(state, config, output_dir, file_label=file_label)
        _mark("pdf render")

    except CancelledError:
        state.status = "Stopped"
        state.notes = "Stopped by user"
        logger.info("Row %s (%s) stopped by user", row_number, title)
    except AIClientError as exc:
        state.status = "Failed"
        state.notes = str(exc)
        logger.error("Row %s (%s) failed: %s", row_number, title, exc)
    except Exception as exc:  # noqa: BLE001 - one row must never crash the batch
        state.status = "Failed"
        state.notes = f"Unexpected error: {exc}"
        logger.exception("Row %s (%s) failed unexpectedly", row_number, title)

    return state
