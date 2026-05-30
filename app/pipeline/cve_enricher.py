import asyncio
import json
import logging
import os

import httpx

from app.database import get_articles_needing_cve_enrichment, upsert_cve_scores

logger = logging.getLogger(__name__)

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"


async def _fetch_cvss(client: httpx.AsyncClient, cve_id: str, api_key: str | None) -> dict:
    headers = {"apiKey": api_key} if api_key else {}
    for attempt in range(2):
        try:
            resp = await client.get(
                NVD_URL, params={"cveId": cve_id}, headers=headers, timeout=10
            )
            resp.raise_for_status()
            vulns = resp.json().get("vulnerabilities", [])
            if not vulns:
                return {}
            cve = vulns[0]["cve"]
            for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
                metrics = cve.get("metrics", {}).get(key, [])
                if metrics:
                    cvss = metrics[0]["cvssData"]
                    return {
                        "score": cvss.get("baseScore"),
                        "severity": cvss.get("baseSeverity", cvss.get("baseSeverityV2", "")),
                    }
            return {}
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(10)
            else:
                logger.warning("NVD lookup failed for %s: %s", cve_id, e)
    return {}


async def run_cve_enricher() -> int:
    articles = get_articles_needing_cve_enrichment()
    if not articles:
        return 0

    api_key = os.environ.get("NVD_API_KEY")
    delay = 0.6 if api_key else 6.0

    # Collect unique CVEs across all articles to minimise API calls
    article_cve_map: dict[int, list[str]] = {}
    unique_cves: set[str] = set()
    for a in articles:
        cves = json.loads(a["cves_json"] or "[]")
        if cves:
            article_cve_map[a["id"]] = cves
            unique_cves.update(cves)

    if not unique_cves:
        return 0

    logger.info("CVE enricher: fetching scores for %d unique CVEs", len(unique_cves))

    cache: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for cve_id in sorted(unique_cves):
            cache[cve_id] = await _fetch_cvss(client, cve_id, api_key)
            sev = cache[cve_id].get("severity") or "no score"
            logger.info("CVE %s: %s", cve_id, sev)
            await asyncio.sleep(delay)

    for article_id, cves in article_cve_map.items():
        upsert_cve_scores(article_id, {cve: cache.get(cve, {}) for cve in cves})

    return len(article_cve_map)
