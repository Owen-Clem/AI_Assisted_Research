import logging
import math
import os
from datetime import datetime, timezone

from app.database import get_articles_by_state, get_summary, set_article_state, upsert_score

logger = logging.getLogger(__name__)

WEIGHTS = {
    "pt_relevance": float(os.getenv("SCORER_WEIGHT_PT_RELEVANCE", "0.65")),
    "recency":      float(os.getenv("SCORER_WEIGHT_RECENCY",       "0.15")),
    "reputation":   float(os.getenv("SCORER_WEIGHT_REPUTATION",    "0.20")),
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"


_RECENCY_HALF_LIFE_DAYS = float(os.getenv("SCORER_RECENCY_HALF_LIFE_DAYS", "14.0"))

def _recency_score(published_at: str) -> float:
    try:
        pub = datetime.fromisoformat(published_at)
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - pub).total_seconds() / 86400
        return math.exp(-math.log(2) * age_days / _RECENCY_HALF_LIFE_DAYS)
    except Exception:
        return 0.5


def _score_article(article, summary_row) -> float:
    pt_relevance = float(summary_row["pt_relevance"] or 0.5)
    reputation   = article["reputation"] / 5.0
    recency      = _recency_score(article["published_at"] or "")

    total = (
        pt_relevance * WEIGHTS["pt_relevance"] +
        recency      * WEIGHTS["recency"]      +
        reputation   * WEIGHTS["reputation"]
    ) * 100

    upsert_score(
        article_id=article["id"],
        recency=recency,
        reputation=reputation,
        pt_relevance=pt_relevance,
        total=round(total, 1),
    )
    return total


def run_scorer() -> int:
    articles = get_articles_by_state("summarized")
    count = 0

    for article in articles:
        summary_row = get_summary(article["id"])
        if not summary_row:
            continue
        total = _score_article(article, summary_row)
        set_article_state(article["id"], "ranked")
        logger.info("Scored (%.1f): %s", total, article["title"])
        count += 1

    return count


def rescore_all() -> int:
    """Re-score all ranked articles with the current formula. Used after tuning weights."""
    from app.database import db
    with db() as conn:
        articles = conn.execute(
            """SELECT a.*, s.reputation FROM articles a
               JOIN sources s ON a.source_id = s.id
               WHERE a.pipeline_state = 'ranked'"""
        ).fetchall()
    count = 0
    for article in articles:
        summary_row = get_summary(article["id"])
        if not summary_row:
            continue
        total = _score_article(article, summary_row)
        logger.info("Re-scored (%.1f): %s", total, article["title"])
        count += 1
    return count
