# AI-Assisted Security Research Dashboard

This project is a locally hosted open-source security research feed that aggregates 30+ security research source. Utilizing a multi-stage AI pipline to perform triage and summary of articles, content ranking, and display of articles based on tunable prompts

![Dashboard](https://img.shields.io/badge/stack-FastAPI%20%7C%20HTMX%20%7C%20SQLite-blue)
![AI](https://img.shields.io/badge/AI-Claude%20Haiku%20%2B%20Sonnet-purple)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

---

## Overview

There are a great number of articles containg all sorts of information, while all of the sources in the sources.yaml provide generally high quallity reporting and research not all of it is going to be applicable to ones specific interest in needs. The intention of this project is that you can tune the parameters of the ranking system to display on the dashboard aritcles which are more relevant to your personal/professional interests.

This tool provides automated triage as every article passes through a two-model AI pipeline that scores technical depth and extracts structured data (TTPs, tooling, CVEs, threat actors), then ranks results by content quality as it relates to user provided parameters rather than recency alone. The output is dispayed via a locally hosted dashboard in score order.

---

## How It Works

### Pipeline

Articles move through a state machine with five stages:

```
fetched → evaluated_rejected | preliminary_rated | evaluated_accepted → summarized → ranked
```

**1. Fetcher** — Scrapes RSS feeds from all configured sources. Deduplicates by URL and normalized title. Respects a configurable lookback window (default 7 days).

**2. Evaluator** (Claude Haiku) — Cheap, fast triage. Scores each article 0–100 on technical depth using only the title and first 1,000 characters. Articles below threshold go to Low Priority; articles above proceed to full summarization. Runs up to 3 concurrent requests with exponential backoff on rate limits.

**3. Summarizer** (Claude Sonnet) — Deep analysis on accepted articles. Sends up to 12,000 characters and returns structured JSON: a 3–5 sentence technique breakdown, tooling list with purpose, named threat actors, CVE IDs, and a `pt_relevance` score (0.0–1.0) measuring how much the article advances understanding of attack mechanics.

**4. Scorer** — Local math, no API calls. Combines three signals into a final 0–100 score:

| Signal | Weight | Method |
|---|---|---|
| `pt_relevance` | 65% | Sonnet's quality judgment |
| Recency | 15% | Exponential decay, 14-day half-life |
| Source reputation | 20% | Configurable per-source rating ÷ 5 |

**5. CVE Enricher** — Fetches CVSS base scores and severity ratings from the NVD API for any CVEs the summarizer extracted. Deduplicates CVE lookups across articles to minimize API calls.

### Idempotency

Each stage queries only articles in its specific input state. If the server restarts mid-run, the next startup detects articles stuck in intermediate states and resumes from exactly where processing stopped.

### Auto-Refresh

A background loop reads `refresh_interval_hours` from `sources.yaml` on each iteration and fires the pipeline on schedule — no cron job or external scheduler required.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI (async) |
| Database | SQLite — raw SQL, no ORM |
| Templates | Jinja2 |
| Frontend | HTMX — reactive UI with zero JS framework |
| AI | Anthropic Claude (Haiku for triage, Sonnet for analysis) |
| HTTP client | httpx (async) |
| Feed parsing | feedparser + BeautifulSoup4 |

---

## Features

- **30+ curated sources** across threat intelligence, offensive research, APT/malware analysis, web/cloud attack research, and CVE exploitation — organized and reputation-rated in a single YAML file
- **Cost-optimized AI usage** — cheap Haiku filters ~60% of articles before Sonnet is invoked
- **Structured extraction** — every ranked article includes a summary, tooling breakdown, threat actor tags, and CVE tags with inline NVD detail on click
- **Search and filter** — full-text search across ranked articles with source, CVE, actor, and tool filters
- **Mark as read** — select individual cards or bulk-archive the feed; reviewed articles collapse into a separate section
- **Force-promote** — manually push a Low Priority article through the full summarization pipeline
- **Live logs** — in-process log buffer accessible at `/logs` without touching the filesystem
- **Stats page** — per-source hit rate, ranked article count, and average score

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
# Edit .env and add your ANTHROPIC_API_KEY as well as tune your prompts 

# Run the server
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

### Sources — `config/sources.yaml`

```yaml
lookback_days: 7
refresh_interval_hours: 1

sources:
  - name: watchTowr Labs
    url: https://labs.watchtowr.com/rss/
    type: rss
    reputation: 5
```

- `lookback_days` — how far back to fetch articles on each run
- `refresh_interval_hours` — how often the pipeline auto-runs in the background
- `reputation` — 1–5 score contributing 20% of the final article ranking

Add any RSS feed by adding an entry. No code changes required.

### Environment — `.env`

```
ANTHROPIC_API_KEY=your_key_here
NVD_API_KEY=your_key_here          # optional; increases NVD rate limit from 5 to 50 req/30s
```

---

## Security Notes

- Server binds to `127.0.0.1` only — not accessible on the network
- All article content is HTML-stripped before being sent to Claude
- Jinja2 autoescaping is enabled — external content is never rendered as raw HTML
- `.env` is gitignored — API keys are never committed

---

## Project Structure

```
├── app/
│   ├── main.py                  # FastAPI app, routes, lifespan
│   ├── database.py              # All SQLite access
│   ├── log_buffer.py            # In-memory log ring buffer
│   ├── pipeline/
│   │   ├── runner.py            # Pipeline orchestration
│   │   ├── fetcher.py           # RSS ingestion
│   │   ├── evaluator.py         # Haiku triage stage
│   │   ├── summarizer.py        # Sonnet analysis stage
│   │   ├── scorer.py            # Local scoring
│   │   └── cve_enricher.py      # NVD CVE enrichment
│   ├── scrapers/
│   │   └── rss.py               # RSS/Atom feed parser
│   ├── static/                  # CSS, HTMX
│   └── templates/               # Jinja2 templates + partials
└── config/
    └── sources.yaml             # Source list and pipeline config
```
