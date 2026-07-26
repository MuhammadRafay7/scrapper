"""OpenStreetMap / Overpass discovery.

Free, no API key. For niches that map to a physical place type (vets, dentists,
garages, pharmacies, hotels) this replaces a paid search API entirely: OSM already
knows where they are, and often knows their website and email.

Overpass is a donated volunteer service. Queries run one country at a time with a
delay between them, and back off on 429/504. Do not remove that.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .config import Config
from .db import Store
from .net import Fetcher, normalize_url, registrable_domain
from .score import is_blocked_domain, is_blocked_email

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Overpass rejects browser-spoofing User-Agents with a bare Apache 406, so this
# client identifies itself honestly rather than reusing crawl.user_agent.
OSM_HEADERS = {
    "User-Agent": "scrapper/0.1 (OSM Overpass client)",
    "Accept": "application/json",
}
RETRYABLE = (406, 429, 502, 503, 504)

EMAIL_TAGS = ("email", "contact:email", "operator:email")
SITE_TAGS = ("website", "contact:website", "url", "contact:url")

# EU/EEA/UK ISO-3166-1 alpha-2 codes, largest first so early results dominate.
EUROPE = [
    "DE", "FR", "GB", "IT", "ES", "PL", "NL", "BE", "SE", "AT", "CH", "CZ",
    "PT", "GR", "DK", "FI", "NO", "IE", "HU", "RO", "SK", "BG", "HR", "SI",
    "LT", "LV", "EE", "LU", "IS", "CY", "MT",
]


@dataclass
class Place:
    osm_id: str
    name: str
    email: str | None
    website: str | None
    city: str
    country: str


NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Keep bounding boxes on the European mainland: Nominatim's box for France or
# Spain otherwise spans the Atlantic and the Caribbean because of overseas
# territories, which turns one query into a planet-sized one.
EUROPE_CLAMP = (33.0, -25.0, 72.0, 45.0)   # south, west, north, east


def _selector(tag: str, no_website: bool = False) -> str:
    key, _, value = tag.partition("=")
    sel = f'["{key}"="{value}"]' if value else f'["{key}"]'
    if no_website:
        # Push the filter into Overpass rather than discarding results locally:
        # the response shrinks by ~80% and the server does far less work.
        sel += "".join(f'[!"{t}"]' for t in SITE_TAGS)
    return sel


def build_bbox_query(tag: str, bbox: tuple[float, float, float, float],
                     timeout: int = 300, no_website: bool = False) -> str:
    """Query by bounding box - far cheaper than resolving a country area."""
    selector = _selector(tag, no_website)
    s, w, n, e = bbox
    return (
        f"[out:json][timeout:{timeout}];\n"
        f"nwr{selector}({s:.4f},{w:.4f},{n:.4f},{e:.4f});\n"
        f"out center tags;"
    )


def split_bbox(bbox: tuple[float, float, float, float], parts: int = 2):
    """Split a box into a parts x parts grid of smaller boxes."""
    s, w, n, e = bbox
    dlat, dlon = (n - s) / parts, (e - w) / parts
    return [
        (s + i * dlat, w + j * dlon, s + (i + 1) * dlat, w + (j + 1) * dlon)
        for i in range(parts)
        for j in range(parts)
    ]


async def country_bbox(fetcher: Fetcher, country: str):
    """Look up a country's bounding box, clamped to Europe."""
    try:
        r = await fetcher.client.get(
            NOMINATIM,
            params={"country": country, "format": "json", "limit": 1},
            headers=OSM_HEADERS,
            timeout=45.0,
        )
        data = r.json()
        if not data:
            return None
        s, n, w, e = (float(x) for x in data[0]["boundingbox"])
    except Exception:
        return None
    cs, cw, cn, ce = EUROPE_CLAMP
    box = (max(s, cs), max(w, cw), min(n, cn), min(e, ce))
    return box if box[0] < box[2] and box[1] < box[3] else None


def build_query(country: str, tags: list[str], timeout: int = 180,
                no_website: bool = False) -> str:
    """One Overpass QL query for a country, unioning every configured tag filter."""
    clauses = []
    for tag in tags:
        clauses.append(f"  nwr{_selector(tag, no_website)}(area.searchArea);")
    body = "\n".join(clauses)
    return (
        f"[out:json][timeout:{timeout}];\n"
        f'area["ISO3166-1"="{country}"][admin_level=2]->.searchArea;\n'
        f"(\n{body}\n);\n"
        f"out center tags;"
    )


def parse_overpass(payload: dict, country: str) -> list[Place]:
    places = []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        email = next((tags[t] for t in EMAIL_TAGS if tags.get(t)), None)
        website = next((tags[t] for t in SITE_TAGS if tags.get(t)), None)
        if not email and not website:
            continue
        if email and "," in email:
            email = email.split(",")[0].strip()
        if email and email.lower().startswith("mailto:"):
            email = email[7:]
        places.append(
            Place(
                osm_id=f"{el.get('type', 'node')}/{el.get('id', '')}",
                name=tags.get("name", "").strip(),
                email=email.strip().lower() if email else None,
                website=website.strip() if website else None,
                city=tags.get("addr:city", "").strip(),
                country=tags.get("addr:country", country).strip(),
            )
        )
    return places


