"""SQLite storage. Everything is resumable: kill the process, rerun, it continues."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# has_website is three-valued. Two-valued was wrong: an address found on a
# directory page proves nothing about whether that business runs a site.
WEBSITE_NONE = 0      # confirmed no website (e.g. OSM place with no website tag)
WEBSITE_OWN = 1       # confirmed: found on the business's own site
WEBSITE_UNKNOWN = 2   # listed in a directory; we never visited a site of theirs

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
    -- 1 = this contact's business runs its own website. Prospecting for
    -- businesses with no online presence filters on this.
    has_website INTEGER NOT NULL DEFAULT 0,
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
    sender      TEXT,                            -- the account it went out from
    queued_at   INTEGER NOT NULL,
    sent_at     INTEGER,
    PRIMARY KEY (campaign, email)
);
CREATE INDEX IF NOT EXISTS messages_queue ON messages(campaign, status);
CREATE INDEX IF NOT EXISTS messages_sent  ON messages(sent_at);
"""


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        p = Path(path)
        self.readonly = readonly
        if readonly:
            # Readers must not create tables or run migrations: those take write
            # locks, and a scrape DB is usually busy being crawled. Outreach only
            # reads the scrape, so it opens it this way.
            self.conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
            self.conn.row_factory = sqlite3.Row
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS never adds columns, so widen older DBs here."""
        mcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if mcols and "sender" not in mcols:
            # Throttles protect one mailbox's reputation, so sends must be counted
            # per account once more than one account is in play.
            self.conn.execute("ALTER TABLE messages ADD COLUMN sender TEXT")

        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(emails)")}
        if "has_website" not in cols:
            self.conn.execute(
                "ALTER TABLE emails ADD COLUMN has_website INTEGER NOT NULL DEFAULT 0"
            )
            # Backfill: an OSM contact whose site_domain was queued as a domain
            # is exactly one we found a website for.
            self.conn.execute(
                "UPDATE emails SET has_website=1 "
                "WHERE site_domain IN (SELECT domain FROM domains)"
            )

    def close(self) -> None:
        if not self.readonly:
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
        rec = {"has_website": 0, **rec}
        cur = self.conn.execute("SELECT email, score FROM emails WHERE email=?", (rec["email"],))
        existing = cur.fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO emails(email, local_part, email_domain, site_domain, kind, name,"
                " context, source_url, page_title, score, hits, has_website, created_at)"
                " VALUES (:email,:local_part,:email_domain,:site_domain,:kind,:name,:context,"
                ":source_url,:page_title,:score,1,:has_website,:created_at)",
                {**rec, "created_at": int(time.time())},
            )
            return True
        # Once we know a business has a website, that fact stays true; an
        # UNKNOWN sighting must never overwrite a confirmed one.
        if rec.get("has_website") == WEBSITE_OWN:
            self.conn.execute("UPDATE emails SET has_website=1 WHERE email=:email", rec)
        elif rec.get("has_website") == WEBSITE_UNKNOWN:
            self.conn.execute(
                "UPDATE emails SET has_website=2 WHERE email=:email AND has_website=0", rec)
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
                       subject: str = "", error: str = "", sender: str = "") -> None:
        self.conn.execute(
            "UPDATE messages SET status=?, subject=?, error=?, attempts=attempts+1,"
            " sender=COALESCE(NULLIF(?,''), sender), sent_at=? WHERE campaign=? AND email=?",
            (status, subject[:300], error[:300], sender,
             int(time.time()) if status == "sent" else None, campaign, email),
        )

    def sent_since(self, since: int, sender: str = "") -> int:
        """Sends in the window, across every campaign that used this account.

        Keyed on the account, not the campaign: the limit being protected is one
        mailbox's reputation, and two campaigns sharing an account share its budget.
        """
        if sender:
            return self.conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE status='sent' AND sent_at>=? "
                "AND sender=?", (since, sender),
            ).fetchone()["c"]
        return self.conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE status='sent' AND sent_at>=?", (since,)
        ).fetchone()["c"]

    def already_contacted(self, email: str) -> str | None:
        """The campaign that already holds this address, if any. Stops a second
        account mailing someone the first account already reached."""
        row = self.conn.execute(
            "SELECT campaign FROM messages WHERE email=? AND status!='skipped' LIMIT 1",
            (email,),
        ).fetchone()
        return row["campaign"] if row else None

    def message_counts(self, campaign: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM messages WHERE campaign=? GROUP BY status",
            (campaign,),
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def queued_identities(self, campaign: str) -> list[sqlite3.Row]:
        """Rows already queued, for the caller to re-key by business identity."""
        return self.conn.execute(
            "SELECT email, site_domain FROM messages WHERE campaign=? AND status!='skipped'",
            (campaign,),
        ).fetchall()

    def commit(self) -> None:
        self.conn.commit()
