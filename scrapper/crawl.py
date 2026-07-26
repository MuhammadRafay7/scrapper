"""The crawl loop: fetch -> score -> gate -> extract -> enqueue."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from .config import Config
from .db import Store
from .discover import expand_sitemap
from .extract import find_emails, parse
from .net import Fetcher, looks_fetchable, normalize_url, registrable_domain
from .score import Score, is_blocked_domain, is_blocked_email, score_page


class Crawler:
    def __init__(self, cfg: Config, store: Store, fetcher: Fetcher):
        self.cfg = cfg
        self.store = store
        self.fetcher = fetcher
        self.domain_scores: dict[str, float] = {}
        self.stats = {"fetched": 0, "errors": 0, "emails": 0, "accepted": 0, "rejected": 0}

    async def run(self) -> None:
        cfg = self.cfg
        budget = cfg.crawl.max_total_pages
        while True:
            done = self.store.count_pages("done") + self.store.count_pages("error")
            if done >= budget:
                print(f"  reached max_total_pages ({budget})")
                return
            batch = self.store.next_pending(cfg.crawl.concurrency * 3)
            if not batch:
                return
            await asyncio.gather(*(self._handle(dict(row)) for row in batch))
            self.store.commit()
            print(
                f"  fetched={self.stats['fetched']} emails={self.stats['emails']} "
                f"domains ok/no={self.stats['accepted']}/{self.stats['rejected']} "
                f"pending={self.store.count_pages('pending')}"
            )

    async def _handle(self, row: dict) -> None:
        url, domain, depth = row["url"], row["domain"], row["depth"]
        cfg = self.cfg

        if self.store.domain_status(domain) == "rejected":
            self.store.finish_page(url, "skipped", error="domain rejected")
            return
        if self.store.pages_done_for_domain(domain) >= cfg.crawl.max_pages_per_domain:
            self.store.finish_page(url, "skipped", error="domain page cap")
            return

        resp = await self.fetcher.get(url)
        if not resp.ok:
            self.stats["errors"] += 1
            self.store.finish_page(url, "error", resp.status, error=resp.error or "empty body")
            if depth == 0:
                self.store.set_domain_status(domain, "error", note=resp.error[:200])
            return

        self.stats["fetched"] += 1
        page = parse(resp.text, resp.final_url or url)
        score = score_page(page, cfg.niche)
        kind = self.store.domain_kind(domain) or "site"

        # Domain gate: judged once, on the first page we see for that domain.
        if kind == "site" and self.store.domain_status(domain) == "pending":
            if not self._gate_domain(domain, url, score):
                self.store.finish_page(url, "done", resp.status, score.value, page.title)
                return
            base = f"{urlparse(resp.final_url or url).scheme}://{urlparse(resp.final_url or url).netloc}"
            await expand_sitemap(self.fetcher, self.store, cfg, domain, base)

        domain_score = self.domain_scores.get(domain, 0.0)
        if kind != "directory":
            self._harvest(page, domain, score, domain_score)

        self._enqueue_links(page, domain, depth, kind)
        self.store.finish_page(url, "done", resp.status, score.value, page.title)

    def _gate_domain(self, domain: str, url: str, score: Score) -> bool:
        if score.value >= self.cfg.niche.domain_threshold:
            self.domain_scores[domain] = score.value
            self.store.set_domain_status(domain, "accepted", score.value, score.top())
            self.stats["accepted"] += 1
            return True
        self.store.set_domain_status(domain, "rejected", score.value, f"off-niche: {score.top()}")
        self.store.drop_pending_for_domain(domain)
        self.stats["rejected"] += 1
        return False

    def _harvest(self, page, domain: str, score: Score, domain_score: float) -> None:
        cfg = self.cfg
        combined = round(score.value + domain_score * 0.5, 2)
        if combined < cfg.niche.page_threshold:
            return
        for found in find_emails(page):
            if is_blocked_email(found.email, cfg.filters.blocked_email_patterns):
                continue
            if is_blocked_domain(found.email_domain, cfg.filters.blocked_domains):
                continue
            if found.kind == "role" and not cfg.filters.keep_role_addresses:
                continue
            if cfg.filters.require_matching_domain and \
                    registrable_domain(found.email_domain) != domain:
                continue
            created = self.store.add_email({
                "email": found.email,
                "local_part": found.local_part,
                "email_domain": found.email_domain,
                "site_domain": domain,
                "kind": found.kind,
                "name": found.name,
                "context": found.context,
                "source_url": page.url,
                "page_title": page.title[:200],
                "score": combined,
            })
            if created:
                self.stats["emails"] += 1

    def _enqueue_links(self, page, domain: str, depth: int, kind: str) -> None:
        cfg = self.cfg
        if depth >= cfg.crawl.max_depth and kind != "directory":
            return
        same_domain_added = 0
        for href in page.links:
            url = normalize_url(href, base=page.url)
            if not url or not looks_fetchable(url):
                continue
            target = registrable_domain(url)
            if is_blocked_domain(target, cfg.filters.blocked_domains):
                continue

            if target == domain:
                if same_domain_added >= cfg.crawl.max_pages_per_domain:
                    continue
                if not cfg.crawl.allow_subdomains and \
                        urlparse(url).netloc.lower() != urlparse(page.url).netloc.lower():
                    continue
                if self.store.add_page(url, domain, depth + 1, self._priority(url)):
                    same_domain_added += 1
            elif kind == "directory":
                # A directory's outbound links are the whole point: each is a
                # candidate business site, gated on its own homepage.
                if self.store.add_domain(target, kind="site", discovered_by=f"dir:{domain}"):
                    home = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
                    self.store.add_page(home, target, depth=0, priority=10)

    def _priority(self, url: str) -> int:
        path = urlparse(url).path.lower()
        return 8 if any(p in path for p in self.cfg.crawl.priority_paths) else 1
