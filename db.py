"""SQLite storage for Provenance Guard.

Tables:
  content       one row per submission, tracks current status (classified / under_review)
  audit_log     one row per event (classification or appeal), the canonical record
  certificates  one row per Verified Human Creator credential (stretch)
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "provenance.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS content (
            content_id   TEXT PRIMARY KEY,
            creator_id   TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'text',
            text         TEXT NOT NULL,
            attribution  TEXT,
            confidence   REAL,
            llm_score    REAL,
            stylo_score  REAL,
            lexical_score REAL,
            status       TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id   TEXT NOT NULL,
            creator_id   TEXT,
            content_type TEXT,
            event_type   TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            attribution  TEXT,
            confidence   REAL,
            llm_score    REAL,
            stylo_score  REAL,
            lexical_score REAL,
            status       TEXT,
            detail       TEXT
        );

        CREATE TABLE IF NOT EXISTS certificates (
            certificate_id TEXT PRIMARY KEY,
            creator_id     TEXT UNIQUE NOT NULL,
            statement      TEXT,
            issued_at      TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def save_classification(record):
    """Persist a new submission and write its classification audit entry."""
    ts = now_iso()
    content_type = record.get("content_type", "text")
    conn = _connect()
    conn.execute(
        """INSERT INTO content
           (content_id, creator_id, content_type, text, attribution, confidence,
            llm_score, stylo_score, lexical_score, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["content_id"],
            record["creator_id"],
            content_type,
            record["text"],
            record.get("attribution"),
            record.get("confidence"),
            record.get("llm_score"),
            record.get("stylo_score"),
            record.get("lexical_score"),
            record["status"],
            ts,
        ),
    )
    conn.execute(
        """INSERT INTO audit_log
           (content_id, creator_id, content_type, event_type, timestamp, attribution,
            confidence, llm_score, stylo_score, lexical_score, status, detail)
           VALUES (?, ?, ?, 'classification', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["content_id"],
            record["creator_id"],
            content_type,
            ts,
            record.get("attribution"),
            record.get("confidence"),
            record.get("llm_score"),
            record.get("stylo_score"),
            record.get("lexical_score"),
            record["status"],
            json.dumps(
                {
                    "rationale": record.get("rationale"),
                    "stylo_details": record.get("stylo_details"),
                    "lexical_details": record.get("lexical_details"),
                    "metadata_details": record.get("metadata_details"),
                    "disagreement": record.get("disagreement"),
                    "verified": record.get("verified"),
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    return ts


def get_content(content_id):
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM content WHERE content_id = ?", (content_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def file_appeal(content_id, creator_reasoning):
    """Flip status to under_review and log the appeal next to the original decision.

    Returns the updated content row, or None if the content_id is unknown.
    """
    content = get_content(content_id)
    if content is None:
        return None

    ts = now_iso()
    conn = _connect()
    conn.execute(
        "UPDATE content SET status = 'under_review' WHERE content_id = ?",
        (content_id,),
    )
    conn.execute(
        """INSERT INTO audit_log
           (content_id, creator_id, content_type, event_type, timestamp, attribution,
            confidence, llm_score, stylo_score, lexical_score, status, detail)
           VALUES (?, ?, ?, 'appeal', ?, ?, ?, ?, ?, ?, 'under_review', ?)""",
        (
            content_id,
            content["creator_id"],
            content["content_type"],
            ts,
            content["attribution"],
            content["confidence"],
            content["llm_score"],
            content["stylo_score"],
            content["lexical_score"],
            json.dumps({"appeal_reasoning": creator_reasoning}),
        ),
    )
    conn.commit()
    conn.close()
    return get_content(content_id)


def get_log(limit=50):
    """Return recent audit entries, newest first, as plain dicts."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()

    entries = []
    for row in rows:
        entry = dict(row)
        detail = json.loads(entry.pop("detail") or "{}")
        entry.update({k: v for k, v in detail.items() if v is not None})
        entries.append(entry)
    return entries


# --- Provenance certificate (stretch) ----------------------------------------

def grant_certificate(certificate_id, creator_id, statement):
    """Store a Verified Human Creator credential. Idempotent per creator_id."""
    ts = now_iso()
    conn = _connect()
    conn.execute(
        """INSERT OR REPLACE INTO certificates
           (certificate_id, creator_id, statement, issued_at)
           VALUES (?, ?, ?, ?)""",
        (certificate_id, creator_id, statement, ts),
    )
    conn.commit()
    conn.close()
    return {"certificate_id": certificate_id, "creator_id": creator_id, "issued_at": ts}


def get_certificate(creator_id):
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM certificates WHERE creator_id = ?", (creator_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# --- Analytics (stretch) -----------------------------------------------------

def get_analytics():
    """Return detection-pattern, appeal-rate, and extra metrics for the dashboard."""
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) AS n FROM content").fetchone()["n"]
    by_attr = {
        r["attribution"]: r["n"]
        for r in conn.execute(
            "SELECT attribution, COUNT(*) AS n FROM content GROUP BY attribution"
        ).fetchall()
    }
    appeals = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'appeal'"
    ).fetchone()["n"]
    avg_conf = conn.execute(
        "SELECT AVG(confidence) AS a FROM content"
    ).fetchone()["a"]
    verified = conn.execute("SELECT COUNT(*) AS n FROM certificates").fetchone()["n"]
    conn.close()

    def ratio(n):
        return round(n / total, 3) if total else 0.0

    likely_ai = by_attr.get("likely_ai", 0)
    uncertain = by_attr.get("uncertain", 0)
    likely_human = by_attr.get("likely_human", 0)

    return {
        "total_submissions": total,
        "detection_pattern": {
            "likely_ai": {"count": likely_ai, "ratio": ratio(likely_ai)},
            "uncertain": {"count": uncertain, "ratio": ratio(uncertain)},
            "likely_human": {"count": likely_human, "ratio": ratio(likely_human)},
        },
        "appeal_rate": ratio(appeals),
        "appeals": appeals,
        "average_confidence": round(avg_conf, 3) if avg_conf is not None else None,
        "verified_creators": verified,
    }
