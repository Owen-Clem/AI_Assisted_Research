# AI-Assisted Security Research Dashboard

This project is a locally hosted open-source security research feed that aggregates 30+ security research sources, utilizing a multi-stage AI pipeline to perform triage and summary of articles, content ranking, and display of articles based on tunable prompts.

![Dashboard](https://img.shields.io/badge/stack-FastAPI%20%7C%20HTMX%20%7C%20SQLite-blue)
![AI](https://img.shields.io/badge/AI-Claude%20Haiku%20%2B%20Sonnet-purple)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

---

## Overview

Not every article from a high-quality security source will be relevant to a given researcher's specific focus. This tool lets you tune the evaluator and summarizer prompts to filter content down to what matters to you. Results are ranked by content quality rather than recency alone.

Every article passes through a two-model AI pipeline that scores technical depth and extracts structured data (TTPs, tooling, CVEs, threat actors), then ranks results on a locally hosted dashboard.

---

## How It Works

### Pipeline

Articles move through a state machine with five stages:

```
fetched -> evaluated_rejected | preliminary_rated | evaluated_accepted -> summarized -> ranked
```

**1. Fetcher:** Scrapes RSS feeds from all configured sources based on lookback windows (default 7 days), deduplicates by URL and normalized title.

**2. Evaluator** (Claude Haiku): Fast initial scoring of each article 0-100 based on a user-defined prompt. Only the title and first 1,000 characters are sent. Articles below the acceptance threshold are filed as Low Priority (`preliminary_rated`) and skipped by the summarizer; articles above proceed to full analysis. Articles that fail after all retries are marked `evaluated_rejected`. Runs up to 3 concurrent requests with exponential backoff on rate limits.

**3. Summarizer** (Claude Sonnet): Deep analysis of evaluator-accepted articles. Sends up to 12,000 characters and returns structured JSON with a 3-5 sentence technique breakdown, tooling list with purpose, named threat actors, CVE IDs, and a `pt_relevance` score (0.0-1.0) measuring how much the article relates to the user-provided parameters.

**4. Scorer:** Combines the three signals below into a final 0-100 ranking score.

| Signal | Weight | Method |
|---|---|---|
| `pt_relevance` | configurable | Sonnet's quality judgment |
| Recency | configurable | Exponential decay, configurable half-life |
| Source reputation | configurable | Configurable per-source rating / 5 |

**5. CVE Enricher:** Fetches CVSS base scores and severity ratings from the NVD API for any CVEs the summarizer extracted. Deduplicates CVE lookups across articles to minimize API calls.

### Idempotency

Each stage queries only articles in its specific input state. If the server restarts mid-run, the next startup detects articles stuck in intermediate states and resumes from exactly where processing stopped.

### Auto-Refresh

A background loop reads `refresh_interval_hours` from `sources.yaml` on each iteration and fires the pipeline on schedule -- no cron job or external scheduler required.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI (async) |
| Database | SQLite - raw SQL, no ORM |
| Templates | Jinja2 |
| Frontend | HTMX - reactive UI with zero JS framework |
| AI | Anthropic Claude (Haiku for triage, Sonnet for analysis) |
| HTTP client | httpx (async) |
| Feed parsing | feedparser + BeautifulSoup4 |

---

## Features

- **40 curated sources** across threat intelligence, offensive research, APT/malware analysis, web/cloud attack research, and CVE exploitation
- **Cost-optimized AI usage:** cheap Haiku filters ~59% of articles before Sonnet is invoked (mileage may vary depending on user prompts)
- **Structured extraction:** every ranked article includes a summary, tooling breakdown, threat actor tags, and CVE tags with inline NVD detail on click
- **Search and filter:** full-text search across ranked articles with source, CVE, actor, and tool filters
- **Mark as read:** select individual cards or bulk-archive the feed; reviewed articles collapse into a separate section
- **Force-promote:** manually push a Low Priority article through the full summarization pipeline
- **Live logs:** in-process log buffer accessible at `/logs` without touching the filesystem
- **Stats page:** per-source hit rate, ranked article count, and average score

---

## Getting Started

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)
- Optional: [NVD API key](https://nvd.nist.gov/developers/request-an-api-key) for higher CVE lookup rate limits

### Setup

```bash
# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate.bat        # Windows
source .venv/bin/activate          # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Configure API keys
copy .env.example .env             # Windows
cp .env.example .env               # macOS/Linux
# Edit .env and add your ANTHROPIC_API_KEY and tune your prompts

# Run the server (localhost only -- do not change the host binding)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open [http://localhost:8000](http://localhost:8000). The pipeline runs automatically on first start.

### Reset

```bash
del research.db      # Windows
rm research.db       # macOS/Linux
```

Deleting the database wipes all articles and run history. The pipeline re-runs from scratch on next startup.

---

## Configuration

### Sources: `config/sources.yaml`

```yaml
lookback_days: 7
refresh_interval_hours: 1

sources:
  - name: watchTowr Labs
    url: https://labs.watchtowr.com/rss/
    type: rss
    reputation: 5
```

- `lookback_days`: how far back to fetch articles on each run
- `refresh_interval_hours`: how often the pipeline auto-runs in the background
- `reputation`: 1-5 score contributing to the final article ranking (weight configurable in `.env`)

Add any RSS feed by adding an entry matching the existing schema -- no code changes required.

### Environment: `.env`

All content scoring and prompt configuration is read from `.env`. Copy the provided `.env.example` to get started. It covers API keys, evaluator and summarizer prompts, the acceptance threshold, scorer weights, and recency decay.

---

## Security Notes

- Server binds to `127.0.0.1` only -- not accessible on the network
- All article content is HTML-stripped before being sent to Claude
- Jinja2 autoescaping is enabled -- external content is never rendered as raw HTML
- `.env` is gitignored -- API keys are never committed

---

## Project Structure

```
app/
    main.py                  # FastAPI app, routes, lifespan
    database.py              # All SQLite access
    log_buffer.py            # In-memory log ring buffer
    pipeline/
        runner.py            # Pipeline orchestration
        fetcher.py           # RSS ingestion
        evaluator.py         # Haiku triage stage
        summarizer.py        # Sonnet analysis stage
        scorer.py            # Local scoring
        cve_enricher.py      # NVD CVE enrichment
    scrapers/
        rss.py               # RSS/Atom feed parser
    static/                  # CSS, HTMX
    templates/               # Jinja2 templates + partials
config/
    sources.yaml             # Source list and pipeline config
```