async def _post_query(fetcher: Fetcher, label: str, query: str,
                      attempts: int = 3) -> dict | None:
    for attempt in range(attempts):
        endpoint = ENDPOINTS[attempt % len(ENDPOINTS)]
        try:
            r = await fetcher.client.post(
                endpoint, data={"data": query}, headers=OSM_HEADERS, timeout=240.0
            )
        except Exception as e:
            print(f"    {label}: {type(e).__name__}, retrying")
            await asyncio.sleep(10 * (attempt + 1))
            continue
        if r.status_code in RETRYABLE:         # Overpass is busy - back off, don't hammer
            wait = 20 * (attempt + 1)
            print(f"    {label}: overpass busy ({r.status_code}), waiting {wait}s")
            await asyncio.sleep(wait)
            continue
        if r.status_code != 200:
            print(f"    {label}: HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except Exception:
            print(f"    {label}: bad JSON response")
            return None
    return None


async def harvest(cfg: Config, store: Store, fetcher: Fetcher) -> dict[str, int]:
    """Query OSM per country; store direct emails, queue websites as verified domains."""
    osm = cfg.discovery.osm
    stats = {"places": 0, "emails": 0, "domains": 0}
    if not osm.enabled:
        return stats

    countries = [c.upper() for c in (osm.countries or EUROPE)]
    print(f"OSM discovery: {len(countries)} countries, tags={osm.tags}")

    for i, country in enumerate(countries, 1):
        # One query per tag. Unioning many tags over a whole country regularly
        # exceeds Overpass's server-side time limit and comes back as a 504.
        totals = [0, 0, 0]
        bbox = None            # looked up lazily, only if an area query fails
        for j, tag in enumerate(osm.tags):
            payloads = []
            got = await _post_query(
                fetcher, f"{country}/{tag}",
                build_query(country, [tag], no_website=osm.skip_with_website),
            )
            if got is not None:
                payloads.append(got)
            else:
                # Whole-country area queries time out on large countries. Fall
                # back to a bbox grid: each cell is a far cheaper query.
                if bbox is None:
                    bbox = await country_bbox(fetcher, country)
                    await asyncio.sleep(1.5)      # Nominatim: max ~1 req/sec
                if bbox:
                    cells = split_bbox(bbox, osm.bbox_grid)
                    print(f"    {country}/{tag}: splitting into {len(cells)} cells")
                    for k, cell in enumerate(cells, 1):
                        cell_payload = await _post_query(
                            fetcher, f"{country}/{tag} cell {k}/{len(cells)}",
                            build_bbox_query(tag, cell,
                                             no_website=osm.skip_with_website),
                            attempts=2,
                        )
                        if cell_payload is not None:
                            payloads.append(cell_payload)
                        await asyncio.sleep(osm.per_tag_delay)

            for payload in payloads:
                places = parse_overpass(payload, country)
                e, d = _store_places(store, cfg, places)
                totals[0] += len(places)
                totals[1] += e
                totals[2] += d
            if j < len(osm.tags) - 1:
                await asyncio.sleep(osm.per_tag_delay)
        stats["places"] += totals[0]
        stats["emails"] += totals[1]
        stats["domains"] += totals[2]
        print(f"  [{i}/{len(countries)}] {country}: {totals[0]} places, "
              f"+{totals[1]} emails, +{totals[2]} domains")
        store.commit()
        if i < len(countries):
            await asyncio.sleep(osm.per_country_delay)

    store.commit()
    return stats


def _store_places(store: Store, cfg: Config, places: list[Place]) -> tuple[int, int]:
    emails = domains = 0
    # OSM-sourced sites are pre-verified by the tag itself, so they skip the
    # keyword gate. This score is what the crawler credits their pages with.
    verified_score = max(cfg.niche.domain_threshold * 2, 10.0)

    skip_webbed = cfg.discovery.osm.skip_with_website

    for place in places:
        # Prospecting mode: a business that already runs its own website is not
        # the target, so we neither keep its email nor queue its site to crawl.
        if skip_webbed and place.website:
            continue

        site_domain = ""
        if place.website:
            url = normalize_url(place.website)
            if url:
                domain = registrable_domain(url)
                if domain and not is_blocked_domain(domain, cfg.filters.blocked_domains):
                    site_domain = domain
                    if store.add_domain(domain, kind="site", discovered_by="osm"):
                        domains += 1
                    store.set_domain_status(domain, "accepted", verified_score,
                                            f"osm:{place.osm_id}")
                    store.add_page(url, domain, depth=0, priority=10)

        if not place.email:
            continue
        if is_blocked_email(place.email, cfg.filters.blocked_email_patterns):
            continue
        if "@" not in place.email:
            continue
        local, _, email_domain = place.email.partition("@")
        if is_blocked_domain(email_domain, cfg.filters.blocked_domains):
            continue
        created = store.add_email({
            "email": place.email,
            "local_part": local,
            "email_domain": email_domain,
            "site_domain": site_domain or email_domain,
            "kind": "role" if local in ("info", "praxis", "kontakt", "contact") else "personal",
            "name": None,
            "context": f"{place.name} · {place.city} · {place.country}".strip(" ·"),
            "source_url": f"https://www.openstreetmap.org/{place.osm_id}",
            "page_title": place.name,
            "score": verified_score,
            "has_website": 1 if place.website else 0,
        })
        if created:
            emails += 1
    return emails, domains
