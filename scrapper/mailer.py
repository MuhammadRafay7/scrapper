"""Message rendering and delivery.

Two transports: `DryRun` (writes .eml files, the default) and `Smtp` (actually sends).
Nothing here reads the database - it renders and delivers one message at a time so the
campaign loop stays in charge of throttling, suppression and resumability.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import quote

# {{ name }} or {{ name|there }} - the part after | is used when the value is empty.
VAR_RE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*(?:\|([^}]*?))?\s*\}\}", re.I)


class TemplateError(ValueError):
    pass


def variables(template: str) -> set[str]:
    return {m.group(1) for m in VAR_RE.finditer(template)}


def render(template: str, values: dict[str, str]) -> str:
    """Substitute {{ var }} / {{ var|fallback }}. Unknown names raise, so a typo
    fails at preview time rather than going out to a thousand people."""
    def sub(m: re.Match) -> str:
        key, fallback = m.group(1), (m.group(2) or "")
        if key not in values:
            raise TemplateError(f"unknown template variable {{{{ {key} }}}}")
        value = (values.get(key) or "").strip()
        return value or fallback.strip()

    return VAR_RE.sub(sub, template)


def unsubscribe_link(email: str, url: str = "", mailto: str = "") -> str:
    """A per-recipient opt-out. URL form carries the address so you can honour it
    without a lookup; mailto form is the zero-infrastructure fallback."""
    if url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}e={quote(email)}"
    if mailto:
        return f"mailto:{mailto}?subject=unsubscribe&body={quote(email)}"
    raise TemplateError("no unsubscribe_url or unsubscribe_mailto configured")


@dataclass
class Sender:
    # Empty defaults so a config missing `sender:` fails through validate() with a
    # readable message instead of a TypeError during load.
    from_name: str = ""
    from_email: str = ""
    reply_to: str = ""
    postal_address: str = ""       # CAN-SPAM requires a real one in the body
    # Shipping without a postal address is a deliberate, acknowledged choice - see
    # validate(). It does not remove the legal requirement, it only stops this tool
    # from blocking the send.
    omit_postal_address: bool = False
    unsubscribe_url: str = ""
    unsubscribe_mailto: str = ""
    # Human-readable opt-out shown in the body, so the footer isn't a raw mailto:
    # URL. This is the opt-out mechanism recipients actually use.
    opt_out_instruction: str = "Reply with STOP and we won't contact you again."
    # The List-Unsubscribe header is the clearest "this is bulk" signal a message
    # can carry, and Gmail files bulk under Promotions. Turning it off is only
    # defensible because opt_out_instruction gives every recipient a working way
    # out; never run with both disabled.
    list_unsubscribe: bool = True


def build_message(sender: Sender, to_email: str, to_name: str, subject: str,
                  text: str, html: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = str(Address(sender.from_name, addr_spec=sender.from_email))
    msg["To"] = str(Address(to_name, addr_spec=to_email)) if to_name else to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.from_email.split("@")[-1])
    if sender.reply_to:
        msg["Reply-To"] = sender.reply_to

    if sender.list_unsubscribe:
        link = unsubscribe_link(to_email, sender.unsubscribe_url, sender.unsubscribe_mailto)
        msg["List-Unsubscribe"] = f"<{link}>"
        if link.startswith("http"):
            # RFC 8058: lets Gmail/Outlook show a native one-click unsubscribe.
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    # No Auto-Submitted / Precedence: bulk here on purpose. Auto-Submitted is
    # specified for machine notifications (bounces, vacation replies) and filters
    # treat outreach carrying it as suppressible; Precedence: bulk is legacy and
    # only costs placement. List-Unsubscribe above is the modern, correct signal
    # and is what receivers actually weigh. Measured: with those two headers Gmail
    # dropped the message outright, without them it reaches the inbox.

    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


class DryRun:
    """Writes each message to disk instead of sending it. The default transport."""

    def __init__(self, outdir: str | Path):
        self.dir = Path(outdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sent: list[EmailMessage] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, msg: EmailMessage) -> None:
        self.sent.append(msg)
        safe = re.sub(r"[^a-z0-9._-]+", "_", msg["To"].lower())
        (self.dir / f"{len(self.sent):04d}-{safe}.eml").write_bytes(msg.as_bytes())


class Smtp:
    """Real delivery. One connection reused for the whole run, reopened on drop."""

    def __init__(self, host: str, port: int = 587, user: str = "", password: str = "",
                 starttls: bool = True, timeout: float = 30.0):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.starttls, self.timeout = starttls, timeout
        self.conn: smtplib.SMTP | smtplib.SMTP_SSL | None = None

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _connect(self) -> None:
        ctx = ssl.create_default_context()
        if self.port == 465:
            self.conn = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            self.conn = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            if self.starttls:
                self.conn.starttls(context=ctx)
        if self.user:
            self.conn.login(self.user, self.password)

    def send(self, msg: EmailMessage) -> None:
        if self.conn is None:
            self._connect()
        try:
            self.conn.send_message(msg)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
            self._connect()                       # long runs outlive idle timeouts
            self.conn.send_message(msg)
        except smtplib.SMTPResponseException as exc:
            # 421 "Connection expired" - Gmail recycles long-lived connections.
            # Transient: reconnect once and resend rather than losing the message.
            if exc.smtp_code != 421:
                raise
            self.close()
            self._connect()
            self.conn.send_message(msg)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:
                pass
            self.conn = None


def is_permanent_failure(exc: Exception) -> bool:
    """5xx means the address is bad - suppress it. 4xx is transient, retry later."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return all(code >= 500 for code, _ in exc.recipients.values())
    code = getattr(exc, "smtp_code", None)
    return isinstance(code, int) and code >= 500


class ImapArchive:
    """Files a copy of each sent message under a Gmail label.

    Gmail already saves SMTP-sent mail to "Sent"; this puts it under a label of
    your own too, so outreach is one click to review and does not drown in the
    rest of the mailbox. Best-effort by design - the campaign records a send as
    successful the moment SMTP accepts it, and a failure to file a copy must never
    look like a failure to deliver.
    """

    def __init__(self, host: str, user: str, password: str, folder: str,
                 port: int = 993, timeout: float = 30.0):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.folder, self.timeout = folder, timeout
        self.conn = None
        self.errors = 0

    def __enter__(self):
        try:
            self._connect()
        except Exception:
            self.conn = None            # archiving is optional; sending is not
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _connect(self) -> None:
        import imaplib
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        self.conn.login(self.user, self.password)
        # CREATE fails harmlessly when the label already exists.
        self.conn.create(f'"{self.folder}"')

    def store(self, msg: EmailMessage) -> bool:
        if self.conn is None:
            return False
        try:
            self.conn.append(f'"{self.folder}"', r"(\Seen)", None, msg.as_bytes())
            return True
        except Exception:
            self.errors += 1
            return False

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None
