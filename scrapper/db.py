"""SQLite storage. Everything is resumable: kill the process, rerun, it continues."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS domains (
    domain        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'site',   -- site | directory
    discovered_by TEXT,
    score         REAL,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending|accepted|rejected|error
    note          TEXT,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    url         TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    priority    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|done|skipped|error
    http_status INTEGER,
    score       REAL,
    title       TEXT,
    error       TEXT,
    fetched_at  INTEGER
);
CREATE INDEX IF NOT EXISTS pages_pending ON pages(status, priority DESC, depth);
CREATE INDEX IF NOT EXISTS pages_domain  ON pages(domain, status);

CREATE TABLE IF NOT EXISTS emails (
    email       TEXT PRIMARY KEY,
    local_part  TEXT,
    email_domain TEXT,
    site_domain TEXT,
    kind        TEXT,          -- personal | role | unknown
    name        TEXT,
    context     TEXT,
    source_url  TEXT,
    page_title  TEXT,
    score       REAL,
    hits        INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS emails_site ON emails(site_domain);
CREATE INDEX IF NOT EXISTS emails_score ON emails(score DESC);
"""


class Store:
    def __init__(self, path: str | Path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---------- domains ----------

    def add_domain(self, domain: str, kind: str = "site", discovered_by: str = "") -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO domains(domain, kind, discovered_by, created_at) "
            "VALUES (?,?,?,?)",
            (domain, kind, discovered_by, int(time.time())),
        )
        return cur.rowcount > 0

    def set_domain_status(self, domain: str, status: str, score: float | None = None,
                          note: str = "") -> None:
        self.conn.execute(
            "UPDATE domains SET status=?, score=COALESCE(?, score), note=? WHERE domain=?",
            (status, score, note, domain),
        )

    def domain_status(self, domain: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM domains WHERE domain=?", (domain,)
        ).fetchone()
        return row["status"] if row else None

    def domain_kind(self, domain: str) -> str | None:
        row = self.conn.execute(
            "SELECT kind FROM domains WHERE domain=?", (domain,)
        ).fetchone()
        return row["kind"] if row else None

    def count_domains(self, status: str | None = None) -> int:
        if status:
            q = "SELECT COUNT(*) c FROM domains WHERE status=?"
            return self.conn.execute(q, (status,)).fetchone()["c"]
        return self.conn.execute("SELECT COUNT(*) c FROM domains").fetchone()["c"]

    # ---------- pages ----------

    def add_page(self, url: str, domain: str, depth: int, priority: int = 0) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO pages(url, domain, depth, priority) VALUES (?,?,?,?)",
            (url, domain, depth, priority),
        )
        return cur.rowcount > 0

    def next_pending(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pages WHERE status='pending' "
            "ORDER BY priority DESC, depth ASC, rowid ASC LIMIT ?",
            (limit,),
        ).fetchall()

    def finish_page(self, url: str, status: str, http_status: int | None = None,
                    score: float | None = None, title: str = "", error: str = "") -> None:
        self.conn.execute(
            "UPDATE pages SET status=?, http_status=?, score=?, title=?, error=?, fetched_at=? "
            "WHERE url=?",
            (status, http_status, score, title[:300], error[:300], int(time.time()), url),
        )

    def pages_done_for_domain(self, domain: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE domain=? AND status IN ('done','error')",
            (domain,),
        ).fetchone()["c"]

    def count_pages(self, status: str | None = None) -> int:
        if status:
            q = "SELECT COUNT(*) c FROM pages WHERE status=?"
            return self.conn.execute(q, (status,)).fetchone()["c"]
        return self.conn.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]

    def drop_pending_for_domain(self, domain: str) -> None:
        self.conn.execute(
            "UPDATE pages SET status='skipped' WHERE domain=? AND status='pending'", (domain,)
        )

    # ---------- emails ----------

    def add_email(self, rec: dict) -> bool:
        """Insert, or bump the hit count and keep the best-scoring provenance."""
        cur = self.conn.execute("SELECT email, score FROM emails WHERE email=?", (rec["email"],))
        existing = cur.fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO emails(email, local_part, email_domain, site_domain, kind, name,"
                " context, source_url, page_title, score, hits, created_at)"
                " VALUES (:email,:local_part,:email_domain,:site_domain,:kind,:name,:context,"
                ":source_url,:page_title,:score,1,:created_at)",
                {**rec, "created_at": int(time.time())},
            )
            return True
        if rec["score"] > (existing["score"] or 0):
            self.conn.execute(
                "UPDATE emails SET hits=hits+1, score=:score, name=COALESCE(:name, name),"
                " context=:context, source_url=:source_url, page_title=:page_title,"
                " site_domain=:site_domain WHERE email=:email",
                rec,
            )
        else:
            self.conn.execute(
                "UPDATE emails SET hits=hits+1, name=COALESCE(name, :name) WHERE email=:email",
                rec,
            )
        return False

    def count_emails(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM emails").fetchone()["c"]

    def commit(self) -> None:
        self.conn.commit()
