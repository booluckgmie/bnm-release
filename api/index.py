"""Read-only JSON API over the committed BNM notices dataset (deployed on Vercel).

The API never scrapes. A GitHub Action runs the collector, commits ./data, and Vercel
redeploys on that push - so responses are always served from a consistent snapshot.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE = "public, s-maxage=900, stale-while-revalidate=3600"

app = FastAPI(
    title="BNM Public Notices API",
    version="1.0.0",
    description="Unofficial mirror of Bank Negara Malaysia public notices "
                "(https://www.bnm.gov.my/notices-announcements). Not affiliated with BNM; "
                "always confirm against the source `url` on each record.",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


# ------------------------------------------------------------------------ data
@lru_cache(maxsize=1)
def index() -> list[dict]:
    return json.loads((DATA / "index.json").read_text(encoding="utf-8")) if (DATA / "index.json").exists() else []


@lru_cache(maxsize=1)
def by_id() -> dict[str, dict]:
    return {r["id"]: r for r in index()}


@lru_cache(maxsize=32)
def year_file(year: int) -> dict[str, dict]:
    p = DATA / "notices" / f"{year}.json"
    return {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8"))} if p.exists() else {}


@lru_cache(maxsize=1)
def coverage() -> dict:
    p = DATA / "coverage.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _set_cache(resp: Response) -> None:
    resp.headers["Cache-Control"] = CACHE


# --------------------------------------------------------------------- endpoints
@app.get("/", tags=["meta"])
def root(response: Response):
    _set_cache(response)
    return {
        "name": "BNM Public Notices API (unofficial)",
        "data_updated_at": coverage().get("updated_at"),
        "notices": len(index()),
        "endpoints": ["/notices", "/notices/{id}", "/years", "/changes", "/health", "/docs"],
    }


@app.get("/health", tags=["meta"])
def health():
    return {"ok": bool(index()), "notices": len(index())}


@app.get("/years", tags=["meta"])
def years(response: Response):
    """Per-year coverage: rows listed on BNM vs. stored, and fetch failures."""
    _set_cache(response)
    return coverage()


@app.get("/notices", tags=["notices"])
def list_notices(
    response: Response,
    year: int | None = Query(None, ge=2009, le=2100),
    q: str | None = Query(None, min_length=2, description="case-insensitive substring match"),
    scope: Literal["title", "all"] = Query("title", description="'all' also searches the body text"),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD"),
    has_attachments: bool | None = None,
    sort: Literal["newest", "oldest"] = "newest",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    _set_cache(response)
    rows = index()
    if year:
        rows = [r for r in rows if r["year"] == year]
    if date_from:
        rows = [r for r in rows if r["published_date"] >= date_from]
    if date_to:
        rows = [r for r in rows if r["published_date"] <= date_to]
    if has_attachments is not None:
        rows = [r for r in rows if (r["attachment_count"] > 0) == has_attachments]
    if q:
        needle = q.lower()

        def hit(r: dict) -> bool:
            if needle in r["title"].lower():
                return True
            return scope == "all" and needle in year_file(r["year"]).get(r["id"], {}).get("body_text", "").lower()

        rows = [r for r in rows if hit(r)]
    if sort == "oldest":
        rows = rows[::-1]
    return {"total": len(rows), "limit": limit, "offset": offset, "data": rows[offset: offset + limit]}


@app.get("/notices/{notice_id}", tags=["notices"])
def get_notice(notice_id: str, response: Response):
    """Full record including body_text and attachment links."""
    _set_cache(response)
    meta = by_id().get(notice_id)
    if not meta:
        raise HTTPException(404, "notice not found")
    return year_file(meta["year"]).get(notice_id, meta)


@app.get("/changes", tags=["events"])
def changes(
    response: Response,
    since: str | None = Query(None, description="ISO timestamp; only events after this"),
    event_type: Literal["notice.created", "notice.updated", "notice.removed",
                        "attachment.created", "attachment.removed"] | None = None,
    include_historical: bool = Query(False, description="include events from the initial backfill"),
    limit: int = Query(100, ge=1, le=500),
):
    """Newest-first event log. Historical (backfill) events are hidden unless requested."""
    _set_cache(response)
    p = DATA / "changes.jsonl"
    events = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line] if p.exists() else []
    if not include_historical:
        events = [e for e in events if not e.get("historical")]
    if since:
        events = [e for e in events if e["ts"] > since]
    if event_type:
        events = [e for e in events if e["event_type"] == event_type]
    events.reverse()
    return {"total": len(events), "data": events[:limit]}
