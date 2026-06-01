import logging
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

DB_PATH = Path(__file__).parent.parent / "research.db"
logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                feed_type TEXT NOT NULL,
                reputation INTEGER NOT NULL CHECK(reputation BETWEEN 1 AND 5)
            );

            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id),
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                published_at TEXT,
                content_text TEXT,
                fetched_at TEXT NOT NULL,
                pipeline_state TEXT NOT NULL DEFAULT 'fetched',
                review_attempts INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id),
                summary_text TEXT NOT NULL,
                tooling_json TEXT NOT NULL DEFAULT '[]',
                actors_json TEXT NOT NULL DEFAULT '[]',
                cves_json TEXT NOT NULL DEFAULT '[]',
                pt_relevance REAL NOT NULL DEFAULT 0.5,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cross_references (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL REFERENCES articles(id),
                related_article_id INTEGER NOT NULL REFERENCES articles(id),
                relationship_type TEXT NOT NULL,
                UNIQUE(article_id, related_article_id, relationship_type)
            );

            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id),
                recency_score REAL NOT NULL DEFAULT 0,
                reputation_score REAL NOT NULL DEFAULT 0,
                pt_relevance_score REAL NOT NULL DEFAULT 0,
                total_score REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                articles_fetched INTEGER NOT NULL DEFAULT 0,
                articles_processed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running'
            );

            CREATE INDEX IF NOT EXISTS idx_articles_pipeline_state ON articles(pipeline_state);
            CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
            CREATE INDEX IF NOT EXISTS idx_scores_total ON scores(total_score DESC);
        """)
        # Migrate existing DBs — ignore errors if columns already exist
        for table, col, defn in [
            ("summaries", "actors_json",    "TEXT NOT NULL DEFAULT '[]'"),
            ("summaries", "cves_json",      "TEXT NOT NULL DEFAULT '[]'"),
            ("summaries", "pt_relevance",   "REAL NOT NULL DEFAULT 0.5"),
            ("articles",  "user_reviewed",  "INTEGER NOT NULL DEFAULT 0"),
            ("articles",  "is_favorite",    "INTEGER NOT NULL DEFAULT 0"),
            ("articles",  "in_reading_list","INTEGER NOT NULL DEFAULT 0"),
            ("summaries", "cve_scores_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            except Exception as e:
                logger.debug("Migration skip (%s.%s): %s", table, col, e)


def upsert_source(name: str, url: str, feed_type: str, reputation: int) -> int:
    with db() as conn:
        conn.execute(
            """INSERT INTO sources (name, url, feed_type, reputation)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                 name=excluded.name,
                 feed_type=excluded.feed_type,
                 reputation=excluded.reputation""",
            (name, url, feed_type, reputation),
        )
        row = conn.execute("SELECT id FROM sources WHERE url=?", (url,)).fetchone()
        return row["id"]


def _normalize_url(url: str) -> str:
    """Strip query string and fragment so utm_* variants match the canonical URL."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", "")).lower()


def article_exists(url: str) -> bool:
    canonical = _normalize_url(url)
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE url=? OR url=?", (url, canonical)
        ).fetchone()
        return row is not None


def title_already_processed(title: str) -> bool:
    """Return True if an article with the same normalized title was already evaluated/processed."""
    normalized = " ".join(title.lower().split())
    with db() as conn:
        row = conn.execute(
            """SELECT 1 FROM articles
               WHERE lower(trim(title)) = ?
               AND pipeline_state != 'fetched'
               LIMIT 1""",
            (normalized,),
        ).fetchone()
        return row is not None


