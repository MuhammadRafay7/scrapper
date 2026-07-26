"""Offline tests: extraction, scoring, and an end-to-end crawl of a local site."""

from __future__ import annotations

import asyncio
import functools
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from scrapper.config import Config
from scrapper.crawl import Crawler
from scrapper.db import Store
from scrapper.discover import register_candidates
from scrapper.extract import find_emails, parse
from scrapper.net import Fetcher, normalize_url, registrable_domain
from scrapper.score import score_page, score_text

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- extraction

def test_plain_and_obfuscated_emails():
    html = """
    <html><head><title>Contact</title></head><body>
      <a href="mailto:jane.doe@solarworks.co.uk">Email Jane</a>
      <p>Sales: sales@solarworks.co.uk</p>
      <p>Support: support (at) solarworks [dot] co.uk</p>
      <img src="logo@2x.png"> <span>noreply@solarworks.co.uk</span>
    </body></html>"""
    page = parse(html, "https://solarworks.co.uk/contact")
    found = {e.email for e in find_emails(page)}
    assert "jane.doe@solarworks.co.uk" in found
    assert "sales@solarworks.co.uk" in found
    assert "support@solarworks.co.uk" in found     # de-obfuscated
    assert not any(".png" in e for e in found)     # image asset not an email


def test_cloudflare_protected_email():
    # "info@example.com" XOR-encoded with key 0x7a
    plain = "info@example.com"
    key = 0x7A
    hexed = format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in plain)
    page = parse(f'<a href="/cdn-cgi/l/email-protection" data-cfemail="{hexed}">x</a>', "u")
    assert any(e.email == plain for e in find_emails(page))


def test_role_vs_personal_and_name_guess():
    page = parse(
        "<p>Dr. Sarah Mitchell, Head of Installs - sarah.mitchell@acme-solar.com</p>"
        "<p>info@acme-solar.com</p>",
        "https://acme-solar.com/team",
    )
    by_email = {e.email: e for e in find_emails(page)}
    assert by_email["sarah.mitchell@acme-solar.com"].kind == "personal"
    assert by_email["sarah.mitchell@acme-solar.com"].name == "Sarah Mitchell"
    assert by_email["info@acme-solar.com"].kind == "role"


# ------------------------------------------------------------------- scoring

@pytest.fixture
def niche():
    return Config.load(FIXTURES / "niche.yaml").niche


def test_on_niche_page_scores_above_threshold(niche):
    page = parse(
        "<title>Commercial Solar Installation</title>"
        "<p>We are a commercial solar installer offering photovoltaic PV system "
        "design, inverter supply and battery storage.</p>",
        "https://x.com/commercial-solar",
    )
    assert score_page(page, niche).value >= niche.domain_threshold


def test_off_niche_page_scores_below_threshold(niche):
    page = parse("<title>Dog Grooming</title><p>We wash and clip dogs.</p>", "https://x.com/")
    assert score_page(page, niche).value < niche.domain_threshold


def test_negative_keyword_sinks_the_score(niche):
    on = "solar installer photovoltaic commercial solar"
    assert score_text(on, niche).value > 0
    assert score_text(on + " horoscope casino", niche).value < score_text(on, niche).value


def test_keyword_stuffing_is_capped(niche):
    once = score_text("solar installer", niche).value
    spammed = score_text(" ".join(["solar installer"] * 50), niche).value
    assert spammed <= once * 3 + 0.01


def test_word_boundaries():
    from scrapper.config import NicheConfig
    n = NicheConfig(strong=["solar"], page_threshold=1, domain_threshold=1)
    assert score_text("solar panels", n).value > 0
    assert score_text("solarium tanning", n).value == 0


# -------------------------------------------------------------------- helpers

def test_url_normalisation():
    assert normalize_url("/about?utm_source=x", "https://a.com/p") == "https://a.com/about"
    assert normalize_url("https://A.com/x/#frag") == "https://a.com/x"
    assert normalize_url("javascript:void(0)") is None
    assert registrable_domain("https://shop.a.co.uk/x") == "a.co.uk"


# ---------------------------------------------------------------- end to end

@pytest.fixture
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES / "site"))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_end_to_end_crawl(server, tmp_path):
    cfg = Config.load(FIXTURES / "niche.yaml")
    cfg.crawl.respect_robots = False       # local fixture has no robots.txt
    cfg.crawl.per_domain_delay = 0.0
    cfg.discovery.use_sitemaps = False
    store = Store(tmp_path / "test.db")
    register_candidates(store, cfg, [server + "/"], "site", "test")

    async def go():
        async with Fetcher(per_domain_delay=0.0, respect_robots=False) as f:
            await Crawler(cfg, store, f).run()

    asyncio.run(go())
    rows = {r["email"]: r for r in store.conn.execute("SELECT * FROM emails")}

    assert "jane@brightsolar.example" in rows          # from the contact page
    assert "info@brightsolar.example" in rows          # role address on homepage
    assert "noreply@brightsolar.example" not in rows   # filtered
    assert rows["jane@brightsolar.example"]["name"] == "Jane Fletcher"
    assert store.count_domains("accepted") == 1
    store.close()
