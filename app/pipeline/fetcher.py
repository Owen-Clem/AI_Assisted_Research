import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.database import article_exists, insert_article, upsert_source
from app.scrapers.rss import get_scraper

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "sources.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


async def run_fetcher() -> tuple[int, int]:
    """Fetch new articles from all sources. Returns (fetched_count, skipped_count)."""
    config = load_config()
    lookback_days = config.get("lookback_days", 1)
    sources = config.get("sources", [])

    fetched = 0
    skipped = 0

    for source_cfg in sources:
        source_id = upsert_source(
            name=source_cfg["name"],
            url=source_cfg["url"],
            feed_type=source_cfg.get("type", "rss"),
            reputation=source_cfg["reputation"],
        )

        scraper = get_scraper(source_cfg)
        try:
            articles = await scraper.fetch(lookback_days)
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", source_cfg["name"], e)
            continue

        for article in articles:
            if article_exists(article.url):
                skipped += 1
                continue

            insert_article(
                source_id=source_id,
                url=article.url,
                title=article.title,
                published_at=article.published_at,
                content_text=article.content_text,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            fetched += 1
            logger.info("Fetched: %s", article.title)

    return fetched, skipped