def insert_article(source_id: int, url: str, title: str, published_at: str, content_text: str, fetched_at: str) -> int:
    canonical = _normalize_url(url)
    title = " ".join(title.split())  # normalize whitespace so title_already_processed SQL matches
    with db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (source_id, url, title, published_at, content_text, fetched_at, pipeline_state)
               VALUES (?, ?, ?, ?, ?, ?, 'fetched')""",
            (source_id, canonical, title, published_at, content_text, fetched_at),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM articles WHERE url=?", (canonical,)).fetchone()
        return row["id"]



def get_articles_by_state(state: str) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT a.*, s.reputation FROM articles a JOIN sources s ON a.source_id=s.id WHERE a.pipeline_state=?",
            (state,),
        ).fetchall()


def get_article_state(article_id: int) -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT pipeline_state FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        return row["pipeline_state"] if row else None


def set_article_state(article_id: int, state: str):
    with db() as conn:
        conn.execute("UPDATE articles SET pipeline_state=? WHERE id=?", (state, article_id))


def increment_review_attempts(article_id: int) -> int:
    with db() as conn:
        conn.execute("UPDATE articles SET review_attempts=review_attempts+1 WHERE id=?", (article_id,))
        row = conn.execute("SELECT review_attempts FROM articles WHERE id=?", (article_id,)).fetchone()
        return row["review_attempts"]


def upsert_summary(
    article_id: int,
    summary_text: str,
    tooling: list,
    created_at: str,
    actors: list | None = None,
    cves: list | None = None,
    pt_relevance: float = 0.5,
):
    with db() as conn:
        conn.execute(
            """INSERT INTO summaries
                 (article_id, summary_text, tooling_json, actors_json, cves_json, pt_relevance, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(article_id) DO UPDATE SET
                 summary_text=excluded.summary_text,
                 tooling_json=excluded.tooling_json,
                 actors_json=excluded.actors_json,
                 cves_json=excluded.cves_json,
                 pt_relevance=excluded.pt_relevance,
                 created_at=excluded.created_at""",
            (article_id, summary_text, json.dumps(tooling), json.dumps(actors or []),
             json.dumps(cves or []), pt_relevance, created_at),
        )


def get_summary(article_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM summaries WHERE article_id=?", (article_id,)).fetchone()



def upsert_score(article_id: int, recency: float, reputation: float, pt_relevance: float, total: float):
    with db() as conn:
        conn.execute(
            """INSERT INTO scores (article_id, recency_score, reputation_score, pt_relevance_score, total_score)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(article_id) DO UPDATE SET
                 recency_score=excluded.recency_score,
                 reputation_score=excluded.reputation_score,
                 pt_relevance_score=excluded.pt_relevance_score,
                 total_score=excluded.total_score""",
            (article_id, recency, reputation, pt_relevance, total),
        )


def get_sources() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT id, name FROM sources ORDER BY name"
        ).fetchall()


def search_articles(
    q: str = "",
    source_names: list[str] | None = None,
    cve: str | None = None,
    actor: str | None = None,
    tool: str | None = None,
    sort: str = "score",
    limit: int = 50,
) -> list[sqlite3.Row]:
    conditions: list[str] = ["a.pipeline_state = 'ranked'"]
    params: list = []

    if q:
        conditions.append(
            "(a.title LIKE ? OR COALESCE(sm.summary_text,'') LIKE ? OR COALESCE(a.content_text,'') LIKE ?)"
        )
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    if source_names:
        placeholders = ",".join("?" * len(source_names))
        conditions.append(f"s.name IN ({placeholders})")
        params.extend(source_names)

    if cve:
        conditions.append("COALESCE(sm.cves_json,'[]') LIKE ?")
        params.append(f'%"{cve}"%')

    if actor:
        conditions.append("COALESCE(sm.actors_json,'[]') LIKE ?")
        params.append(f'%{actor}%')

    if tool:
        conditions.append("COALESCE(sm.tooling_json,'[]') LIKE ?")
        params.append(f'%{tool}%')

    _SORT_MAP = {
        "date": "a.published_at DESC",
        "score": "COALESCE(sc.total_score, 0) DESC",
    }
    where = "WHERE " + " AND ".join(conditions)
    order = _SORT_MAP.get(sort, _SORT_MAP["score"])
    params.append(limit)

    with db() as conn:
        return conn.execute(
            f"""SELECT a.id, a.url, a.title, a.published_at, a.source_id,
                      a.pipeline_state,
                      s.name AS source_name, s.reputation,
                      COALESCE(sm.summary_text, '') AS summary_text,
                      COALESCE(sm.tooling_json, '[]') AS tooling_json,
                      COALESCE(sm.actors_json, '[]') AS actors_json,
                      COALESCE(sm.cves_json, '[]') AS cves_json,
                      COALESCE(sm.cve_scores_json, '') AS cve_scores_json,
                      COALESCE(sc.total_score, 0) AS total_score,
                      COALESCE(sc.recency_score, 0) AS recency_score,
                      COALESCE(sc.reputation_score, 0) AS reputation_score,
                      COALESCE(sc.pt_relevance_score, 0) AS pt_relevance_score
               FROM articles a
               JOIN sources s ON a.source_id = s.id
               LEFT JOIN summaries sm ON sm.article_id = a.id
               LEFT JOIN scores sc ON sc.article_id = a.id
               {where}
               ORDER BY {order}
               LIMIT ?""",
            params,
        ).fetchall()


def get_preliminary_articles(limit: int = 100) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """SELECT a.id, a.url, a.title, a.published_at,
                      a.content_text,
                      s.name AS source_name,
                      COALESCE(sm.summary_text, '') AS summary_text,
                      COALESCE(sc.total_score, 0) AS total_score
               FROM articles a
               JOIN sources s ON a.source_id = s.id
               LEFT JOIN summaries sm ON sm.article_id = a.id
               LEFT JOIN scores sc ON sc.article_id = a.id
               WHERE a.pipeline_state IN ('preliminary_rated', 'evaluated_rejected')
               ORDER BY sc.total_score DESC, a.published_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


_RANKED_SELECT = """SELECT a.id, a.url, a.title, a.published_at, a.source_id,
                      s.name AS source_name, s.reputation,
                      sm.summary_text, sm.tooling_json, sm.actors_json, sm.cves_json,
                      sm.cve_scores_json,
                      sc.total_score, sc.recency_score, sc.reputation_score,
                      sc.pt_relevance_score
               FROM articles a
               JOIN sources s ON a.source_id = s.id
               JOIN summaries sm ON sm.article_id = a.id
               JOIN scores sc ON sc.article_id = a.id"""


def get_ranked_articles(limit: int = 50) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            f"{_RANKED_SELECT} WHERE a.pipeline_state='ranked' AND a.user_reviewed=0"
            " ORDER BY sc.total_score DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_previously_reviewed(limit: int = 100) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            f"{_RANKED_SELECT} WHERE a.pipeline_state='ranked' AND a.user_reviewed=1"
            " ORDER BY sc.total_score DESC LIMIT ?",
            (limit,),
        ).fetchall()



