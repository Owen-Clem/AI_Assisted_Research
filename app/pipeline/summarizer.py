import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import anthropic
from anthropic import RateLimitError

from app.pipeline import parse_json_response
from app.database import get_articles_by_state, set_article_state, upsert_summary

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
CONCURRENCY = 2  # 30K TPM limit; Sonnet summaries ~3-4K tokens each → 2 concurrent stays safe

SYSTEM_PROMPT = os.environ["SUMMARIZER_SYSTEM_PROMPT"]
SUMMARY_PROMPT = os.environ["SUMMARIZER_USER_PROMPT"]


async def _summarize_one(
    sem: asyncio.Semaphore,
    client: anthropic.AsyncAnthropic,
    article,
) -> bool:
    content = (article["content_text"] or "")[:12000]
    prompt = SUMMARY_PROMPT.format(title=article["title"], content=content)

    async with sem:
        data = None
        for attempt in range(4):
            try:
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=1500,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                data = parse_json_response(response.content[0].text)
                break
            except RateLimitError:
                wait = 15 * 2 ** attempt  # 15s, 30s, 60s, 120s
                logger.warning("Rate limited, retrying in %ds (article %s)", wait, article["id"])
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("Summarizer error for article %s: %s", article["id"], e)
                break
        if data is None:
            return False

    try:
        pt_relevance = max(0.0, min(1.0, float(data.get("pt_relevance", 0.5))))
    except (TypeError, ValueError):
        pt_relevance = 0.5

    upsert_summary(
        article_id=article["id"],
        summary_text=data.get("summary", ""),
        tooling=data.get("tooling", []),
        actors=data.get("threat_actors", []),
        cves=data.get("cves", []),
        pt_relevance=pt_relevance,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    set_article_state(article["id"], "summarized")
    logger.info("Summarized (pt_relevance=%.2f): %s", pt_relevance, article["title"])
    return True


async def run_summarizer(article_ids: list[int] | None = None) -> int:
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if article_ids:
        from app.database import db
        with db() as conn:
            placeholders = ",".join("?" * len(article_ids))
            articles = conn.execute(
                f"SELECT * FROM articles WHERE id IN ({placeholders}) AND pipeline_state='evaluated_accepted'",
                article_ids,
            ).fetchall()
    else:
        articles = get_articles_by_state("evaluated_accepted")

    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[_summarize_one(sem, client, a) for a in articles])
    return sum(results)
