"""Offline tests for the outreach side: templating, gates, throttles, resumability."""

from __future__ import annotations

import smtplib
import time

import pytest

from scrapper import campaign as cm
from scrapper import mailer
from scrapper.db import Store
from scrapper.suppress import Suppression

BODY = """Hi {{ first_name|there }},

about {{ site_domain }} - {{ product }}.

{{ sender_name }}
--
{{ postal_address }}
Unsubscribe: {{ unsubscribe }}
"""


def make_cfg(tmp_path, **over) -> cm.CampaignConfig:
    cfg = cm.CampaignConfig(
        id="test",
        database=str(tmp_path / "scrape.db"),
        outreach_db=str(tmp_path / "outreach.db"),
        suppression_db=str(tmp_path / "suppress.db"),
        preview_dir=str(tmp_path / "previews"),
        log_file=str(tmp_path / "sent.log"),
        sender=mailer.Sender(
            from_name="Ada Byron",
            from_email="ada@example.com",
            reply_to="ada@example.com",
            postal_address="1 Example St, London",
            unsubscribe_mailto="unsub@example.com",
        ),
        message=cm.MessageConfig(subject="About {{ site_domain }}", text=BODY),
        extra={"product": "a widget"},
    )
    cfg.throttle.delay_seconds = 0
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def seed_store(tmp_path, emails) -> Store:
    store = Store(tmp_path / "scrape.db")
    for e in emails:
        store.add_email({
            "email": e["email"], "local_part": e["email"].split("@")[0],
            "email_domain": e["email"].split("@")[1],
            "site_domain": e.get("site_domain", e["email"].split("@")[1]),
            "kind": e.get("kind", "personal"), "name": e.get("name", ""),
            "context": "", "source_url": "https://x/", "page_title": "Contact",
            "score": e.get("score", 9.0),
        })
    store.commit()
    return store


# ----------------------------------------------------------------- templating

def test_render_with_fallback_and_extras():
    v = {"first_name": "", "site_domain": "acme.com", "product": "a widget",
         "sender_name": "Ada", "postal_address": "1 St", "unsubscribe": "mailto:x"}
    out = mailer.render(BODY, v)
    assert out.startswith("Hi there,")          # fallback used for the empty name
    assert "about acme.com - a widget." in out


def test_unknown_variable_raises_instead_of_shipping():
    with pytest.raises(mailer.TemplateError):
        mailer.render("Hi {{ nmae }}", {"name": "Ada"})


def test_unsubscribe_link_forms():
    url = mailer.unsubscribe_link("a+b@x.com", url="https://y.com/u?k=1")
    assert url == "https://y.com/u?k=1&e=a%2Bb%40x.com"
    assert mailer.unsubscribe_link("a@x.com", mailto="u@y.com").startswith("mailto:u@y.com?")


def test_message_carries_required_headers(tmp_path):
    cfg = make_cfg(tmp_path)
    subject, text, _ = cm.render_for(cfg, {"email": "bob@acme.com", "name": "Bob Smith",
                                           "site_domain": "acme.com"})
    msg = mailer.build_message(cfg.sender, "bob@acme.com", "Bob Smith", subject, text)
    assert msg["Subject"] == "About acme.com"
    assert msg["List-Unsubscribe"].startswith("<mailto:unsub@example.com")
    assert msg["Reply-To"] == "ada@example.com"
    assert msg["Message-ID"] and msg["Date"]
    assert "1 Example St, London" in msg.get_content()


# ------------------------------------------------------------------ validation

def test_valid_campaign_passes(tmp_path):
    assert cm.validate(make_cfg(tmp_path)) == []


@pytest.mark.parametrize("mutate,fragment", [
    (lambda c: setattr(c.sender, "unsubscribe_mailto", ""), "unsubscribe_url"),
    (lambda c: setattr(c.sender, "postal_address", ""), "postal_address is missing"),
    (lambda c: setattr(c.sender, "from_name", ""), "from_name"),
    (lambda c: setattr(c.message, "text", "Hi, no opt out. {{ postal_address }}"),
     "must include {{ unsubscribe }}"),
    (lambda c: setattr(c.message, "subject", "Hi {{ nmae }}"), "unknown variables"),
])
def test_missing_compliance_requirement_blocks_sending(tmp_path, mutate, fragment):
    cfg = make_cfg(tmp_path)
    mutate(cfg)
    assert any(fragment in p for p in cm.validate(cfg))


