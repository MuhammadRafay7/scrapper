"""Command line entry point: init | discover | crawl | run | export | stats | reset | mail."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

from . import campaign as campaign_mod
from . import export as export_mod
from . import mailer
from . import osm
from .config import Config
from .crawl import Crawler
from .db import Store
from .discover import register_candidates, search_urls
from .net import Fetcher
from .suppress import Suppression

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TEMPLATE = CONFIG_DIR / "niche.example.yaml"
CAMPAIGN_TEMPLATE = CONFIG_DIR / "campaign.example.yaml"


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
        if cfg.discovery.osm.enabled:
            s = await osm.harvest(cfg, store, fetcher)
            print(f"  openstreetmap -> {s['places']} places, {s['emails']} emails, "
                  f"{s['domains']} new domains")
    print(f"Total domains queued: {store.count_domains()}, pages pending: "
          f"{store.count_pages('pending')}")
    store.close()


async def cmd_osm(args) -> None:
    """OSM discovery on its own - useful to re-run for extra countries later."""
    cfg, store = _open(args)
    cfg.discovery.osm.enabled = True
    if args.countries:
        cfg.discovery.osm.countries = [c.strip().upper() for c in args.countries.split(",")]
    async with Fetcher(
        concurrency=2, per_domain_delay=cfg.discovery.osm.per_country_delay,
        timeout=240.0, user_agent=cfg.crawl.user_agent, respect_robots=False,
    ) as fetcher:
        s = await osm.harvest(cfg, store, fetcher)
    print(f"OSM: {s['places']} places -> {s['emails']} emails, {s['domains']} domains queued")
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
        no_website=args.no_website,
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


# ------------------------------------------------------------------- outreach

def _open_campaign(args):
    """(config, scrape store, shared outreach store, suppression list)"""
    cfg = campaign_mod.CampaignConfig.load(args.config)
    return (cfg, Store(cfg.resolve(cfg.database), readonly=True),
            Store(cfg.resolve(cfg.outreach_db)),
            Suppression(cfg.resolve(cfg.suppression_db)))


def _check(cfg) -> bool:
    problems = campaign_mod.validate(cfg)
    if problems:
        print("Campaign is not ready to send:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
    return not problems


def cmd_mail_init(args) -> None:
    dest = Path(args.out)
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(CAMPAIGN_TEMPLATE, dest)
    print(f"Wrote {dest} - fill in sender/message, then:\n"
          f"  python -m scrapper mail build -c {dest}\n"
          f"  python -m scrapper mail send  -c {dest}          # dry run\n"
          f"  python -m scrapper mail send  -c {dest} --live")


def cmd_mail_check(args) -> None:
    cfg = campaign_mod.CampaignConfig.load(args.config)
    if _check(cfg):
        print(f"Campaign '{cfg.id}' looks sendable.")
    else:
        raise SystemExit(1)


def cmd_mail_build(args) -> None:
    cfg, store, out, sup = _open_campaign(args)
    if args.limit:
        cfg.audience.limit = args.limit
    s = campaign_mod.build(cfg, store, out, sup)
    print(f"Campaign '{cfg.id}': queued {s['queued']} new recipients")
    print(f"  already queued {s['already']}, suppressed {s['suppressed']}, "
          f"one-per-domain cap {s['domain_capped']}, excluded {s['excluded']}")
    if s.get("has_website"):
        print(f"  skipped {s['has_website']} that already have a website")
    if s.get("other_campaign"):
        print(f"  skipped {s['other_campaign']} already contacted by another campaign")
    store.close()
    out.close()
    sup.close()


def cmd_mail_preview(args) -> None:
    cfg, store, out, sup = _open_campaign(args)
    ok = _check(cfg)
    rows = out.pending_messages(cfg.id, args.limit)
    if not rows:
        print("Nothing pending - run `mail build` first.")
    for row in rows:
        rec = campaign_mod._enrich(store, dict(row))
        subject, text, _ = campaign_mod.render_for(cfg, rec)
        print("=" * 70)
        print(f"To:      {rec['email']}")
        print(f"Subject: {subject}\n")
        print(text)
    store.close()
    out.close()
    sup.close()
    if not ok:
        raise SystemExit(1)


def cmd_mail_send(args) -> None:
    cfg, store, out, sup = _open_campaign(args)
    if not _check(cfg):
        store.close()
        out.close()
        sup.close()
        raise SystemExit(1)

    hourly, daily = campaign_mod.remaining_allowance(cfg, out)
    pending = out.message_counts(cfg.id).get("pending", 0)
    print(f"Campaign '{cfg.id}': {pending} pending, allowance {hourly}/hour {daily}/day")

    def progress(email, n, total):
        print(f"  [{n}/{total}] {email}")

    if args.live:
        user = os.environ.get(cfg.smtp.user_env, "")
        password = os.environ.get(cfg.smtp.password_env, "")
        if not cfg.smtp.host:
            print("error: smtp.host is not set", file=sys.stderr)
            raise SystemExit(1)
        if not password:
            print(f"error: ${cfg.smtp.password_env} is not set", file=sys.stderr)
            raise SystemExit(1)
        transport = mailer.Smtp(cfg.smtp.host, cfg.smtp.port, user, password,
                                cfg.smtp.starttls)
        print(f"LIVE - sending through {cfg.smtp.host} as {cfg.sender.from_email}")
    else:
        outdir = cfg.resolve(cfg.preview_dir)
        transport = mailer.DryRun(outdir)
        print(f"DRY RUN - writing .eml files to {outdir} (add --live to actually send)")

    archive = None
    if args.live and cfg.archive.enabled:
        archive = mailer.ImapArchive(cfg.archive.host, os.environ.get(cfg.smtp.user_env, ""),
                                     os.environ.get(cfg.smtp.password_env, ""),
                                     cfg.archive.folder)
        archive.__enter__()
        if archive.conn is None:
            print(f"  note: could not open IMAP - sends will not be labelled "
                  f"'{cfg.archive.folder}' (delivery is unaffected)")
        else:
            print(f"  filing copies under the '{cfg.archive.folder}' label")
    try:
        with transport:
            s = campaign_mod.send(cfg, store, out, sup, transport, limit=args.limit,
                                  on_send=progress, record=args.live, archive=archive)
    finally:
        if archive is not None:
            archive.close()
    verb = "sent" if args.live else "rendered"
    print(f"{verb} {s['sent']}, failed {s['failed']}, suppressed {s['suppressed']}"
          + (f", deferred {s['deferred']} (will retry)" if s.get("deferred") else ""))
    if args.live and s.get("archived"):
        print(f"  labelled {s['archived']} under '{cfg.archive.folder}'")
    if args.live and s["sent"]:
        print(f"  recorded in {cfg.resolve(cfg.sent_json)}")
    if s["stopped"]:
        print(f"  stopped on the {s['stopped']} cap - rerun later to continue")
    if not args.live and s["sent"]:
        print("  nothing was delivered; review the .eml files, then rerun with --live")
    store.close()
    out.close()
    sup.close()


def cmd_mail_test(args) -> None:
    """Send one rendered message to yourself, using a real recipient's data."""
    cfg, store, out, sup = _open_campaign(args)
    if not _check(cfg):
        raise SystemExit(1)
    row = store.conn.execute(
        "SELECT * FROM emails ORDER BY score DESC LIMIT 1"
    ).fetchone()
    rec = dict(row) if row else {"email": args.to, "name": "Test Person",
                                 "site_domain": "example.com"}
    subject, text, html = campaign_mod.render_for(cfg, rec)
    msg = mailer.build_message(cfg.sender, args.to, "", subject, text, html)
    user = os.environ.get(cfg.smtp.user_env, "")
    password = os.environ.get(cfg.smtp.password_env, "")
    with mailer.Smtp(cfg.smtp.host, cfg.smtp.port, user, password, cfg.smtp.starttls) as t:
        t.send(msg)
    print(f"Test message sent to {args.to} (rendered for {rec['email']})")
    store.close()
    out.close()
    sup.close()


