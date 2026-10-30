# BNM Public Notices API (unofficial)

Mirrors Bank Negara Malaysia's [Public Notices](https://www.bnm.gov.my/notices-announcements)
(2009 → present) into versioned JSON and serves it as a read-only API.

```
GitHub Actions (cron)           GitHub repo                 Vercel
scraper/collect.py  ──commit──▶  data/*.json  ──push──▶  api/index.py (FastAPI)
  scrape BNM, diff, log events     source of truth          read-only, CDN-cached
```

**Why this shape:** Vercel functions are short-lived with a read-only filesystem, so they
can't run a crawler or keep state. The scraper runs in Actions and commits plain JSON;
every commit redeploys the API. Git history doubles as an audit trail of what BNM changed.

## Data layout

| Path | Contents |
|---|---|
| `data/notices/{year}.json` | full records incl. `body_text`, attachments, hashes |
| `data/index.json` | same without `body_text` (used for list/search) |
| `data/changes.jsonl` | append-only events: `notice.created/updated/removed`, `attachment.created/removed` |
| `data/coverage.json` | per-year listed vs stored, fetch failures — use to reconcile |

Events from the initial backfill (and any notice older than 14 days when first seen) carry
`"historical": true`, so you can suppress them from live alerts.

## API

| Endpoint | Notes |
|---|---|
| `GET /notices` | `year`, `q`, `scope=title\|all`, `date_from`, `date_to`, `has_attachments`, `sort`, `limit` (≤200), `offset` |
| `GET /notices/{id}` | full record with body + attachment URLs |
| `GET /changes` | `since`, `event_type`, `include_historical`, `limit` |
| `GET /years` | coverage report |
| `GET /docs` | interactive OpenAPI |

## Run locally

```bash
pip install -r requirements-scraper.txt -r requirements.txt uvicorn
python -m scraper.collect --full          # backfill 2009 → now (~12 min, 0.8s between requests)
uvicorn api.index:app --reload            # http://127.0.0.1:8000/docs
```

Other collector modes: `--years 2015-2018`, `--recheck-days 45`, `--recheck-all`, `--listing-only`.

## Deploy

1. Push to GitHub (commit the generated `data/` — it is the product).
2. Vercel → *Add New Project* → import the repo. No build settings needed
   (`vercel.json` routes everything to `api/index.py` and bundles `data/**`).
3. Actions → *update-notices* → *Run workflow* (`mode: reconcile`) to confirm the bot can push.
   Schedules: 30-min discovery · daily 45-day recheck · weekly all-year reconcile · monthly deep recheck.

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
- Treat `/years` as the completeness check; `fetch_status` ≠ `ok` rows need a look.