def test_suppression_db_may_not_be_the_scrape_db(tmp_path):
    cfg = make_cfg(tmp_path, suppression_db=str(tmp_path / "scrape.db"))
    assert any("suppression_db must be a different file" in p for p in cm.validate(cfg))


# -------------------------------------------------------------------- audience

def test_build_respects_suppression_and_one_per_domain(tmp_path):
    store = seed_store(tmp_path, [
        {"email": "a@acme.com", "site_domain": "acme.com", "score": 9},
        {"email": "b@acme.com", "site_domain": "acme.com", "score": 8},
        {"email": "c@other.com", "site_domain": "other.com", "score": 7},
        {"email": "d@gone.com", "site_domain": "gone.com", "score": 7},
    ])
    out = Store(tmp_path / "outreach.db")
    sup = Suppression(tmp_path / "suppress.db")
    sup.add("d@gone.com", "unsubscribe")
    cfg = make_cfg(tmp_path)

    s = cm.build(cfg, store, out, sup)
    assert s["queued"] == 2           # one per domain, and the unsubscribed one dropped
    assert s["suppressed"] == 1
    assert s["domain_capped"] == 1

    assert cm.build(cfg, store, out, sup)["queued"] == 0   # idempotent on a rerun
    store.close()
    out.close()
    sup.close()


def test_build_honours_min_score_and_exclusions(tmp_path):
    store = seed_store(tmp_path, [
        {"email": "a@acme.com", "site_domain": "acme.com", "score": 9},
        {"email": "b@weak.com", "site_domain": "weak.com", "score": 2},
        {"email": "c@skip.com", "site_domain": "skip.com", "score": 9},
    ])
    out = Store(tmp_path / "outreach.db")
    sup = Suppression(tmp_path / "suppress.db")
    cfg = make_cfg(tmp_path)
    cfg.audience.min_score = 5.0
    cfg.audience.exclude_domains = ["skip.com"]
    s = cm.build(cfg, store, out, sup)
    assert s["queued"] == 1 and s["excluded"] == 1
    store.close()
    out.close()
    sup.close()


# ------------------------------------------------------------------ send loop

class FakeTransport:
    def __init__(self, fail_on=None, exc=None):
        self.sent = []
        self.fail_on = fail_on
        self.exc = exc or RuntimeError("boom")

    def send(self, msg):
        if self.fail_on and self.fail_on in msg["To"]:
            raise self.exc
        self.sent.append(msg)


def prepared(tmp_path, n=5, **over):
    store = seed_store(tmp_path, [
        {"email": f"p{i}@d{i}.com", "site_domain": f"d{i}.com", "name": f"P{i} Q"}
        for i in range(n)
    ])
    out = Store(tmp_path / "outreach.db")
    sup = Suppression(tmp_path / "suppress.db")
    cfg = make_cfg(tmp_path, **over)
    cm.build(cfg, store, out, sup)
    return cfg, store, out, sup


def test_send_delivers_and_marks_sent(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 3)
    t = FakeTransport()
    s = cm.send(cfg, store, out, sup, t)
    assert s["sent"] == 3 and len(t.sent) == 3
    assert out.message_counts(cfg.id) == {"sent": 3}
    assert cm.send(cfg, store, out, sup, FakeTransport())["sent"] == 0   # queue drained
    store.close()
    out.close()
    sup.close()


def test_hourly_cap_stops_the_run_and_resumes_later(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 5)
    cfg.throttle.per_hour = 2
    s = cm.send(cfg, store, out, sup, FakeTransport())
    assert s["sent"] == 2 and s["stopped"] == "hourly"
    assert out.message_counts(cfg.id)["pending"] == 3

    # ...an hour passes: the earlier sends age out of the window
    out.conn.execute("UPDATE messages SET sent_at=? WHERE status='sent'",
                     (int(time.time()) - cm.HOUR - 1,))
    out.commit()
    assert cm.send(cfg, store, out, sup, FakeTransport())["sent"] == 2
    store.close()
    out.close()
    sup.close()


