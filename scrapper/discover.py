"""Finding candidate sources: search APIs, seed lists, directories, sitemaps."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from .config import Config
from .db import Store
from .net import Fetcher, normalize_url, registrable_domain, looks_fetchable
from .score import is_blocked_domain


async def search_urls(cfg: Config, fetcher: Fetcher) -> list[str]:
    """Run the configured niche queries through a search API. Returns result URLs."""
    sc = cfg.discovery.search
    if sc.provider == "none" or not sc.queries:
        return []
    key = sc.api_key
    if not key:
        print(f"  ! {sc.provider}: ${sc.api_key_env} not set, skipping search discovery")
        return []

    urls: list[str] = []
    for query in sc.queries:
        try:
            found = await _run_query(sc.provider, query, key, sc, fetcher)
        except Exception as e:
            print(f"  ! search failed for {query!r}: {e}")
            continue
        print(f"  search {query!r} -> {len(found)} results")
        urls.extend(found)
        await asyncio.sleep(1.0)
    return urls


async def _run_query(provider: str, query: str, key: str, sc, fetcher: Fetcher) -> list[str]:
    client = fetcher.client
    if provider == "serper":
        r = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query, "num": min(sc.per_query, 100)},
        )
        r.raise_for_status()
        return [item["link"] for item in r.json().get("organic", []) if item.get("link")]

    if provider == "brave":
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            params={"q": query, "count": min(sc.per_query, 20)},
        )
        r.raise_for_status()
        return [x["url"] for x in r.json().get("web", {}).get("results", []) if x.get("url")]

    if provider == "google_cse":
        import os
        cx = os.environ.get(sc.cse_id_env)
        if not cx:
            raise RuntimeError(f"${sc.cse_id_env} not set")
        out = []
        for start in range(1, min(sc.per_query, 50), 10):  # CSE returns 10 per call
            r = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": key, "cx": cx, "q": query, "start": start, "num": 10},
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            out.extend(i["link"] for i in items if i.get("link"))
            if len(items) < 10:
                break
        return out

    raise ValueError(f"unknown search provider {provider!r}")


def register_candidates(store: Store, cfg: Config, urls: list[str], kind: str,
                        discovered_by: str) -> int:
    """Turn URLs into domain records + a seed page each."""
    added = 0
    for raw in urls:
        url = normalize_url(raw)
        if not url or not looks_fetchable(url):
            continue
        domain = registrable_domain(url)
        if not domain or is_blocked_domain(domain, cfg.filters.blocked_domains):
            continue
        if store.add_domain(domain, kind=kind, discovered_by=discovered_by):
            added += 1
        # Directory URLs are crawled as given; site URLs start from the homepage
        # so the domain-level niche check sees the front page.
        entry = url if kind == "directory" else f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
        store.add_page(entry, domain, depth=0, priority=10)
        if kind != "directory" and url != entry:
            store.add_page(url, domain, depth=1, priority=5)
    store.commit()
    return added


SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


async def expand_sitemap(fetcher: Fetcher, store: Store, cfg: Config, domain: str,
                         base: str) -> int:
    """Pull contact-ish URLs out of sitemap.xml for a domain we already accepted."""
    if not cfg.discovery.use_sitemaps:
        return 0
    added = 0
    seen_maps: set[str] = set()
    queue = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]
    priority_paths = cfg.crawl.priority_paths

    while queue and added < cfg.discovery.sitemap_max_urls:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        resp = await fetcher.get(sm)
        if not resp.ok:
            continue
        locs = SITEMAP_LOC_RE.findall(resp.text)
        for loc in locs:
            if loc.endswith(".xml") and len(seen_maps) < 10:
                queue.append(loc)
                continue
            url = normalize_url(loc)
            if not url or not looks_fetchable(url):
                continue
            if registrable_domain(url) != domain:
                continue
            path = urlparse(url).path.lower()
            if not any(p in path for p in priority_paths):
                continue
            if store.add_page(url, domain, depth=1, priority=8):
                added += 1
            if added >= cfg.discovery.sitemap_max_urls:
                break
    store.commit()
    return added