def mark_reviewed(article_ids: list[int] | None = None):
    with db() as conn:
        if article_ids:
            placeholders = ",".join("?" * len(article_ids))
            conn.execute(
                f"UPDATE articles SET user_reviewed=1 WHERE id IN ({placeholders})",
                article_ids,
            )
        else:
            conn.execute(
                "UPDATE articles SET user_reviewed=1 WHERE pipeline_state='ranked' AND user_reviewed=0"
            )


def restore_article(article_id: int):
    with db() as conn:
        conn.execute("UPDATE articles SET user_reviewed=0 WHERE id=?", (article_id,))




def get_articles_needing_cve_enrichment() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """SELECT a.id, sm.cves_json FROM articles a
               JOIN summaries sm ON sm.article_id = a.id
               WHERE a.pipeline_state = 'ranked'
               AND sm.cves_json != '[]'
               AND (sm.cve_scores_json = '{}' OR sm.cve_scores_json = '')"""
        ).fetchall()


def upsert_cve_scores(article_id: int, scores: dict):
    with db() as conn:
        conn.execute(
            "UPDATE summaries SET cve_scores_json=? WHERE article_id=?",
            (json.dumps(scores), article_id),
        )


def start_pipeline_run() -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (started_at, status) VALUES (datetime('now'), 'running')"
        )
        return cur.lastrowid


def finish_pipeline_run(run_id: int, fetched: int, processed: int, status: str = "completed"):
    with db() as conn:
        conn.execute(
            """UPDATE pipeline_runs
               SET completed_at=datetime('now'), articles_fetched=?, articles_processed=?, status=?
               WHERE id=?""",
            (fetched, processed, status, run_id),
        )


def get_last_run_time() -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT completed_at FROM pipeline_runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["completed_at"] if row else None


def get_source_stats() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT
                 s.name, s.reputation,
                 COUNT(a.id) AS total,
                 SUM(CASE WHEN a.pipeline_state = 'ranked' THEN 1 ELSE 0 END) AS ranked,
                 SUM(CASE WHEN a.pipeline_state IN ('preliminary_rated','evaluated_rejected') THEN 1 ELSE 0 END) AS filtered,
                 SUM(CASE WHEN a.pipeline_state = 'review_failed' THEN 1 ELSE 0 END) AS failed,
                 ROUND(AVG(CASE WHEN a.pipeline_state = 'ranked' THEN sc.total_score END), 1) AS avg_score
               FROM sources s
               LEFT JOIN articles a ON a.source_id = s.id
               LEFT JOIN scores sc ON sc.article_id = a.id
               GROUP BY s.id
               ORDER BY avg_score DESC NULLS LAST, ranked DESC""",
        ).fetchall()
        return [dict(r) for r in rows]
