"""CSV / JSON export with post-hoc filters."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .db import Store

COLUMNS = [
    "email", "name", "kind", "site_domain", "email_domain",
    "score", "hits", "page_title", "source_url", "context",
]


def query(store: Store, *, min_score: float = 0.0, personal_only: bool = False,
          matching_domain_only: bool = False, limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM emails WHERE score >= ?"
    params: list = [min_score]
    if personal_only:
        sql += " AND kind = 'personal'"
    if matching_domain_only:
        sql += " AND email_domain LIKE '%' || site_domain"
    sql += " ORDER BY score DESC, email ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in store.conn.execute(sql, params).fetchall()]


def shape(row: dict) -> dict:
    """One contact as a clean record: the reported columns only, no internal ids."""
    out = {k: row.get(k) for k in COLUMNS}
    out["context"] = (out.get("context") or "").replace("\n", " ").strip()[:200]
    out["name"] = out.get("name") or None
    return out


def write(rows: list[dict], out: str | Path) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.suffix == ".json":
        # A plain array - directly consumable by JSON.parse / prisma createMany.
        payload = [shape(r) for r in rows]
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return path

    if path.suffix == ".jsonl":
        # One object per line: streamable, appendable, no full-file parse needed.
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(shape(row), ensure_ascii=False) + "\n")
        return path

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["context"] = (row.get("context") or "").replace("\n", " ")[:200]
            writer.writerow(row)
    return path