def test_unsubscribe_between_build_and_send_is_honoured(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 3)
    sup.add("p1@d1.com", "unsubscribe")           # arrives after the queue was built
    t = FakeTransport()
    s = cm.send(cfg, store, out, sup, t)
    assert s["sent"] == 2 and s["suppressed"] == 1
    assert not any("p1@d1.com" in m["To"] for m in t.sent)
    store.close()
    out.close()
    sup.close()


def test_hard_bounce_is_auto_suppressed(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 2)
    exc = smtplib.SMTPRecipientsRefused({"p0@d0.com": (550, b"no such user")})
    s = cm.send(cfg, store, out, sup, FakeTransport(fail_on="p0@d0.com", exc=exc))
    assert s["failed"] == 1 and s["sent"] == 1
    assert sup.blocks("p0@d0.com") == "bounce"
    store.close()
    out.close()
    sup.close()


def test_transient_failure_is_not_suppressed(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 1)
    exc = smtplib.SMTPRecipientsRefused({"p0@d0.com": (451, b"try later")})
    cm.send(cfg, store, out, sup, FakeTransport(fail_on="p0@d0.com", exc=exc))
    assert sup.blocks("p0@d0.com") is None
    store.close()
    out.close()
    sup.close()


def test_dry_run_writes_eml_and_sends_nothing(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 2)
    with mailer.DryRun(cfg.resolve(cfg.preview_dir)) as t:
        s = cm.send(cfg, store, out, sup, t)
    files = list((tmp_path / "previews").glob("*.eml"))
    assert s["sent"] == 2 and len(files) == 2
    assert "List-Unsubscribe" in files[0].read_text()
    store.close()
    out.close()
    sup.close()


def test_sends_are_logged(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 2)
    cm.send(cfg, store, out, sup, FakeTransport())
    lines = (tmp_path / "sent.log").read_text().strip().splitlines()
    assert len(lines) == 2 and "test\tp0@d0.com" in lines[0]
    store.close()
    out.close()
    sup.close()


def test_throttle_delay_is_applied_between_messages(tmp_path):
    cfg, store, out, sup = prepared(tmp_path, 3)
    cfg.throttle.delay_seconds = 20
    slept = []
    cm.send(cfg, store, out, sup, FakeTransport(), sleep=slept.append)
    assert slept == [20, 20]          # no wait before the first message
    store.close()
    out.close()
    sup.close()


# ----------------------------------------------------------------- suppression

def test_domain_wide_suppression(tmp_path):
    sup = Suppression(tmp_path / "s.db")
    sup.add("@blocked.com", "complaint")
    assert sup.blocks("anyone@blocked.com") == "complaint"
    assert sup.blocks("someone@fine.com") is None
    sup.close()


def test_import_csv_and_txt(tmp_path):
    csv_path = tmp_path / "unsubs.csv"
    csv_path.write_text("email,when\nA@X.com,today\nb@y.com,today\n")
    txt_path = tmp_path / "unsubs.txt"
    txt_path.write_text("# exported\nc@z.com\n")
    sup = Suppression(tmp_path / "s.db")
    assert sup.import_file(csv_path) == 2
    assert sup.import_file(txt_path) == 1
    assert sup.blocks("a@x.com") == "unsubscribe"     # case-insensitive
    sup.close()


def test_nested_config_resolves_paths_to_project_root(tmp_path):
    """A campaign at config/campaigns/x.yaml must find data/ at the project root,
    not create an empty DB beside itself."""
    root = tmp_path / "proj"
    (root / "scrapper").mkdir(parents=True)
    (root / "config" / "campaigns").mkdir(parents=True)
    cfg_path = root / "config" / "campaigns" / "c.yaml"
    cfg_path.write_text("id: x\ndatabase: data/scrape.db\n")
    cfg = cm.CampaignConfig.load(cfg_path)
    assert cfg.resolve(cfg.database) == root / "data" / "scrape.db"


def test_no_auto_submitted_or_bulk_headers(tmp_path):
    """Gmail dropped messages carrying these outright; List-Unsubscribe is the
    correct bulk signal and must stay."""
    cfg = make_cfg(tmp_path)
    msg = mailer.build_message(cfg.sender, "bob@acme.com", "Bob", "Hi", "body")
    assert msg["Auto-Submitted"] is None
    assert msg["Precedence"] is None
    assert msg["List-Unsubscribe"]
