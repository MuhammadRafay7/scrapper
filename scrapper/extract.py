"""HTML -> text, links, and email addresses (including common obfuscations)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)

# " name (at) example (dot) com ", "[at]", "{ at }", " AT ", "&#64;"
AT_RE = re.compile(r"\s*(?:[\(\[\{]\s*(?:at|@)\s*[\)\]\}]|&#0?64;|&commat;|\s+at\s+)\s*", re.I)
DOT_RE = re.compile(r"\s*(?:[\(\[\{]\s*(?:dot|punkt|punto|\.)\s*[\)\]\}]|&#0?46;|\s+dot\s+)\s*", re.I)

ROLE_LOCALS = {
    "info", "contact", "hello", "hi", "sales", "support", "admin", "office",
    "enquiries", "enquiry", "inquiries", "help", "team", "mail", "email",
    "service", "services", "customerservice", "bookings", "booking", "orders",
    "press", "media", "marketing", "hr", "jobs", "careers", "recruitment",
    "accounts", "accounting", "billing", "finance", "invoice", "kontakt",
    "general", "reception", "welcome", "ask", "connect", "partners",
}

NAME_RE = re.compile(r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-zA-Z'\-]{1,20})\b")

DROP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe")


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[str]
    raw: str


def parse(html: str, url: str) -> Page:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""

    meta_bits = []
    for name in ("description", "keywords", "og:description", "og:site_name"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            meta_bits.append(tag["content"])

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s{2,}", " ", " ".join([title, *meta_bits, text]))

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.lower().startswith(("javascript:", "#", "tel:", "sms:")):
            links.append(href)

    return Page(url=url, title=title, text=text, links=links, raw=html)


def _decode_cfemail(hexstr: str) -> str | None:
    """Cloudflare email protection: XOR-encoded hex, first byte is the key."""
    try:
        data = bytes.fromhex(hexstr)
        key = data[0]
        return "".join(chr(b ^ key) for b in data[1:])
    except (ValueError, IndexError):
        return None


def _deobfuscate(text: str) -> str:
    text = AT_RE.sub("@", text)
    text = DOT_RE.sub(".", text)
    return text


def _clean(email: str) -> str | None:
    email = email.strip().strip(".,;:()<>[]\"'").lower()
    if email.count("@") != 1:
        return None
    local, _, domain = email.partition("@")
    local = local.strip(".")
    if not local or not domain or ".." in email:
        return None
    if len(email) > 254 or len(local) > 64:
        return None
    tld = domain.rsplit(".", 1)[-1]
    if not tld.isalpha() or not (2 <= len(tld) <= 24):
        return None
    return f"{local}@{domain}"


@dataclass
class FoundEmail:
    email: str
    local_part: str
    email_domain: str
    kind: str
    name: str | None
    context: str


def find_emails(page: Page) -> list[FoundEmail]:
    candidates: dict[str, str] = {}  # email -> surrounding context

    # 1. mailto: links (highest confidence)
    for m in re.finditer(r"mailto:([^\"'?>\s]+)", page.raw, re.I):
        cleaned = _clean(_deobfuscate(m.group(1)))
        if cleaned:
            candidates.setdefault(cleaned, _context(page.text, cleaned.split("@")[0]))

    # 2. Cloudflare-protected addresses
    for m in re.finditer(r"data-cfemail=\"([0-9a-fA-F]+)\"", page.raw):
        decoded = _decode_cfemail(m.group(1))
        cleaned = _clean(decoded) if decoded else None
        if cleaned:
            candidates.setdefault(cleaned, _context(page.text, cleaned.split("@")[0]))

    # 3. Plain and obfuscated text
    text = _deobfuscate(page.text)
    for m in EMAIL_RE.finditer(text):
        cleaned = _clean(m.group(0))
        if cleaned:
            start = max(0, m.start() - 120)
            candidates.setdefault(cleaned, text[start : m.end() + 60].strip())

    out = []
    for email, context in candidates.items():
        local, _, domain = email.partition("@")
        base = local.split("+")[0]
        kind = "role" if base.replace(".", "").replace("-", "") in ROLE_LOCALS else "personal"
        out.append(
            FoundEmail(
                email=email,
                local_part=local,
                email_domain=domain,
                kind=kind,
                name=_guess_name(local, context),
                context=context[:400],
            )
        )
    return out


def _context(text: str, needle: str) -> str:
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return ""
    return text[max(0, idx - 120) : idx + 120].strip()


def _guess_name(local: str, context: str) -> str | None:
    """Prefer a real name near the address; fall back to first.last@ patterns."""
    for m in NAME_RE.finditer(context or ""):
        first, last = m.group(1), m.group(2)
        if first.lower() in ("contact", "email", "phone", "the", "our", "monday", "call"):
            continue
        return f"{first} {last}"
    parts = re.split(r"[._-]", local)
    if len(parts) == 2 and all(p.isalpha() and len(p) > 1 for p in parts):
        return f"{parts[0].capitalize()} {parts[1].capitalize()}"
    return None
