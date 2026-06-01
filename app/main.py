import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.log_buffer import setup as setup_log_buffer, get_lines as get_log_lines

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR.parent / "config" / "sources.yaml"

load_dotenv(BASE_DIR.parent / ".env", override=True)
setup_log_buffer()

from app.database import (
    get_article_state,
    get_articles_by_state,
    get_last_run_time,
    get_preliminary_articles,
    get_ranked_articles,
    get_previously_reviewed,
    get_source_stats,
    get_sources,
    init_db,
    mark_reviewed,
    restore_article,
    search_articles,
    set_article_state,
)
from app.pipeline.runner import get_pipeline_stage, is_running, run_pipeline
from app.pipeline.scorer import rescore_all

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
logger = logging.getLogger(__name__)
_cve_cache: dict[str, dict] = {}
_active_tasks: set[asyncio.Task] = set()


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


async def _auto_refresh_loop():
    while True:
        try:
            interval_hours = _load_config().get("refresh_interval_hours") or 1.0
            await asyncio.sleep(float(interval_hours) * 3600)
            logger.info("Auto-refresh triggered (interval=%.1fh)", interval_hours)
            if not is_running():
                asyncio.create_task(run_pipeline())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Auto-refresh loop error, retrying in 60s: %s", e)
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _background_tasks: set[asyncio.Task] = set()

    # Auto-run only on first ever start, or to resume a previously interrupted run.
    if not get_last_run_time() or get_articles_by_state("fetched"):
        t = asyncio.create_task(run_pipeline())
        _background_tasks.add(t)
        t.add_done_callback(_background_tasks.discard)

    _refresh_task = asyncio.create_task(_auto_refresh_loop())
    _background_tasks.add(_refresh_task)

    yield

    _refresh_task.cancel()
    try:
        await _refresh_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        if origin is not None and not (
            origin.startswith("http://localhost")
            or origin.startswith("http://127.0.0.1")
        ):
            return HTMLResponse("Forbidden", status_code=403)
    return await call_next(request)


_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _safe_url(url: str) -> str:
    """Return url only if scheme is http/https, otherwise '#' to block javascript: execution."""
    return url if urlparse(url).scheme in ("http", "https") else "#"


def _build_cves(cves_json: str, scores_json: str) -> list[dict]:
    cve_ids = json.loads(cves_json) if cves_json else []
    scores = json.loads(scores_json) if scores_json else {}
    enriched = [
        {
            "id": cve,
            "score": (scores.get(cve) or {}).get("score"),
            "severity": (scores.get(cve) or {}).get("severity", ""),
        }
        for cve in cve_ids
    ]
    return sorted(
        enriched,
        key=lambda c: (_SEVERITY_ORDER.get((c["severity"] or "").upper(), 99), -(c["score"] or 0)),
    )


def _build_cards(articles) -> list[dict]:
    cards = []
    for a in articles:
        d = dict(a)
        cards.append({
            "id": d["id"],
            "title": d["title"],
            "url": _safe_url(d["url"]),
            "source_name": d["source_name"],
            "published_at": (d.get("published_at") or "")[:10],
            "summary": d.get("summary_text", ""),
            "tooling": json.loads(d["tooling_json"]) if d.get("tooling_json") else [],
            "actors": json.loads(d["actors_json"]) if d.get("actors_json") else [],
            "cves": _build_cves(d.get("cves_json", "[]"), d.get("cve_scores_json", "")),
            "total_score": d.get("total_score", 0),
        })
    return cards


def _build_preliminary_cards(articles) -> list[dict]:
    cards = []
    for a in articles:
        if a["source_name"] == "Risky Bulletin":
            raw = (a["content_text"] or "").strip()
            summary = (raw[:600].rsplit(" ", 1)[0] + "…" if len(raw) > 600 else raw) if raw else a["summary_text"]
        else:
            summary = a["summary_text"]
        cards.append({
            "id": a["id"],
            "title": a["title"],
            "url": _safe_url(a["url"]),
            "source_name": a["source_name"],
            "published_at": (a["published_at"] or "")[:10],
            "summary": summary,
            "total_score": a["total_score"],
        })
    return cards


def _all_sections(request: Request) -> dict:
    return {
        "request": request,
        "cards": _build_cards(get_ranked_articles()),
        "previously_reviewed_cards": _build_cards(get_previously_reviewed()),
        "preliminary_cards": _build_preliminary_cards(get_preliminary_articles()),
    }




@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = _all_sections(request)
    ctx["last_refresh"] = get_last_run_time() or "Never"
    ctx["pipeline_running"] = is_running()
    ctx["pipeline_stage"] = get_pipeline_stage()
    ctx["sources"] = [dict(s) for s in get_sources()]
    return templates.TemplateResponse("index.html", ctx)


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    cve: str = "",
    actor: str = "",
    tool: str = "",
    sort: str = "",
):
    try:
        source_names = request.query_params.getlist("sources")
        active_cve = cve.strip().upper() if cve.strip() else None
        active_actor = actor.strip() or None
        active_tool = tool.strip() or None
        articles = search_articles(
            q=q.strip(),
            source_names=source_names or None,
            cve=active_cve,
            actor=active_actor,
            tool=active_tool,
            sort=sort or "score",
        )
        cards = _build_cards(articles)
        return templates.TemplateResponse("partials/cards.html", {
            "request": request,
            "cards": cards,
            "active_cve": active_cve,
            "active_actor": active_actor,
            "active_tool": active_tool,
            "is_search": True,
        })
    except Exception as e:
        logger.exception("Search error: %s", e)
        return HTMLResponse("<div class='empty-state'><strong>Search error</strong><p>An error occurred — please try again.</p></div>", status_code=500)


