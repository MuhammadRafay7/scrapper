"""Polite async fetching: robots.txt, per-domain rate limiting, size caps."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse, urljoin
from urllib.robotparser import RobotFileParser

import httpx
import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())  # offline snapshot, no network on import


def registrable_domain(url_or_host: str) -> str:
    host = url_or_host
    if "://" in url_or_host:
        host = urlparse(url_or_host).netloc
    host = host.split("@")[-1].split(":")[0].lower()
    ext = _EXTRACT(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Absolutise, strip fragments/tracking params, reject non-http schemes."""
    if not url:
        return None
    url = url.strip()
    if base:
        url = urljoin(base, url)
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    query = "&".join(
        part for part in p.query.split("&")
        if part and not part.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid", "mc_"))
    )
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".mp4", ".mp3",
    ".zip", ".gz", ".rar", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf", ".dmg", ".exe",
)


def looks_fetchable(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not path.endswith(SKIP_EXTENSIONS)


@dataclass
class Response:
    url: str
    final_url: str = ""
    status: int = 0
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)


class Fetcher:
    def __init__(self, *, concurrency: int = 8, per_domain_delay: float = 1.5,
                 timeout: float = 20.0, user_agent: str = "NicheContactBot/1.0",
                 respect_robots: bool = True, max_bytes: int = 3_000_000):
        self.sem = asyncio.Semaphore(concurrency)
        self.per_domain_delay = per_domain_delay
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.max_bytes = max_bytes
        self._last_hit: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def _lock(self, host: str) -> asyncio.Lock:
        return self._domain_locks.setdefault(host, asyncio.Lock())

    async def _throttle(self, host: str) -> None:
        async with self._lock(host):
            wait = self.per_domain_delay - (time.monotonic() - self._last_hit.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        host = p.netloc.lower()
        lock = self._robots_locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host not in self._robots:
                self._robots[host] = await self._load_robots(f"{p.scheme}://{host}/robots.txt")
        rp = self._robots[host]
        if rp is None:
            return True  # no robots.txt served -> unrestricted
        return rp.can_fetch(self.user_agent, url)

    async def _load_robots(self, robots_url: str) -> RobotFileParser | None:
        try:
            r = await self.client.get(robots_url, timeout=10.0)
        except Exception:
            return None
        if r.status_code != 200 or not r.text.strip():
            return None
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp

    async def get(self, url: str) -> Response:
        host = urlparse(url).netloc.lower()
        try:
            if not await self.allowed(url):
                return Response(url, error="blocked by robots.txt")
        except Exception as e:  # robots fetch should never kill the crawl
            return Response(url, error=f"robots error: {e}")

        async with self.sem:
            await self._throttle(host)
            try:
                r = await self.client.get(url)
            except Exception as e:
                return Response(url, error=f"{type(e).__name__}: {e}"[:200])

            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                return Response(url, str(r.url), r.status_code, error=f"content-type {ctype}")
            body = r.content[: self.max_bytes]
            try:
                text = body.decode(r.encoding or "utf-8", errors="replace")
            except (LookupError, TypeError):
                text = body.decode("utf-8", errors="replace")
            return Response(url, str(r.url), r.status_code, text)
