"""Local SQLite cache: thread_id -> compact thread summary.

The summary is a 1-2 sentence distillation of what happened in the thread
(e.g. "Onsite confirmation for June 5, two rounds with engineering").
The outer classification loop reads summaries grouped by company and decides
stage/action — it never re-fetches email bodies or re-summarizes threads.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "thread_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_cache (
    thread_id    TEXT PRIMARY KEY,
    company_name TEXT,
    summary      TEXT NOT NULL,
    date         TEXT,
    cached_at    TEXT DEFAULT (datetime('now'))
);
"""


@dataclass
class ThreadSummary:
    thread_id: str
    company_name: str
    summary: str
    date: str


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)


def get(thread_id: str) -> Optional[ThreadSummary]:
    """Return cached summary or None if this thread hasn't been seen before."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM thread_cache WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    if row is None:
        return None
    return ThreadSummary(
        thread_id=row["thread_id"],
        company_name=row["company_name"] or "",
        summary=row["summary"],
        date=row["date"] or "",
    )


def put(summary: ThreadSummary) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT INTO thread_cache (thread_id, company_name, summary, date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                company_name = excluded.company_name,
                summary      = excluded.summary,
                date         = excluded.date,
                cached_at    = datetime('now')
            """,
            (summary.thread_id, summary.company_name, summary.summary, summary.date),
        )


def get_all() -> list[ThreadSummary]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM thread_cache ORDER BY date ASC"
        ).fetchall()
    return [
        ThreadSummary(
            thread_id=row["thread_id"],
            company_name=row["company_name"] or "",
            summary=row["summary"],
            date=row["date"] or "",
        )
        for row in rows
    ]


def stats() -> dict:
    with _conn() as con:
        total = con.execute("SELECT COUNT(*) FROM thread_cache").fetchone()[0]
        companies = con.execute(
            "SELECT COUNT(DISTINCT company_name) FROM thread_cache WHERE company_name != ''"
        ).fetchone()[0]
    return {"cached_threads": total, "companies": companies}
