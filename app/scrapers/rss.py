import gzip
import json
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser
import httpx
from bs4 import BeautifulSoup

from .base import BaseScraper, RawArticle


def _strip_html(html: str) -> str:
    if not html or "<" not in html:
        return re.sub(r"\s+", " ", html).strip()
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(entry) -> str:
    for attr in ("published", "updated"):
        val = getattr(entry, attr, None)
        if val:
            try:
                dt = parsedate_to_datetime(val)
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


class RssScraper(BaseScraper):
    async def fetch(self, lookback_days: int) -> list[RawArticle]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        articles = []

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(self.url, headers={"User-Agent": "SecurityResearchBot/1.0"})
            resp.raise_for_status()

        feed = feedparser.parse(resp.text)

        for entry in feed.entries:
            pub_str = _parse_date(entry)
            try:
                pub_dt = datetime.fromisoformat(pub_str)
            except ValueError:
                continue

            if pub_dt < cutoff:
                continue

            url = entry.get("link", "")
            if not url:
                continue

            title = entry.get("title", "No title")

            raw_content = (
                entry.get("content", [{}])[0].get("value", "")
                or entry.get("summary", "")
            )
            content_text = _strip_html(raw_content)
            excerpt = content_text[:500]

            articles.append(RawArticle(
                url=url,
                title=title,
                published_at=pub_str,
                content_text=content_text,
                excerpt=excerpt,
            ))

        return articles


class NvdJsonScraper(BaseScraper):
    async def fetch(self, lookback_days: int) -> list[RawArticle]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        articles = []

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(self.url, headers={"User-Agent": "SecurityResearchBot/1.0"})
            resp.raise_for_status()

        data = json.loads(gzip.decompress(resp.content))
        cve_items = data.get("CVE_Items", [])

        for item in cve_items:
            try:
                pub_str = item["publishedDate"]
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue

            if pub_dt < cutoff:
                continue

            cve_id = item.get("cve", {}).get("CVE_data_meta", {}).get("ID", "UNKNOWN")
            descriptions = item.get("cve", {}).get("description", {}).get("description_data", [])
            desc = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")

            refs = item.get("cve", {}).get("references", {}).get("reference_data", [])
            url = refs[0]["url"] if refs else f"https://nvd.nist.gov/vuln/detail/{cve_id}"

            content_text = f"{cve_id}: {desc}"
            articles.append(RawArticle(
                url=url,
                title=cve_id,
                published_at=pub_str,
                content_text=content_text,
                excerpt=content_text[:500],
            ))

        return articles


def get_scraper(source_config: dict) -> BaseScraper:
    feed_type = source_config.get("type", "rss")
    if feed_type == "nvd_json":
        return NvdJsonScraper(source_config)
    return RssScraper(source_config)
