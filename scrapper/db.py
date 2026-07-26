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

-- Outreach. One row per (campaign, recipient); the send loop is resumable the
-- same way the crawl is. Suppressions live in a separate file (see suppress.py).
CREATE TABLE IF NOT EXISTS messages (
    campaign    TEXT NOT NULL,
    email       TEXT NOT NULL,
    name        TEXT,
    site_domain TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|sent|failed|skipped
    subject     TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    queued_at   INTEGER NOT NULL,
    sent_at     INTEGER,
    PRIMARY KEY (campaign, email)
);
CREATE INDEX IF NOT EXISTS messages_queue ON messages(campaign, status);
CREATE INDEX IF NOT EXISTS messages_sent  ON messages(sent_at);
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

    def domain_score(self, domain: str) -> float:
        row = self.conn.execute(
            "SELECT score FROM domains WHERE domain=?", (domain,)
        ).fetchone()
        return (row["score"] or 0.0) if row else 0.0

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

    # ---------- outreach ----------

    def queue_message(self, campaign: str, email: str, name: str = "",
                      site_domain: str = "") -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages(campaign, email, name, site_domain, queued_at)"
            " VALUES (?,?,?,?,?)",
            (campaign, email, name, site_domain, int(time.time())),
        )
        return cur.rowcount > 0

    def pending_messages(self, campaign: str, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE campaign=? AND status='pending'"
            " ORDER BY queued_at, rowid LIMIT ?",
            (campaign, limit),
        ).fetchall()

    def finish_message(self, campaign: str, email: str, status: str,
                       subject: str = "", error: str = "") -> None:
        self.conn.execute(
            "UPDATE messages SET status=?, subject=?, error=?, attempts=attempts+1,"
            " sent_at=? WHERE campaign=? AND email=?",
            (status, subject[:300], error[:300],
             int(time.time()) if status == "sent" else None, campaign, email),
        )

    def sent_since(self, since: int, campaign: str | None = None) -> int:
        """Sends across all campaigns by default - throttles protect the sending
        domain's reputation, which is shared, not per-campaign."""
        if campaign:
            return self.conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE status='sent' AND sent_at>=? "
                "AND campaign=?", (since, campaign),
            ).fetchone()["c"]
        return self.conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE status='sent' AND sent_at>=?", (since,)
        ).fetchone()["c"]

    def message_counts(self, campaign: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM messages WHERE campaign=? GROUP BY status",
            (campaign,),
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def domains_already_queued(self, campaign: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT site_domain d, COUNT(*) c FROM messages WHERE campaign=?"
            " AND status!='skipped' GROUP BY site_domain", (campaign,),
        ).fetchall()
        return {r["d"]: r["c"] for r in rows}

    def commit(self) -> None:
        self.conn.commit()
