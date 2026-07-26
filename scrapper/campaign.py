"""Outreach campaigns: pick an audience from the scrape, render, send, record.

Same shape as the crawler: a resumable queue in SQLite, a config file per campaign,
and hard gates before anything leaves the machine. Sending is dry-run unless you
pass --live.
"""

from __future__ import annotations

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


@dataclass
class AudienceConfig:
    min_score: float = 0.0
    personal_only: bool = False
    matching_domain_only: bool = True   # an address on the site's own domain is the
                                        # one the business actually published
    # One contact per business by default: five people at the same practice getting
    # the same cold email is what gets a sending domain blocked.
    max_per_domain: int = 1
    exclude_domains: list[str] = field(default_factory=list)
    limit: int = 0                      # 0 = no cap on audience size


@dataclass
class ThrottleConfig:
    delay_seconds: float = 20.0
    per_hour: int = 40
    per_day: int = 200
    max_per_run: int = 0                # 0 = until the queue or a cap runs out


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
    sender: mailer.Sender = field(
        default_factory=lambda: mailer.Sender(from_name="", from_email="")
    )
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
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
            sender=mailer.Sender(**_subset(raw.get("sender", {}), mailer.Sender)),
            smtp=SmtpConfig(**_subset(raw.get("smtp", {}), SmtpConfig)),
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
    if not s.postal_address:
        problems.append("sender.postal_address is missing (required by CAN-SPAM)")
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
    if "unsubscribe" not in mailer.variables(m.text):
        problems.append("message.text must include {{ unsubscribe }}")
    if m.html and "unsubscribe" not in mailer.variables(m.html):
        problems.append("message.html must include {{ unsubscribe }}")
    if "postal_address" not in mailer.variables(m.text):
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
        "name", "first_name", "email", "site_domain", "page_title",
        "unsubscribe", "sender_name", "from_email", "postal_address",
    } | set(cfg.extra)


def recipient_values(cfg: CampaignConfig, row: dict) -> dict[str, str]:
    name = (row.get("name") or "").strip()
    return {
        **cfg.extra,
        "name": name,
        "first_name": name.split()[0] if name else "",
        "email": row["email"],
        "site_domain": row.get("site_domain") or "",
        "page_title": row.get("page_title") or "",
        "sender_name": cfg.sender.from_name,
        "from_email": cfg.sender.from_email,
        "postal_address": cfg.sender.postal_address,
        "unsubscribe": mailer.unsubscribe_link(
            row["email"], cfg.sender.unsubscribe_url, cfg.sender.unsubscribe_mailto
        ),
    }


def render_for(cfg: CampaignConfig, row: dict) -> tuple[str, str, str]:
    v = recipient_values(cfg, row)
    return (
        mailer.render(cfg.message.subject, v),
        mailer.render(cfg.message.text, v),
        mailer.render(cfg.message.html, v) if cfg.message.html else "",
    )


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
    per_domain = out.domains_already_queued(cfg.id)
    stats = {"queued": 0, "suppressed": 0, "domain_capped": 0,
             "excluded": 0, "already": 0}

    for row in rows:
        if cfg.audience.limit and stats["queued"] >= cfg.audience.limit:
            break
        email = row["email"]
        domain = (row.get("site_domain") or "").lower()
        if domain in excluded or (row.get("email_domain") or "").lower() in excluded:
            stats["excluded"] += 1
            continue
        if sup.blocks(email):
            stats["suppressed"] += 1
            continue
        if cfg.audience.max_per_domain and domain:
            if per_domain.get(domain, 0) >= cfg.audience.max_per_domain:
                stats["domain_capped"] += 1
                continue
        if out.queue_message(cfg.id, email, row.get("name") or "", domain):
            stats["queued"] += 1
            per_domain[domain] = per_domain.get(domain, 0) + 1
        else:
            stats["already"] += 1
    out.commit()
    return stats


# --------------------------------------------------------------- send loop

def remaining_allowance(cfg: CampaignConfig, out: Store) -> tuple[int, int]:
    """Counted over every campaign in the shared outreach DB, not just this one."""
    now = int(time.time())
    hourly = cfg.throttle.per_hour - out.sent_since(now - HOUR)
    daily = cfg.throttle.per_day - out.sent_since(now - DAY)
    return max(0, hourly), max(0, daily)


def send(cfg: CampaignConfig, store: Store, out: Store, sup: Suppression, transport,
         limit: int = 0, sleep=time.sleep, on_send=None) -> dict[str, int]:
    """Drain the pending queue within the throttle caps.

    Any address suppressed since `build` ran is re-checked here, so an unsubscribe
    that arrives mid-campaign is honoured on the very next message.
    """
    hourly, daily = remaining_allowance(cfg, out)
    budget = min(hourly, daily)
    for cap in (limit, cfg.throttle.max_per_run):
        if cap:
            budget = min(budget, cap)

    stats = {"sent": 0, "suppressed": 0, "failed": 0, "stopped": ""}
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
            out.finish_message(cfg.id, email, "skipped", error=f"suppressed:{reason}")
            stats["suppressed"] += 1
            continue

        subject, text, html = render_for(cfg, _enrich(store, rec))
        msg = mailer.build_message(cfg.sender, email, rec.get("name") or "",
                                   subject, text, html)
        if not first and cfg.throttle.delay_seconds > 0:
            sleep(cfg.throttle.delay_seconds)
        first = False

        try:
            transport.send(msg)
        except Exception as exc:                       # noqa: BLE001 - recorded, not raised
            out.finish_message(cfg.id, email, "failed", subject, str(exc))
            stats["failed"] += 1
            if mailer.is_permanent_failure(exc):
                sup.add(email, "bounce", f"{cfg.id}: {exc}")
            out.commit()
            continue

        out.finish_message(cfg.id, email, "sent", subject)
        stats["sent"] += 1
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{int(time.time())}\t{cfg.id}\t{email}\t{subject}\n")
        out.commit()
        if on_send:
            on_send(email, stats["sent"], budget)

    if stats["sent"] >= budget and budget in (hourly, daily):
        stats["stopped"] = "hourly" if budget == hourly else "daily"
    return stats


def _enrich(store: Store, rec: dict) -> dict:
    """Pull the scrape row back in so templates can use page_title etc."""
    row = store.conn.execute(
        "SELECT * FROM emails WHERE email=?", (rec["email"],)
    ).fetchone()
    return {**(dict(row) if row else {}), **{k: v for k, v in rec.items() if v}}