def cmd_mail_status(args) -> None:
    cfg, store, out, sup = _open_campaign(args)
    counts = out.message_counts(cfg.id)
    hourly, daily = campaign_mod.remaining_allowance(cfg, out)
    print(f"campaign: {cfg.id}   ({cfg.sender.from_name} <{cfg.sender.from_email}>)")
    print(f"queue:    " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty")
    print(f"allowance remaining: {hourly}/hour, {daily}/day")
    print(f"suppressed addresses: {sup.count()} ({sup.path})")
    rows = out.conn.execute(
        "SELECT email, error FROM messages WHERE campaign=? AND status='failed' LIMIT 5",
        (cfg.id,),
    ).fetchall()
    for r in rows:
        print(f"  failed: {r['email']} - {r['error']}")
    store.close()
    out.close()
    sup.close()


def cmd_mail_sent(args) -> None:
    """Everyone contacted so far, as JSON. Reads the append-only ledger."""
    import json
    cfg = campaign_mod.CampaignConfig.load(args.config)
    path = cfg.resolve(cfg.sent_json)
    if not path.exists():
        print(f"no sends recorded yet ({path})")
        return
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.campaign:
        entries = [e for e in entries if e.get("campaign") == args.campaign]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    by_campaign = {}
    for e in entries:
        by_campaign[e.get("campaign", "?")] = by_campaign.get(e.get("campaign", "?"), 0) + 1
    print(f"Wrote {len(entries)} contacted businesses to {out}")
    for k, v in sorted(by_campaign.items()):
        print(f"  {k}: {v}")


def cmd_suppress(args) -> None:
    cfg = campaign_mod.CampaignConfig.load(args.config)
    sup = Suppression(cfg.resolve(cfg.suppression_db))
    if args.file:
        n = sup.import_file(args.file, args.reason)
        print(f"Imported {n} new suppressions from {args.file}")
    if args.entries:
        n = sup.add_many(args.entries, args.reason)
        print(f"Added {n} new suppressions")
    if args.list:
        for r in sup.all():
            print(f"{r['entry']}\t{r['reason']}\t{r['note'] or ''}")
    print(f"{sup.count()} suppressed entries in {sup.path}")
    sup.close()


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

    sp = with_config(sub.add_parser("osm", help="OpenStreetMap discovery (free, no API key)"))
    sp.add_argument("--countries", help="comma-separated ISO codes, e.g. DE,FR,NL")
    sp.set_defaults(func=cmd_osm, is_async=True)

    sp = with_config(sub.add_parser("crawl", help="crawl pending pages and extract emails"))
    sp.add_argument("--max-pages", type=int, default=0, help="override max_total_pages")
    sp.set_defaults(func=cmd_crawl, is_async=True)

    sp = with_config(sub.add_parser("run", help="discover then crawl"))
    sp.add_argument("--max-pages", type=int, default=0)
    sp.set_defaults(func=cmd_run, is_async=True)

    sp = with_config(sub.add_parser("export", help="write results to CSV/JSON"))
    sp.add_argument("-o", "--out", default="data/contacts.csv",
                    help="output file; format follows the suffix (.json | .jsonl | .csv)")
    sp.add_argument("--min-score", type=float, default=0.0)
    sp.add_argument("--personal-only", action="store_true", help="drop info@/sales@ addresses")
    sp.add_argument("--matching-domain", action="store_true",
                    help="only emails on the same domain as the site")
    sp.add_argument("--no-website", action="store_true",
                    help="only businesses with no website of their own")
    sp.add_argument("--limit", type=int)
    sp.set_defaults(func=cmd_export, is_async=False)

    sp = with_config(sub.add_parser("stats", help="show progress"))
    sp.set_defaults(func=cmd_stats, is_async=False)

    sp = with_config(sub.add_parser("reset", help="delete the database"))
    sp.set_defaults(func=cmd_reset, is_async=False)

    # ---- mail: outreach to the addresses already collected ----
    mail = sub.add_parser("mail", help="send outreach to scraped contacts")
    msub = mail.add_subparsers(dest="mailcmd", required=True)

    def with_campaign(sp):
        sp.add_argument("-c", "--config", default="config/campaign.yaml",
                        help="campaign YAML file")
        sp.set_defaults(is_async=False)
        return sp

    sp = msub.add_parser("init", help="write a starter campaign config")
    sp.add_argument("-o", "--out", default="config/campaign.yaml")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_mail_init, is_async=False)

    sp = with_campaign(msub.add_parser("check", help="verify the campaign may be sent"))
    sp.set_defaults(func=cmd_mail_check)

    sp = with_campaign(msub.add_parser("build", help="queue recipients from the scrape"))
    sp.add_argument("--limit", type=int, default=0, help="cap how many to queue")
    sp.set_defaults(func=cmd_mail_build)

    sp = with_campaign(msub.add_parser("preview", help="render queued messages to stdout"))
    sp.add_argument("-n", "--limit", type=int, default=3)
    sp.set_defaults(func=cmd_mail_preview)

    sp = with_campaign(msub.add_parser("send", help="send the queue (dry run unless --live)"))
    sp.add_argument("--live", action="store_true", help="actually deliver over SMTP")
    sp.add_argument("--limit", type=int, default=0, help="stop after N messages")
    sp.set_defaults(func=cmd_mail_send)

    sp = with_campaign(msub.add_parser("test", help="send one message to yourself"))
    sp.add_argument("--to", required=True, help="your own address")
    sp.set_defaults(func=cmd_mail_test)

    sp = with_campaign(msub.add_parser("sent", help="export everyone contacted, as JSON"))
    sp.add_argument("-o", "--out", default="data/sent.json")
    sp.add_argument("--campaign", help="filter to one campaign id")
    sp.set_defaults(func=cmd_mail_sent)

    sp = with_campaign(msub.add_parser("status", help="queue and throttle status"))
    sp.set_defaults(func=cmd_mail_status)

    sp = with_campaign(msub.add_parser("suppress", help="manage the do-not-email list"))
    sp.add_argument("entries", nargs="*", help="addresses, or @domain.com for a whole domain")
    sp.add_argument("--file", help="import a .txt or .csv of addresses")
    sp.add_argument("--reason", default="unsubscribe",
                    choices=["unsubscribe", "bounce", "complaint", "manual"])
    sp.add_argument("--list", action="store_true")
    sp.set_defaults(func=cmd_suppress)
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
