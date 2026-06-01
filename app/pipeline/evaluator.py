import asyncio
import logging
import os
from datetime import datetime, timezone

import anthropic
from anthropic import RateLimitError

from app.pipeline import parse_json_response, _require_env
from app.database import get_articles_by_state, set_article_state, title_already_processed, upsert_summary, upsert_score

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
ACCEPT_THRESHOLD = int(os.getenv("EVAL_ACCEPT_THRESHOLD", "55"))
CONCURRENCY = 3  # 50 RPM org limit; Haiku responds ~1s so 3 concurrent ≈ 45 RPM safe

SYSTEM_PROMPT = _require_env("EVAL_SYSTEM_PROMPT")
EVAL_PROMPT = _require_env("EVAL_USER_PROMPT")


async def _evaluate_one(sem: asyncio.Semaphore, client: anthropic.AsyncAnthropic, article) -> str:
    """Evaluate a single article. Returns 'accepted', 'preliminary', 'duplicate', or 'error'."""
    if title_already_processed(article["title"]):
        upsert_summary(
            article_id=article["id"],
            summary_text="Duplicate title — already covered by another source.",
            tooling=[],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        upsert_score(article_id=article["id"], recency=0.0, reputation=0.0, pt_relevance=0.0, total=0.0)
        set_article_state(article["id"], "preliminary_rated")
        logger.debug("DUPLICATE TITLE (skipped eval): %s", article["title"])
        return "duplicate"

    excerpt = (article["content_text"] or "")[:1000]
    prompt = EVAL_PROMPT.format(title=article["title"], excerpt=excerpt)

    score = 0
    brief = ""
    success = False
    async with sem:
        for attempt in range(4):
            try:
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=60,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                data = parse_json_response(response.content[0].text)
                score = max(0, min(100, int(data.get("score", 0))))
                brief = str(data.get("brief", ""))[:200]
                success = True
                break
            except RateLimitError:
                wait = 5 * 2 ** attempt  # 5s, 10s, 20s, 40s
                logger.warning("Rate limited, retrying in %ds (article %s)", wait, article["id"])
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("Evaluator error for article %s: %s", article["id"], e)
                break

    if not success:
        logger.warning("Evaluation failed for article %s after all retries — marking rejected", article["id"])
        set_article_state(article["id"], "evaluated_rejected")
        return "error"

    if score >= ACCEPT_THRESHOLD:
        set_article_state(article["id"], "evaluated_accepted")
        logger.info("ACCEPTED (%d): %s", score, article["title"])
        return "accepted"

    excerpt_summary = brief or (article["content_text"] or "")[:300]
    upsert_summary(
        article_id=article["id"],
        summary_text=excerpt_summary,
        tooling=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    upsert_score(article_id=article["id"], recency=0.0, reputation=0.0, pt_relevance=0.0, total=round(score, 1))
    set_article_state(article["id"], "preliminary_rated")
    logger.debug("PRELIMINARY (%d): %s", score, article["title"])
    return "preliminary"


async def run_evaluator() -> tuple[int, int]:
    """Score all fetched articles concurrently. Returns (accepted, preliminary_rated)."""
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    articles = get_articles_by_state("fetched")

    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[_evaluate_one(sem, client, a) for a in articles])

    accepted = results.count("accepted")
    preliminary = results.count("preliminary") + results.count("duplicate")
    errors = results.count("error")
    if errors:
        logger.warning("Evaluator: %d articles left in 'fetched' state due to errors", errors)
    return accepted, preliminary
