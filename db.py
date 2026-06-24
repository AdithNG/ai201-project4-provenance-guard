"""SQLite storage for Provenance Guard.

Two tables:
  content    one row per submission, tracks current status (classified / under_review)
  audit_log  one row per event (classification or appeal), the canonical record
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
            text         TEXT NOT NULL,
            attribution  TEXT,
            confidence   REAL,
            llm_score    REAL,
            stylo_score  REAL,
            status       TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id   TEXT NOT NULL,
            creator_id   TEXT,
            event_type   TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            attribution  TEXT,
            confidence   REAL,
            llm_score    REAL,
            stylo_score  REAL,
            status       TEXT,
            detail       TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def save_classification(record):
    """Persist a new submission and write its classification audit entry.

    record keys: content_id, creator_id, text, attribution, confidence,
                 llm_score, stylo_score, status, rationale
    """
    ts = now_iso()
    conn = _connect()
    conn.execute(
        """INSERT INTO content
           (content_id, creator_id, text, attribution, confidence,
            llm_score, stylo_score, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["content_id"],
            record["creator_id"],
            record["text"],
            record.get("attribution"),
            record.get("confidence"),
            record.get("llm_score"),
            record.get("stylo_score"),
            record["status"],
            ts,
        ),
    )
    conn.execute(
        """INSERT INTO audit_log
           (content_id, creator_id, event_type, timestamp, attribution,
            confidence, llm_score, stylo_score, status, detail)
           VALUES (?, ?, 'classification', ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["content_id"],
            record["creator_id"],
            ts,
            record.get("attribution"),
            record.get("confidence"),
            record.get("llm_score"),
            record.get("stylo_score"),
            record["status"],
            json.dumps(
                {
                    "rationale": record.get("rationale"),
                    "stylo_details": record.get("stylo_details"),
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
           (content_id, creator_id, event_type, timestamp, attribution,
            confidence, llm_score, stylo_score, status, detail)
           VALUES (?, ?, 'appeal', ?, ?, ?, ?, ?, 'under_review', ?)""",
        (
            content_id,
            content["creator_id"],
            ts,
            content["attribution"],
            content["confidence"],
            content["llm_score"],
            content["stylo_score"],
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
