import logging

from app.database import finish_pipeline_run, start_pipeline_run
from app.pipeline.evaluator import run_evaluator
from app.pipeline.fetcher import run_fetcher
from app.pipeline.cve_enricher import run_cve_enricher
from app.pipeline.scorer import run_scorer
from app.pipeline.summarizer import run_summarizer

logger = logging.getLogger(__name__)

_pipeline_running = False
_pipeline_stage = ""


def is_running() -> bool:
    return _pipeline_running


def get_pipeline_stage() -> str:
    return _pipeline_stage


async def run_pipeline() -> dict:
    global _pipeline_running, _pipeline_stage
    if _pipeline_running:
        return {"status": "already_running"}

    _pipeline_running = True
    run_id = start_pipeline_run()
    stats = {"fetched": 0, "accepted": 0, "preliminary": 0, "summarized": 0, "ranked": 0, "errors": []}

    try:
        _pipeline_stage = "Fetching feeds..."
        fetched, skipped = await run_fetcher()
        stats["fetched"] = fetched
        logger.info("Fetch: %d new, %d skipped", fetched, skipped)

        _pipeline_stage = f"Evaluating {fetched} articles..."
        accepted, preliminary = await run_evaluator()
        stats["accepted"] = accepted
        stats["preliminary"] = preliminary
        logger.info("Evaluate: %d accepted, %d preliminary", accepted, preliminary)

        _pipeline_stage = f"Summarizing {accepted} articles..."
        summarized = await run_summarizer()
        stats["summarized"] = summarized
        logger.info("Summarize: %d articles summarized", summarized)

        _pipeline_stage = "Scoring..."
        ranked = run_scorer()
        stats["ranked"] = ranked
        logger.info("Score: %d articles ranked", ranked)

        _pipeline_stage = "Enriching CVEs..."
        enriched = await run_cve_enricher()
        logger.info("CVE enricher: %d articles enriched", enriched)

        finish_pipeline_run(run_id, fetched=fetched, processed=ranked)

    except Exception as e:
        logger.error("Pipeline error: %s", e, exc_info=True)
        finish_pipeline_run(run_id, fetched=stats["fetched"], processed=0, status="error")
        stats["errors"].append(str(e))
    finally:
        _pipeline_running = False
        _pipeline_stage = ""

    return stats
