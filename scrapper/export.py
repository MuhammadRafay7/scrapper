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


def write(rows: list[dict], out: str | Path) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
        return path
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["context"] = (row.get("context") or "").replace("\n", " ")[:200]
            writer.writerow(row)
    return path
