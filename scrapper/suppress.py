"""Suppression list: who must never be emailed again.

Deliberately a *separate* SQLite file from the scrape database. `reset` deletes the
scrape DB; an unsubscribe request is permanent and must survive that. Never point
this at the same path as `database:`.

Entries are either a full address (`jane@acme.com`) or a whole domain (`@acme.com`).
"""

from __future__ import annotations

import csv
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppressions (
    entry      TEXT PRIMARY KEY,   -- 'jane@acme.com' or '@acme.com'
    reason     TEXT NOT NULL,      -- unsubscribe | bounce | complaint | manual
    note       TEXT,
    created_at INTEGER NOT NULL
);
"""

REASONS = ("unsubscribe", "bounce", "complaint", "manual")


def normalize(entry: str) -> str:
    e = entry.strip().lower()
    if e.startswith("@"):
        return e
    return e


class Suppression:
    def __init__(self, path: str | Path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        self.conn = sqlite3.connect(p, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def add(self, entry: str, reason: str = "manual", note: str = "") -> bool:
        entry = normalize(entry)
        if not entry:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO suppressions(entry, reason, note, created_at) VALUES (?,?,?,?)",
            (entry, reason, note[:200], int(time.time())),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_many(self, entries: list[str], reason: str = "manual") -> int:
        return sum(self.add(e, reason) for e in entries)

    def import_file(self, path: str | Path, reason: str = "unsubscribe") -> int:
        """Read a .txt (one per line) or .csv (an `email` column, or the first column)."""
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".csv":
            rows = list(csv.reader(text.splitlines()))
            if not rows:
                return 0
            header = [c.strip().lower() for c in rows[0]]
            idx = header.index("email") if "email" in header else 0
            body = rows[1:] if "email" in header else rows
            entries = [r[idx] for r in body if r and r[idx].strip()]
        else:
            entries = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        return self.add_many(entries, reason)

    def blocks(self, email: str) -> str | None:
        """Return the matching reason if this address must not be mailed, else None."""
        e = email.strip().lower()
        domain = "@" + e.split("@")[-1] if "@" in e else ""
        row = self.conn.execute(
            "SELECT reason FROM suppressions WHERE entry=? OR entry=? LIMIT 1", (e, domain)
        ).fetchone()
        return row["reason"] if row else None

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM suppressions ORDER BY created_at DESC"
        ).fetchall()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM suppressions").fetchone()["c"]
