"""Outreach campaigns: pick an audience from the scrape, render, send, record.

Same shape as the crawler: a resumable queue in SQLite, a config file per campaign,
and hard gates before anything leaves the machine. Sending is dry-run unless you
pass --live.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import export as export_mod
from . import mailer
from .config import _subset
from .db import Store
from .suppress import Suppression

HOUR = 3600
DAY = 86400

# Consumer mail hosts. A contact at one of these is a business without its own
# domain, not a colleague of everyone else at that host - it must never be used
# as a business identity or shown back to the recipient.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.de", "yahoo.fr",
    "yahoo.it", "yahoo.es", "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de",
    "hotmail.it", "hotmail.es", "outlook.com", "outlook.de", "outlook.fr", "live.com",
    "live.co.uk", "live.nl", "msn.com", "icloud.com", "me.com", "aol.com", "gmx.de",
    "gmx.net", "gmx.at", "gmx.ch", "web.de", "t-online.de", "freenet.de", "arcor.de",
    "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "laposte.net", "neuf.fr", "bbox.fr",
    "libero.it", "virgilio.it", "alice.it", "tiscali.it", "tin.it", "terra.es",
    "telefonica.net", "telenet.be", "skynet.be", "proximus.be", "ziggo.nl", "xs4all.nl",
    "home.nl", "planet.nl", "kpnmail.nl", "hetnet.nl", "chello.nl", "bluewin.ch",
    "sunrise.ch", "eircom.net", "btinternet.com", "virginmedia.com", "sky.com",
    "talktalk.net", "seznam.cz", "wp.pl", "o2.pl", "interia.pl", "onet.pl", "yandex.ru",
    "mail.ru", "protonmail.com", "proton.me", "zoho.com", "hushmail.com",
}

# First-name tokens that are page furniture, not people. The name guesser reads
# nearby text, so directory pages yield "New Membership", "Gallery About",
# "Hedgerow Mana" - addressing someone as "Hi New," destroys the message.
NOT_A_NAME = {
    "new", "membership", "member", "gallery", "about", "home", "contact", "contacts",
    "management", "manager", "sires", "dams", "news", "login", "register", "search",
    "shop", "store", "services", "service", "products", "product", "team", "staff",
    "committee", "council", "society", "association", "club", "breed", "breeders",
    "sales", "sale", "events", "event", "info", "office", "admin", "secretary",
    "chairman", "president", "treasurer", "editor", "webmaster", "privacy", "terms",
    "cookie", "cookies", "menu", "read", "click", "view", "download", "welcome",
    "farm", "farms", "stud", "kennel", "cattery", "the", "and", "for", "our", "your",
}


def business_key(email: str, site_domain: str) -> str:
    """Identity used to enforce one-contact-per-business.

    Keyed on the *email* domain, not the site: a directory listing 160 breeders
    shares one site_domain but 160 businesses, while a chain's 66 branch addresses
    share one email domain and are genuinely one organisation. Consumer-host
    addresses are each their own business.
    """
    domain = email.split("@")[-1].lower()
    if domain in FREEMAIL:
        return email.lower()
    return domain or (site_domain or "").lower()


DOMAINISH = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.I)


def clean_business_name(page_title: str, site_domain: str = "", email: str = "") -> str:
    """A human business name to show the recipient, or '' for the template fallback.

    Never returns a domain. The message must not name any site but Occuin's, so a
    contact with no usable name is addressed as "your business" rather than
    "petstop.ie" - which reads as scraped, because it is.
    """
    title = (page_title or "").strip()
    # Page titles carry taglines: "McCartneys LLP | Simplifying The Path For..."
    for sep in ("|", " - ", " – ", " — ", " :: ", " • "):
        if sep in title:
            title = title.split(sep)[0].strip()
            break
    if not (2 <= len(title) <= 60):
        return ""
    if title.lower() in {"home", "welcome", "index", "untitled"}:
        return ""
    if DOMAINISH.match(title) or title.lower().startswith(("http://", "https://", "www.")):
        return ""                       # a title that is just the domain
    return title


def clean_first_name(name: str, email: str = "") -> str:
    """Return a usable first name, or '' so the template falls back to 'there'.

    The name guesser reads text near an address, so it invents plausible-looking
    people: "Hedgerow Mana" is indistinguishable from a real name by shape alone.
    The only reliable corroboration is the address itself - if the local part
    doesn't contain the name, we don't use it. Conservative on purpose: greeting
    a stranger by the wrong name is worse than not greeting them by name at all.
    """
    token = (name or "").strip().split()[:1]
    if not token:
        return ""
    first = token[0].strip(".,;:'\"")
    if len(first) < 2 or not first[0].isupper() or first.lower() in NOT_A_NAME:
        return ""
    if not all(c.isalpha() or c in "-'" for c in first):
        return ""
    local = email.split("@")[0].lower()
    if first.lower() not in local:
        return ""
    return first


@dataclass
class AudienceConfig:
    min_score: float = 0.0
    personal_only: bool = False
    matching_domain_only: bool = True   # an address on the site's own domain is the
                                        # one the business actually published
    # One contact per business by default: five people at the same practice getting
    # the same cold email is what gets a sending domain blocked.
    max_per_domain: int = 1
    # Skip anyone already queued or mailed by ANY campaign. Off would let a second
    # sending account mail people the first account already reached - the fastest
    # way to turn outreach into a complaint.
    allow_recontact: bool = False
    # Only contact businesses that appear to have no website at all - the ones a
    # free Occuin storefront actually helps. Evidence: the address is on a consumer
    # mail host AND no page was ever crawled on its domain.
    no_website_only: bool = False
    exclude_domains: list[str] = field(default_factory=list)
    limit: int = 0                      # 0 = no cap on audience size


@dataclass
class ThrottleConfig:
    delay_seconds: float = 20.0
    per_hour: int = 40
    per_day: int = 200
    max_per_run: int = 0                # 0 = until the queue or a cap runs out


@dataclass
class ArchiveConfig:
    """File a copy of every sent message under a Gmail label, over IMAP."""
    enabled: bool = True
    host: str = "imap.gmail.com"
    folder: str = "Occuin Outreach"


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    starttls: bool = True
    user_env: str = "SMTP_USER"
    password_env: str = "SMTP_PASSWORD"


@dataclass
class MessageConfig:
    subject: str = ""
    text: str = ""
    html: str = ""
    text_file: str | None = None
    html_file: str | None = None


@dataclass
class CampaignConfig:
    id: str = "default"
    database: str = "data/scrapper.db"
    # The send queue lives outside the scrape DB and is SHARED by every campaign:
    # throttles protect one sending domain, so they must count across verticals.
    outreach_db: str = "data/outreach.db"
    suppression_db: str = "data/suppression.db"
    preview_dir: str = "data/previews"
    log_file: str = "data/sent.log"
    # Append-only JSON record of everyone contacted, one object per line so a
    # killed run cannot corrupt it. `mail sent` renders it as a JSON array.
    sent_json: str = "data/sent.jsonl"
    sender: mailer.Sender = field(
        default_factory=lambda: mailer.Sender(from_name="", from_email="")
    )
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    message: MessageConfig = field(default_factory=MessageConfig)
    audience: AudienceConfig = field(default_factory=AudienceConfig)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    extra: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @staticmethod
    def load(path: str | Path) -> "CampaignConfig":
        p = Path(path)
        raw: dict[str, Any] = yaml.safe_load(p.read_text()) or {}
        cfg = CampaignConfig(
            id=raw.get("id", p.stem),
            database=raw.get("database", "data/scrapper.db"),
            outreach_db=raw.get("outreach_db", "data/outreach.db"),
            suppression_db=raw.get("suppression_db", "data/suppression.db"),
            preview_dir=raw.get("preview_dir", "data/previews"),
            log_file=raw.get("log_file", "data/sent.log"),
            sent_json=raw.get("sent_json", "data/sent.jsonl"),
            sender=mailer.Sender(**_subset(raw.get("sender", {}), mailer.Sender)),
            smtp=SmtpConfig(**_subset(raw.get("smtp", {}), SmtpConfig)),
            archive=ArchiveConfig(**_subset(raw.get("archive", {}), ArchiveConfig)),
            message=MessageConfig(**_subset(raw.get("message", {}), MessageConfig)),
            audience=AudienceConfig(**_subset(raw.get("audience", {}), AudienceConfig)),
            throttle=ThrottleConfig(**_subset(raw.get("throttle", {}), ThrottleConfig)),
            extra={k: str(v) for k, v in (raw.get("extra") or {}).items()},
            path=p,
        )
        # Bodies may live in separate files next to the config - easier to edit.
        for attr, target in (("text_file", "text"), ("html_file", "html")):
            name = getattr(cfg.message, attr)
            if name:
                setattr(cfg.message, target, (p.parent / name).read_text(encoding="utf-8"))
        return cfg

    def resolve(self, value: str) -> Path:
        """Paths in the config are relative to the project root.

        Found by walking up from the config file, so `config/x.yaml` and
        `config/campaigns/x.yaml` both resolve `data/foo.db` to the same file
        rather than quietly creating an empty one beside the config.
        """
        path = Path(value)
        if path.is_absolute() or self.path is None:
            return path
        for parent in self.path.resolve().parents:
            if (parent / "scrapper").is_dir() or (parent / ".git").is_dir():
                return parent / path
        return Path.cwd() / path


# --------------------------------------------------------------- validation

# Verbatim strings from campaign.example.yaml. If any survive, the config was
# copied and not filled in - that must not reach a real recipient.
PLACEHOLDERS = (
    "Your Name", "you@yourcompany.com", "yourcompany.com",
    "Your Company Ltd", "<your one specific",
)


def validate(cfg: CampaignConfig) -> list[str]:
    """Everything that must be true before a single message may be sent.

    These are not style preferences: an identifiable sender, a working opt-out and
    a postal address are what CAN-SPAM and GDPR/PECR actually require of you.
    """
    problems: list[str] = []
    s, m = cfg.sender, cfg.message

    if not s.from_email or "@" not in s.from_email:
        problems.append("sender.from_email is missing or not an address")
    if not s.from_name:
        problems.append("sender.from_name is missing (the recipient must know who you are)")
    if not s.unsubscribe_url and not s.unsubscribe_mailto:
        problems.append("sender needs unsubscribe_url or unsubscribe_mailto")
    if not s.list_unsubscribe and not s.opt_out_instruction.strip():
        problems.append("with list_unsubscribe off, sender.opt_out_instruction must give "
                        "recipients a way out")
    if not s.postal_address and not s.omit_postal_address:
        problems.append("sender.postal_address is missing (required by CAN-SPAM). Set "
                        "sender.omit_postal_address: true to send without one anyway.")
    if not m.subject.strip():
        problems.append("message.subject is empty")
    if not m.text.strip():
        problems.append("message.text is empty (a plain-text part is always required)")

    known = allowed_variables(cfg)
    for label, tpl in (("subject", m.subject), ("text", m.text), ("html", m.html)):
        if not tpl:
            continue
        unknown = mailer.variables(tpl) - known
        if unknown:
            problems.append(f"message.{label} uses unknown variables: "
                            f"{', '.join(sorted(unknown))}")
    if not ({"unsubscribe", "opt_out"} & mailer.variables(m.text)):
        problems.append("message.text must include {{ unsubscribe }} or {{ opt_out }}")
    if "opt_out" in mailer.variables(m.text) and not s.opt_out_instruction.strip():
        problems.append("message.text uses {{ opt_out }} but sender.opt_out_instruction is empty")
    if m.html and not ({"unsubscribe", "opt_out"} & mailer.variables(m.html)):
        problems.append("message.html must include {{ unsubscribe }} or {{ opt_out }}")
    if "postal_address" not in mailer.variables(m.text) and not s.omit_postal_address:
        problems.append("message.text must include {{ postal_address }}")

    blob = " ".join([s.from_name, s.from_email, s.reply_to, s.postal_address,
                     s.unsubscribe_url, s.unsubscribe_mailto, m.subject, m.text, m.html])
    left = sorted({p for p in PLACEHOLDERS if p in blob})
    if left:
        problems.append("config still contains example placeholders: " + ", ".join(left))

    for name in ("database", "outreach_db"):
        if cfg.resolve(cfg.suppression_db) == cfg.resolve(getattr(cfg, name)):
            problems.append(f"suppression_db must be a different file from {name} "
                            "(reset deletes those; unsubscribes are permanent)")
    if cfg.throttle.per_hour <= 0 or cfg.throttle.per_day <= 0:
        problems.append("throttle.per_hour and throttle.per_day must be positive")
    return problems


def allowed_variables(cfg: CampaignConfig) -> set[str]:
    return {
        # No site_domain / page_title: both are the recipient's own site, and a
        # message must never name a site but ours. Use {{ business }}, which is
        # sanitised to a human name or nothing.
        "name", "first_name", "email", "business",
        "unsubscribe", "opt_out", "sender_name", "from_email", "postal_address",
    } | set(cfg.extra)


def recipient_values(cfg: CampaignConfig, row: dict) -> dict[str, str]:
    name = (row.get("name") or "").strip()
    first = clean_first_name(name, row["email"])
    return {
        **cfg.extra,
        # Blank the whole name too when the first token was page furniture -
        # "Hi New Membership," is no better than "Hi New,".
        "name": name if first else "",
        "first_name": first,
        "business": clean_business_name(
            row.get("page_title") or "", row.get("site_domain") or "", row["email"]
        ),
        "email": row["email"],
        "site_domain": row.get("site_domain") or "",
        "page_title": row.get("page_title") or "",
        "sender_name": cfg.sender.from_name,
        "from_email": cfg.sender.from_email,
        "postal_address": cfg.sender.postal_address,
        "opt_out": cfg.sender.opt_out_instruction,
        "unsubscribe": mailer.unsubscribe_link(
            row["email"], cfg.sender.unsubscribe_url, cfg.sender.unsubscribe_mailto
        ),
    }


# Hosts allowed to appear in a rendered message. Anything else - a scraped
# business domain leaking through a title, a competitor named in copy - means the
# message advertises someone else's site.
ALLOWED_HOSTS = ("occuin.com",)

FOREIGN_HOST_RE = re.compile(
    r"\b(?:https?://|www\.)?([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|"
    r"uk|ie|nl|be|de|fr|es|it|pt|pl|se|dk|no|fi|at|ch|cz|sk|hu|ro|gr|lu|eu))\b", re.I)


# Addresses and mailto: links legitimately contain the recipient's own domain
# (the opt-out link is built from their address) and the sender's. Strip those
# before looking for hosts we would be advertising.
ADDRESS_RE = re.compile(r"(?:mailto:)?[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]+", re.I)


def foreign_hosts(text: str) -> set[str]:
    """Hostnames in the rendered text that are not ours."""
    text = ADDRESS_RE.sub(" ", text or "")
    found = set()
    for host in FOREIGN_HOST_RE.findall(text):
        h = host.lower().lstrip("www.")
        if not any(h == a or h.endswith("." + a) for a in ALLOWED_HOSTS):
            found.add(h)
    return found


def render_for(cfg: CampaignConfig, row: dict) -> tuple[str, str, str]:
    v = recipient_values(cfg, row)
    subject = mailer.render(cfg.message.subject, v)
    text = mailer.render(cfg.message.text, v)
    html = mailer.render(cfg.message.html, v) if cfg.message.html else ""
    leaked = foreign_hosts(subject) | foreign_hosts(text) | foreign_hosts(html)
    if leaked:
        raise mailer.TemplateError(
            f"message for {row.get('email')} names a site that is not ours: "
            f"{', '.join(sorted(leaked))}"
        )
    return subject, text, html


# ----------------------------------------------------------------- audience

def build(cfg: CampaignConfig, store: Store, out: Store, sup: Suppression) -> dict[str, int]:
    """Select recipients from the scrape (`store`) and queue them in the shared
    outreach DB (`out`). Idempotent: rerun after a bigger crawl and only genuinely
    new contacts are added."""
    rows = export_mod.query(
        store,
        min_score=cfg.audience.min_score,
        personal_only=cfg.audience.personal_only,
        matching_domain_only=cfg.audience.matching_domain_only,
        limit=None,
    )
    excluded = {d.lower() for d in cfg.audience.exclude_domains}
    crawled = set()
    if cfg.audience.no_website_only:
        crawled = {r["domain"] for r in store.conn.execute("SELECT DISTINCT domain FROM pages")}
    per_domain: dict[str, int] = {}
    for r in out.queued_identities(cfg.id):
        k = business_key(r["email"], r["site_domain"] or "")
        per_domain[k] = per_domain.get(k, 0) + 1
    stats = {"queued": 0, "suppressed": 0, "domain_capped": 0,
             "excluded": 0, "already": 0, "has_website": 0, "other_campaign": 0}

    for row in rows:
        if cfg.audience.limit and stats["queued"] >= cfg.audience.limit:
            break
        email = row["email"]
        domain = (row.get("site_domain") or "").lower()
        if domain in excluded or (row.get("email_domain") or "").lower() in excluded:
            stats["excluded"] += 1
            continue
        if cfg.audience.no_website_only and not _looks_websiteless(row, crawled):
            stats["has_website"] += 1
            continue
        if not cfg.audience.allow_recontact:
            other = out.already_contacted(email)
            if other and other != cfg.id:
                stats["other_campaign"] += 1
                continue
        if sup.blocks(email):
            stats["suppressed"] += 1
            continue
        key = business_key(email, domain)
        if cfg.audience.max_per_domain and key:
            if per_domain.get(key, 0) >= cfg.audience.max_per_domain:
                stats["domain_capped"] += 1
                continue
        if out.queue_message(cfg.id, email, row.get("name") or "", domain):
            stats["queued"] += 1
            per_domain[key] = per_domain.get(key, 0) + 1
        else:
            stats["already"] += 1
    out.commit()
    return stats


def _looks_websiteless(row: dict, crawled: set[str]) -> bool:
    """A business with no site of its own.

    Two independent signals, both required. `has_website` is authoritative when the
    scrape recorded it; the consumer-mail-host test rules out businesses that own a
    domain but whose website simply was not tagged in OSM - those almost always do
    have a site, and offering them a free one reads as careless.
    """
    if row.get("has_website"):
        return False
    if (row.get("email", "").split("@")[-1].lower()) not in FREEMAIL:
        return False
    return (row.get("site_domain") or "").lower() not in crawled


# --------------------------------------------------------------- send loop

def remaining_allowance(cfg: CampaignConfig, out: Store) -> tuple[int, int]:
    """Budget left for THIS campaign's sending account.

    Per account, not per campaign: two campaigns on one mailbox share its budget,
    and two mailboxes each get their own.
    """
    now = int(time.time())
    account = cfg.sender.from_email
    hourly = cfg.throttle.per_hour - out.sent_since(now - HOUR, account)
    daily = cfg.throttle.per_day - out.sent_since(now - DAY, account)
    return max(0, hourly), max(0, daily)


def send(cfg: CampaignConfig, store: Store, out: Store, sup: Suppression, transport,
         limit: int = 0, sleep=time.sleep, on_send=None, record: bool = True,
         archive=None) -> dict[str, int]:
    """Drain the pending queue within the throttle caps.

    Any address suppressed since `build` ran is re-checked here, so an unsubscribe
    that arrives mid-campaign is honoured on the very next message.

    `record=False` (dry runs) renders and hands off to the transport but leaves the
    queue untouched: a dry run must never consume the messages, or the live send
    that follows would deliver nothing and still report success.
    """
    hourly, daily = remaining_allowance(cfg, out)
    if record:
        budget = min(hourly, daily)
        for cap in (limit, cfg.throttle.max_per_run):
            if cap:
                budget = min(budget, cap)
    else:
        # A dry run writes files: it neither spends the sending allowance nor needs
        # the pacing, so it renders the whole queue and you review the batch at once.
        budget = limit or out.message_counts(cfg.id).get("pending", 0)

    stats = {"sent": 0, "suppressed": 0, "failed": 0, "archived": 0, "stopped": ""}
    if budget <= 0:
        stats["stopped"] = "hourly" if hourly <= 0 else "daily"
        return stats

    log = cfg.resolve(cfg.log_file)
    log.parent.mkdir(parents=True, exist_ok=True)

    first = True
    for row in out.pending_messages(cfg.id, budget):
        rec = dict(row)
        email = rec["email"]

        reason = sup.blocks(email)
        if reason:
            if record:
                out.finish_message(cfg.id, email, "skipped", error=f"suppressed:{reason}")
            stats["suppressed"] += 1
            continue

        full = _enrich(store, rec)          # scrape row + queue row
        subject, text, html = render_for(cfg, full)
        msg = mailer.build_message(cfg.sender, email, rec.get("name") or "",
                                   subject, text, html)
        if record and not first and cfg.throttle.delay_seconds > 0:
            sleep(cfg.throttle.delay_seconds)
        first = False

        try:
            transport.send(msg)
        except Exception as exc:                       # noqa: BLE001 - recorded, not raised
            if record:
                out.finish_message(cfg.id, email, "failed", subject, str(exc),
                                   sender=cfg.sender.from_email)
                if mailer.is_permanent_failure(exc):
                    sup.add(email, "bounce", f"{cfg.id}: {exc}")
                out.commit()
            stats["failed"] += 1
            continue

        stats["sent"] += 1
        if record:
            out.finish_message(cfg.id, email, "sent", subject,
                               sender=cfg.sender.from_email)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"{int(time.time())}\t{cfg.id}\t{email}\t{subject}\n")
            _append_sent_json(cfg, full, subject)
            out.commit()
            if archive is not None and archive.store(msg):
                stats["archived"] += 1
        if on_send:
            on_send(email, stats["sent"], budget)

    if record and stats["sent"] >= budget and budget in (hourly, daily):
        stats["stopped"] = "hourly" if budget == hourly else "daily"
    return stats


def _append_sent_json(cfg: CampaignConfig, rec: dict, subject: str) -> None:
    """One JSON object per contacted business, appended as it happens."""
    path = cfg.resolve(cfg.sent_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "email": rec["email"],
        "business": clean_business_name(rec.get("page_title") or ""),
        "campaign": cfg.id,
        "sent_from": cfg.sender.from_email,
        "subject": subject,
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_url": rec.get("source_url") or "",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _enrich(store: Store, rec: dict) -> dict:
    """Pull the scrape row back in so templates can use page_title etc."""
    row = store.conn.execute(
        "SELECT * FROM emails WHERE email=?", (rec["email"],)
    ).fetchone()
    return {**(dict(row) if row else {}), **{k: v for k, v in rec.items() if v}}
