# BNM Public Notices API (unofficial)

Mirrors Bank Negara Malaysia's [Public Notices](https://www.bnm.gov.my/notices-announcements)
(2009 → present) into versioned JSON, and serves it as a read-only JSON API with a simple
search page.

- **Live site:** <https://kahoot-alt-5sxp-my-ketapang.vercel.app> (search page at `/`, API docs at `/docs`)
- **Dataset:** 810 notices across 2009–2026, committed in `data/`
- **Not affiliated with BNM.** Always confirm against the source `url` on each record.

```
GitHub Actions (cron)           GitHub repo                 Vercel
scraper/collect.py  ──commit──▶  data/*.json  ──push──▶  api/index.py (FastAPI) + web/index.html
  scrape BNM, diff, log events     source of truth          read-only, CDN-cached
```

**Why this shape:** Vercel functions are short-lived with a read-only filesystem, so they
can't run a crawler or keep state. The scraper runs in Actions and commits plain JSON;
every data commit redeploys the site. Git history doubles as an audit trail of what BNM changed.

## Repo layout

| Path | Purpose |
|---|---|
| `scraper/collect.py` | Collector: scrapes BNM, diffs against stored data, writes `data/` and the event log |
| `api/index.py` | FastAPI app (the Vercel function). Never scrapes; reads `data/` only |
| `web/index.html` | Search page served at `/` |
| `data/` | The dataset (see below). Committed on purpose: it is the product |
| `vercel.json` | Routing and bundling config for Vercel |
| `.github/workflows/update.yml` | Scheduled and manual collection; commits `data/` when it changes |
| `requirements.txt` | API dependencies (FastAPI only; keep lean for the function) |
| `requirements-scraper.txt` | Collector dependencies (requests, beautifulsoup4, lxml) |

## Data layout

| Path | Contents |
|---|---|
| `data/notices/{year}.json` | full records incl. `body_text`, attachments, hashes |
| `data/index.json` | same without `body_text` (used for list/search) |
| `data/changes.jsonl` | append-only events: `notice.created/updated/removed`, `attachment.created/removed` |
| `data/coverage.json` | per-year listed vs stored, fetch failures — use to reconcile |

Events from the initial backfill (and any notice older than 14 days when first seen) carry
`"historical": true`, so you can suppress them from live alerts. The log currently holds 1,522
historical events and no live ones.

## Search page

`/` is a small page over the same API: search by title or body text, filter by year, date range
and attachments, sort, page through results, and click a notice to read its full text with links
to the source page and PDFs.

## API

| Endpoint | Notes |
|---|---|
| `GET /` | search page (HTML) |
| `GET /info` | JSON summary: notice count, data update time, endpoint list |
| `GET /notices` | `year`, `q`, `scope=title\|all`, `date_from`, `date_to`, `has_attachments`, `sort`, `limit` (≤200), `offset` |
| `GET /notices/{id}` | full record with body + attachment URLs; 404 if unknown |
| `GET /changes` | `since`, `event_type`, `include_historical`, `limit` |
| `GET /years` | coverage report |
| `GET /health` | `{"ok": true, "notices": N}` |
| `GET /docs` | interactive OpenAPI |

Invalid parameters return 422. Responses carry `Cache-Control: public, s-maxage=900,
stale-while-revalidate=3600`, so the CDN may serve data up to ~15 minutes old.

## Update schedule

The `update-notices` workflow runs on these schedules (UTC). It commits to `data/` only when
something changed, and each commit redeploys the site.

| Frequency | When | What it does |
|---|---|---|
| Every 30 min | `*/30 * * * *` | Discover new notices (current year; plus last year in Jan/Feb) |
| Daily | 20:00 UTC (04:00 MYT) | Re-check notices from the last 45 days for edits |
| Weekly | Sundays 21:00 UTC | Reconcile every year's listing (catches backdated additions) |
| Monthly | 1st, 22:00 UTC | Re-fetch all articles to detect edits to old notices |

A new BNM notice should appear on the site within roughly 30–40 minutes. GitHub treats scheduled
runs as best-effort, so they can start late or be skipped under load. You can also run it by hand:
Actions → *update-notices* → *Run workflow* with `mode` = `discover | recheck | reconcile | deep | backfill`.

## Run locally

```bash
pip install -r requirements-scraper.txt -r requirements.txt uvicorn
python -m scraper.collect --full          # backfill 2009 → now (~12 min, 0.8s between requests)
uvicorn api.index:app --reload            # http://127.0.0.1:8000  (docs at /docs)
```

Other collector modes: `--years 2015-2018`, `--recheck-days 45`, `--recheck-all`, `--listing-only`.

## Deploy

1. Push to GitHub (commit the generated `data/`).
2. Vercel → *Add New Project* → import the repo. No build settings needed.
3. Actions → *update-notices* → *Run workflow* (`mode: reconcile`) to confirm the bot can push.
   For that, set Settings → Actions → General → Workflow permissions to **Read and write**.

### How Vercel routing works (important)

`vercel.json` rewrites every path to the single function `api/index.py`, and bundles `data/` and
`web/` with it. Vercel hands the function the rewrite destination (`/api/index`), not the URL that
was requested, so the rewrite forwards the original path as `?__p=<path>`. A small ASGI wrapper
(`RestorePath` in `api/index.py`) restores that path before routing. Without it every URL
reached the app as `/api/index` and returned 404 or the home page. Direct requests to
`/api/index/...` also work as a fallback.

## Status and what is verified

| Item | Status |
|---|---|
| API endpoints, filters, 404/422 handling | Tested locally against the real data |
| Vercel deployment and routing | Tested on a preview and on production: `/health`, `/years`, `/notices` filters, 404 |
| Search page | Tested in a headless browser locally (search, expand a notice, no console errors); not re-checked on production |
| GitHub Action (`update.yml`) | **Not yet run.** First run needs *Read and write* workflow permissions; use `mode: reconcile` |
| Scheduled runs | Not yet observed; check the Actions tab after the first day |

## Verified gotchas about bnm.gov.my (Cloudflare)

These cost real debugging time; the collector already handles them.

- **No cookies.** Persisting `__cf_bm` makes follow-up requests return **403**. The client
  uses a cookie-less session.
- **No `Accept-Language`.** UA + `Accept-Language` returned 403 consistently; plain UA passes.
- **Two `div.journal-content-article` per page.** The first is the "Filter by year" widget;
  the article is the one inside `div.asset-full-content`. The parser is strict on purpose —
  falling back to a wider container silently stores site chrome as content.
- Year pages are **not paginated** (largest year ≈ 77 rows).
- `robots.txt` only disallows `/view.php?*` for generic bots at time of writing.

## Before you rely on it

- Read BNM's [Terms of Use](https://www.bnm.gov.my/terms-of-use) re: reuse/redistribution; this
  repo makes no claim to BNM content. Set `BNM_USER_AGENT` to include a contact.
- Attachments are **linked, not downloaded**. Replaced-in-place PDFs are not detected yet
  (would need HEAD/ETag or hashing).
- Treat `/years` as the completeness check; `fetch_status` ≠ `ok` rows need a look. A few early
  years have a handful of `not_found` / `not_an_article` entries (2009, 2010, 2017).
- If data changes often, the frequent commits could hit your Vercel plan's deployment limits;
  consider an hourly discovery cron (`0 * * * *`).
- The Vercel project is currently named `kahoot-alt-5sxp`; rename it in project settings if you
  want the URL to match the repo.
