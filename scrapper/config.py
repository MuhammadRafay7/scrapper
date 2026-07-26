"""Configuration loading. One YAML file describes a niche and how to crawl for it."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; NicheContactBot/1.0; "
    "+contact-scraper; respects robots.txt)"
)

# Domains that never carry useful niche contact data, or that forbid scraping.
DEFAULT_BLOCKED_DOMAINS = [
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "pinterest.com", "youtube.com", "reddit.com", "quora.com",
    "wikipedia.org", "amazon.com", "ebay.com", "google.com", "bing.com",
    "yelp.com", "glassdoor.com", "indeed.com", "crunchbase.com",
    "w3.org", "schema.org", "gstatic.com", "googleapis.com",
]

# Substrings that mark an address as machinery, not a person or business.
DEFAULT_BLOCKED_EMAIL_PATTERNS = [
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "mailer-daemon", "example.com", "example.org", "yourdomain", "domain.com",
    "email.com", "sentry.io", "wixpress.com", "godaddy.com", "squarespace.com",
    "@2x", "@3x", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css",
    "u003e", "sentry-next", "core-js", "@babel", "@types", "@angular",
]

# Pages most likely to carry contact details; crawled before anything else.
DEFAULT_PRIORITY_PATHS = [
    "contact", "kontakt", "contacto", "contatti", "about", "about-us",
    "team", "our-team", "staff", "people", "management", "leadership",
    "impressum", "legal", "imprint", "support", "sales", "franchise",
    "locations", "offices", "directory", "members", "membership",
]


@dataclass
class NicheConfig:
    name: str = "unnamed niche"
    strong: list[str] = field(default_factory=list)
    medium: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    # Minimum page score for an email found on that page to be stored.
    page_threshold: float = 5.0
    # Minimum score for a domain (judged on its homepage) to be crawled at all.
    domain_threshold: float = 4.0

    @property
    def weights(self) -> list[tuple[list[str], float]]:
        return [
            (self.strong, 4.0),
            (self.medium, 1.5),
            (self.weak, 0.5),
            (self.negative, -6.0),
        ]


@dataclass
class SearchConfig:
    provider: str = "none"          # serper | brave | google_cse | none
    queries: list[str] = field(default_factory=list)
    per_query: int = 20
    api_key_env: str = "SERPER_API_KEY"
    cse_id_env: str = "GOOGLE_CSE_ID"

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


@dataclass
class DiscoveryConfig:
    search: SearchConfig = field(default_factory=SearchConfig)
    seeds: list[str] = field(default_factory=list)
    seed_file: str | None = None
    # Directory/listing pages: crawled wider, and outbound links are followed
    # off-domain (that is the whole point of a directory).
    directories: list[str] = field(default_factory=list)
    directory_max_pages: int = 60
    use_sitemaps: bool = True
    sitemap_max_urls: int = 300


@dataclass
class CrawlConfig:
    concurrency: int = 8
    per_domain_delay: float = 1.5
    timeout: float = 20.0
    max_pages_per_domain: int = 40
    max_depth: int = 2
    max_total_pages: int = 20_000
    respect_robots: bool = True
    allow_subdomains: bool = True
    user_agent: str = DEFAULT_UA
    priority_paths: list[str] = field(default_factory=lambda: list(DEFAULT_PRIORITY_PATHS))


@dataclass
class FilterConfig:
    blocked_domains: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_DOMAINS))
    blocked_email_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCKED_EMAIL_PATTERNS)
    )
    # Keep info@/sales@ style addresses as well as personal ones.
    keep_role_addresses: bool = True
    # Drop emails whose domain differs from the site they were found on
    # (usually agency credits, stock photo licences, CMS vendors).
    require_matching_domain: bool = False


@dataclass
class Config:
    niche: NicheConfig = field(default_factory=NicheConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    database: str = "data/scrapper.db"
    path: Path | None = None

    @staticmethod
    def load(path: str | Path) -> "Config":
        p = Path(path)
        raw: dict[str, Any] = yaml.safe_load(p.read_text()) or {}

        niche = NicheConfig(**_subset(raw.get("niche", {}), NicheConfig))
        search = SearchConfig(**_subset(raw.get("discovery", {}).get("search", {}), SearchConfig))
        disc_raw = {k: v for k, v in raw.get("discovery", {}).items() if k != "search"}
        discovery = DiscoveryConfig(search=search, **_subset(disc_raw, DiscoveryConfig))
        crawl = CrawlConfig(**_subset(raw.get("crawl", {}), CrawlConfig))
        filters = FilterConfig(**_subset(raw.get("filters", {}), FilterConfig))
        # Block lists in YAML extend the built-in defaults rather than replacing them.
        filters.blocked_domains = _merge(filters.blocked_domains, DEFAULT_BLOCKED_DOMAINS)
        filters.blocked_email_patterns = _merge(
            filters.blocked_email_patterns, DEFAULT_BLOCKED_EMAIL_PATTERNS
        )

        cfg = Config(
            niche=niche,
            discovery=discovery,
            crawl=crawl,
            filters=filters,
            database=raw.get("database", "data/scrapper.db"),
            path=p,
        )

        if cfg.discovery.seed_file:
            seed_path = (p.parent / cfg.discovery.seed_file).resolve()
            if seed_path.exists():
                extra = [
                    line.strip()
                    for line in seed_path.read_text().splitlines()
                    if line.strip() and not line.startswith("#")
                ]
                cfg.discovery.seeds.extend(extra)
        return cfg


def _merge(configured: list[str], defaults: list[str]) -> list[str]:
    seen = {x.lower() for x in configured}
    return list(configured) + [d for d in defaults if d.lower() not in seen]


def _subset(data: dict[str, Any], cls) -> dict[str, Any]:
    """Ignore unknown YAML keys instead of blowing up on a typo."""
    known = {f for f in cls.__dataclass_fields__}
    return {k: v for k, v in (data or {}).items() if k in known}
