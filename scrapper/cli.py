"""Command line entry point: init | discover | crawl | run | export | stats | reset."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from . import export as export_mod
from .config import Config
from .crawl import Crawler
from .db import Store
from .discover import register_candidates, search_urls
from .net import Fetcher

TEMPLATE = Path(__file__).resolve().parent.parent / "config" / "niche.example.yaml"


def _open(args) -> tuple[Config, Store]:
    cfg = Config.load(args.config)
    root = cfg.path.parent.parent if cfg.path else Path(".")
    db_path = Path(cfg.database)
    if not db_path.is_absolute():
        db_path = root / db_path
    return cfg, Store(db_path)


async def cmd_discover(args) -> None:
    cfg, store = _open(args)
    async with Fetcher(
        concurrency=cfg.crawl.concurrency,
        per_domain_delay=cfg.crawl.per_domain_delay,
        timeout=cfg.crawl.timeout,
        user_agent=cfg.crawl.user_agent,
        respect_robots=cfg.crawl.respect_robots,
    ) as fetcher:
        print(f"Discovering sources for niche: {cfg.niche.name}")
        n = register_candidates(store, cfg, cfg.discovery.seeds, "site", "seed")
        print(f"  seeds       -> {n} new domains")
        n = register_candidates(store, cfg, cfg.discovery.directories, "directory", "directory")
        print(f"  directories -> {n} new domains")
        urls = await search_urls(cfg, fetcher)
        n = register_candidates(store, cfg, urls, "site", "search")
        print(f"  search      -> {n} new domains")
    print(f"Total domains queued: {store.count_domains()}, pages pending: "
          f"{store.count_pages('pending')}")
    store.close()


async def cmd_crawl(args) -> None:
    cfg, store = _open(args)
    if args.max_pages:
        cfg.crawl.max_total_pages = args.max_pages
    async with Fetcher(
        concurrency=cfg.crawl.concurrency,
        per_domain_delay=cfg.crawl.per_domain_delay,
        timeout=cfg.crawl.timeout,
        user_agent=cfg.crawl.user_agent,
        respect_robots=cfg.crawl.respect_robots,
    ) as fetcher:
        crawler = Crawler(cfg, store, fetcher)
        print(f"Crawling ({store.count_pages('pending')} pages pending) ...")
        try:
            await crawler.run()
        except KeyboardInterrupt:
            print("\ninterrupted - progress saved, rerun `crawl` to resume")
    store.commit()
    print(f"Done. {store.count_emails()} unique emails stored.")
    store.close()


async def cmd_run(args) -> None:
    await cmd_discover(args)
    await cmd_crawl(args)


def cmd_export(args) -> None:
    cfg, store = _open(args)
    rows = export_mod.query(
        store,
        min_score=args.min_score,
        personal_only=args.personal_only,
        matching_domain_only=args.matching_domain,
        limit=args.limit,
    )
    path = export_mod.write(rows, args.out)
    print(f"Wrote {len(rows)} contacts to {path}")
    store.close()


def cmd_stats(args) -> None:
    cfg, store = _open(args)
    c = store.conn
    print(f"niche: {cfg.niche.name}")
    print(f"domains: {store.count_domains()} total, "
          f"{store.count_domains('accepted')} accepted, "
          f"{store.count_domains('rejected')} off-niche, "
          f"{store.count_domains('pending')} unjudged")
    print(f"pages:   {store.count_pages('done')} done, "
          f"{store.count_pages('pending')} pending, "
          f"{store.count_pages('error')} errors")
    print(f"emails:  {store.count_emails()} unique")
    for label, sql in [
        ("by kind", "SELECT kind k, COUNT(*) n FROM emails GROUP BY kind ORDER BY n DESC"),
        ("top domains",
         "SELECT site_domain k, COUNT(*) n FROM emails GROUP BY site_domain "
         "ORDER BY n DESC LIMIT 10"),
    ]:
        rows = c.execute(sql).fetchall()
        if rows:
            print(f"  {label}: " + ", ".join(f"{r['k']}={r['n']}" for r in rows))
    store.close()


def cmd_init(args) -> None:
    dest = Path(args.out)
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATE, dest)
    print(f"Wrote {dest} - edit the niche keywords, then run:\n"
          f"  python -m scrapper run -c {dest}")


def cmd_reset(args) -> None:
    cfg, store = _open(args)
    store.close()
    db = Path(cfg.database)
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if p.exists():
            p.unlink()
    print(f"Deleted {db}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scrapper", description="Niche-filtered email scraper")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_config(sp):
        sp.add_argument("-c", "--config", default="config/niche.yaml", help="niche YAML file")
        return sp

    sp = sub.add_parser("init", help="write a starter config file")
    sp.add_argument("-o", "--out", default="config/niche.yaml")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init, is_async=False)

    sp = with_config(sub.add_parser("discover", help="find candidate domains"))
    sp.set_defaults(func=cmd_discover, is_async=True)

    sp = with_config(sub.add_parser("crawl", help="crawl pending pages and extract emails"))
    sp.add_argument("--max-pages", type=int, default=0, help="override max_total_pages")
    sp.set_defaults(func=cmd_crawl, is_async=True)

    sp = with_config(sub.add_parser("run", help="discover then crawl"))
    sp.add_argument("--max-pages", type=int, default=0)
    sp.set_defaults(func=cmd_run, is_async=True)

    sp = with_config(sub.add_parser("export", help="write results to CSV/JSON"))
    sp.add_argument("-o", "--out", default="data/contacts.csv")
    sp.add_argument("--min-score", type=float, default=0.0)
    sp.add_argument("--personal-only", action="store_true", help="drop info@/sales@ addresses")
    sp.add_argument("--matching-domain", action="store_true",
                    help="only emails on the same domain as the site")
    sp.add_argument("--limit", type=int)
    sp.set_defaults(func=cmd_export, is_async=False)

    sp = with_config(sub.add_parser("stats", help="show progress"))
    sp.set_defaults(func=cmd_stats, is_async=False)

    sp = with_config(sub.add_parser("reset", help="delete the database"))
    sp.set_defaults(func=cmd_reset, is_async=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.is_async:
            asyncio.run(args.func(args))
        else:
            args.func(args)
    except KeyboardInterrupt:
        print("\naborted")
        return 130
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
