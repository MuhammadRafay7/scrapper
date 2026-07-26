# Niche email scraper

Collects publicly published contact addresses **only from sites that are actually in your
niche**. Nothing is stored from a page until both the domain and the page pass a keyword
relevance gate, so you don't end up with a CSV full of unrelated `webmaster@` addresses.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Use

```bash
python -m scrapper init -o config/niche.yaml   # copy the starter config
$EDITOR config/niche.yaml                      # define your niche + sources
python -m scrapper run -c config/niche.yaml    # discover + crawl
python -m scrapper stats -c config/niche.yaml
python -m scrapper export -c config/niche.yaml -o data/contacts.csv --personal-only
```

Everything is resumable. Ctrl-C mid-crawl, rerun `crawl`, it picks up the pending queue.

## How it decides what's "in the niche"

Four keyword tiers in the config, scored per page with each keyword capped at 3 hits so a
stuffed page can't game it:

| tier | weight | meaning |
|---|---|---|
| `strong` | +4.0 | only your niche says these (`solar installer`, `photovoltaic`) |
| `medium` | +1.5 | adjacent / supporting terms |
| `weak` | +0.5 | generic industry words |
| `negative` | −6.0 | hard signals it's the wrong niche |

Title and URL text count 1.5×. Two gates then apply:

1. **`domain_threshold`** — a domain is judged once, on the first page seen (its homepage).
   Below threshold, the domain is marked `rejected` and its whole queue is dropped, so an
   off-niche site costs one request, not forty.
2. **`page_threshold`** — `page score + ½ domain score` must clear this before emails on
   that page are stored. A thin contact page still passes because its domain carries it.

Tune from `stats` and the `note` column in the `domains` table (it records which keywords
fired). Too few results → lower thresholds or add `medium` terms. Too much noise → raise
`domain_threshold` and add `negative` terms.

## Finding sources

Configured under `discovery:`, all optional and combinable:

- **`seeds` / `seed_file`** — explicit company URLs. Deterministic, no API key.
- **`directories`** — association member lists, trade directories. Their *outbound* links
  become new candidate domains, each gated on its own homepage. This is the highest-yield
  source for most niches.
- **`search`** — niche queries through Serper, Brave, or Google CSE. Set the key in the env
  var named by `api_key_env` (default `SERPER_API_KEY`). Widest net; costs a few dollars per
  few thousand queries.
- **`use_sitemaps`** — once a domain is accepted, its `sitemap.xml` is mined for
  contact/about/team URLs, which jump the queue.

## Extraction

Handles plain text, `mailto:` links, Cloudflare `data-cfemail` obfuscation, and the
`name (at) domain [dot] com` family. Filters out image assets (`logo@2x.png`), JS package
names, and `noreply@`-type addresses. Classifies each address as `personal` or `role`
(`info@`, `sales@`, …) and guesses a name from surrounding text or a `first.last@` pattern.

## Politeness

`robots.txt` is honoured, one request per domain per `per_domain_delay` seconds (default
1.5), responses capped at 3 MB, non-HTML skipped, and social/aggregator domains are
blocked by default — including LinkedIn, which forbids scraping and is not supported.

## Before you email anyone

Collecting published business addresses is one thing; sending to them is a separate legal
question. Under GDPR/PECR (EU/UK) B2B cold email needs a legitimate-interest basis, an
identifiable sender, and a working opt-out; CAN-SPAM (US) requires a physical address and
opt-out honoured within 10 days. `--personal-only` addresses carry more risk than role
addresses. Suppress anyone who unsubscribes — keep that list outside this database.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

10 tests, fully offline — the end-to-end one crawls a fixture site over localhost.

## Layout

| file | role |
|---|---|
| [scrapper/config.py](scrapper/config.py) | YAML → dataclasses, defaults, block-list merging |
| [scrapper/db.py](scrapper/db.py) | SQLite schema + resumable queue |
| [scrapper/net.py](scrapper/net.py) | async fetch, robots, per-domain throttle, URL normalising |
| [scrapper/discover.py](scrapper/discover.py) | search APIs, seeds, directories, sitemaps |
| [scrapper/extract.py](scrapper/extract.py) | HTML → text/links/emails, de-obfuscation |
| [scrapper/score.py](scrapper/score.py) | weighted keyword relevance |
| [scrapper/crawl.py](scrapper/crawl.py) | the fetch → score → gate → harvest loop |
| [scrapper/export.py](scrapper/export.py) | CSV/JSON output |
| [scrapper/cli.py](scrapper/cli.py) | commands |