@app.post("/refresh", response_class=HTMLResponse)
async def refresh(request: Request):
    if not is_running():
        t = asyncio.create_task(run_pipeline())
        _active_tasks.add(t)
        t.add_done_callback(_active_tasks.discard)
    return templates.TemplateResponse("partials/refresh_btn.html", {
        "request": request,
        "pipeline_running": True,
        "pipeline_stage": get_pipeline_stage(),
        "last_refresh": get_last_run_time() or "Never",
    })



_FORCE_PROCESSABLE_STATES = {"preliminary_rated", "evaluated_rejected", "fetched"}

@app.post("/force-process/{article_id}", response_class=HTMLResponse)
async def force_process(article_id: int):
    state = get_article_state(article_id)
    if state is None:
        return HTMLResponse("<span class='force-queued'>Not found</span>", status_code=404)
    if state not in _FORCE_PROCESSABLE_STATES:
        return HTMLResponse("<span class='force-queued'>Already processed</span>", status_code=409)
    set_article_state(article_id, "evaluated_accepted")
    if not is_running():
        t = asyncio.create_task(run_pipeline())
        _active_tasks.add(t)
        t.add_done_callback(_active_tasks.discard)
    return HTMLResponse("<span class='force-queued'>Queued ✓</span>")


@app.get("/status", response_class=HTMLResponse)
async def status(request: Request):
    running = is_running()
    articles = get_ranked_articles() if not running else []
    cards = _build_cards(articles) if not running else None
    last_refresh = get_last_run_time()

    if running:
        return templates.TemplateResponse("partials/refresh_btn.html", {
            "request": request,
            "pipeline_running": True,
            "pipeline_stage": get_pipeline_stage(),
            "last_refresh": last_refresh or "Never",
        })

    ctx = _all_sections(request)
    ctx["last_refresh"] = last_refresh or "Never"
    ctx["pipeline_running"] = False
    ctx["pipeline_stage"] = ""
    return templates.TemplateResponse("partials/dashboard_update.html", ctx)


@app.get("/ranked", response_class=HTMLResponse)
async def ranked(request: Request):
    return templates.TemplateResponse("partials/cards.html", {
        "request": request,
        "cards": _build_cards(get_ranked_articles()),
    })


@app.get("/filtered", response_class=HTMLResponse)
async def filtered_view(request: Request):
    articles = get_preliminary_articles()
    rows = []
    for a in articles:
        d = dict(a)
        d["published_at"] = (d.get("published_at") or "")[:10]
        d["url"] = _safe_url(d.get("url") or "")
        raw = (d.get("content_text") or "").strip()
        d["content_text"] = (raw[:600].rsplit(" ", 1)[0] + "…" if len(raw) > 600 else raw)
        rows.append(d)
    return templates.TemplateResponse("partials/filtered.html", {
        "request": request,
        "rows": rows,
    })


@app.post("/rescore", response_class=HTMLResponse)
async def rescore(request: Request):
    count = rescore_all()
    logger.info("Re-scored %d articles", count)
    return _sections_update(request)


def _sections_update(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("partials/sections_update.html", _all_sections(request))


@app.post("/mark-read", response_class=HTMLResponse)
async def mark_read(request: Request, ids: list[int] = Form(default=[])):
    mark_reviewed(ids if ids else None)
    return _sections_update(request)



@app.post("/restore-article/{article_id}", response_class=HTMLResponse)
async def restore_article_route(request: Request, article_id: int):
    restore_article(article_id)
    return _sections_update(request)


@app.get("/cve/{cve_id}", response_class=HTMLResponse)
async def cve_lookup(request: Request, cve_id: str):
    if not CVE_RE.fullmatch(cve_id):
        return HTMLResponse("<span class='cve-error'>Invalid CVE ID</span>", status_code=400)

    cve_id = cve_id.upper()
    if cve_id in _cve_cache:
        return templates.TemplateResponse("partials/cve_detail.html", {
            "request": request, **_cve_cache[cve_id]
        })

    detail = {"cve_id": cve_id, "error": None, "description": "", "severity": "", "score": None, "vector": ""}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://services.nvd.nist.gov/rest/json/cves/2.0",
                params={"cveId": cve_id},
            )
            resp.raise_for_status()
            data = resp.json()

        vulns = data.get("vulnerabilities", [])
        if not vulns:
            detail["error"] = "Not found in NVD"
        else:
            cve = vulns[0]["cve"]
            for d in cve.get("descriptions", []):
                if d["lang"] == "en":
                    detail["description"] = d["value"]
                    break
            for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
                metrics = cve.get("metrics", {}).get(key, [])
                if metrics:
                    cvss = metrics[0]["cvssData"]
                    detail["score"] = cvss.get("baseScore")
                    detail["severity"] = cvss.get("baseSeverity", cvss.get("baseSeverityV2", ""))
                    detail["vector"] = cvss.get("vectorString", "")
                    break
    except Exception as e:
        detail["error"] = f"NVD lookup failed: {e}"

    if not detail["error"]:
        _cve_cache[cve_id] = detail

    return templates.TemplateResponse("partials/cve_detail.html", {
        "request": request,
        **detail,
    })


@app.get("/stats", response_class=HTMLResponse)
async def stats(request: Request):
    rows = get_source_stats()
    return templates.TemplateResponse("stats.html", {
        "request": request,
        "rows": rows,
    })


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "lines": get_log_lines(),
    })


@app.get("/logs/lines", response_class=HTMLResponse)
async def logs_lines(request: Request):
    lines = get_log_lines()
    return templates.TemplateResponse("partials/log_lines.html", {
        "request": request,
        "lines": lines,
    })
