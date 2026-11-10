#!/usr/bin/env python3
"""Collector for Bank Negara Malaysia (BNM) Public Notices.

Source pattern (verified):  https://www.bnm.gov.my/notices-announcements/-/tag/notices-{YEAR}
Each year page is a single, non-paginated table of  date | title | article URL.
Article pages hold the body in <div class="journal-content-article"> and
attachments as links under /documents/...

A second source covers Monetary Policy Statements (OPR decisions): one table at
https://www.bnm.gov.my/monetary-stability/opr-decisions, each row linking to a statement article
at /-/monetary-policy-statement-DDMMYYYY. These records carry category "mps", opr_change and opr_level.

Output (all under ./data, committed to git):
  notices/{year}.json   full records incl. body_text
  mps/{year}.json       Monetary Policy Statements, same record shape
  policy/{year}.json    policy documents, exposure drafts, discussion papers (PDF links, no article page)
  press/{year}.json     press releases          speeches/{year}.json   speeches
  fmc/{year}.json       Financial Markets Committee statements
  fep/{year}.json       foreign exchange policy announcements
  index.json            same records without body_text (fast list/search)
  changes.jsonl         append-only event log (notice.created / notice.updated / ...)
  coverage.json         per-year counts + failures, for reconciliation

Usage:
  python -m scraper.collect                      # incremental: this + last year
  python -m scraper.collect --full               # backfill 2009 -> current year
  python -m scraper.collect --years 2015-2018    # specific range
  python -m scraper.collect --recheck-days 45    # re-fetch recent articles to detect edits
  python -m scraper.collect --listing-only       # discovery only, no article fetch
  python -m scraper.collect --sources mps,policy # only some sources (default: all)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin

from http.cookiejar import DefaultCookiePolicy

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.bnm.gov.my"
YEAR_URL = BASE + "/notices-announcements/-/tag/notices-{year}"
OPR_URL = BASE + "/monetary-stability/opr-decisions"
PRESS_URL = BASE + "/press-release-{year}"
SPEECH_URL = BASE + "/speeches-interviews/-/tag/speeches{year}"
FMC_URL = BASE + "/committees/fmc/statements"
FEP_URL = BASE + "/fep/announcements"
POLICY_PAGES = {  # sector -> page with the "Date | Title | Type" table of policy documents
    "banking": BASE + "/standardsandguidelines",
    "insurance-takaful": BASE + "/insurance-takaful",
    "payment-systems": BASE + "/payment-systems",
}
FIRST_YEAR_PRESS = FIRST_YEAR_SPEECH = 2009  # earlier years exist (press from 1998); raise coverage with --years
FIRST_YEAR = 2009
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("BNM_DATA_DIR", ROOT / "data"))
UA = os.environ.get(
    "BNM_USER_AGENT",
    "bnm-notices-collector/1.0 (personal research; set BNM_USER_AGENT to add contact)",
)
ATTACH_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "xlsm", "csv", "zip", "ppt", "pptx"}
HISTORICAL_AFTER_DAYS = 14  # notices published longer ago than this are "historical imports"

log = logging.getLogger("bnm")


# --------------------------------------------------------------------------- http
class Client:
    def __init__(self, delay: float = 0.8, timeout: int = 40):
        self.delay, self.timeout, self._last = delay, timeout, 0.0
        self.s = requests.Session()
        # BNM sits behind Cloudflare. Verified: (1) UA + Accept-Language header -> 403, and
        # (2) persisting cookies (__cf_bm) makes later requests 403. So: minimal headers, no cookie jar.
        self.s.headers.update({"User-Agent": UA})
        self.s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "HEAD"), respect_retry_after_header=True)
        self.s.mount("https://", HTTPAdapter(max_retries=retry))

    def get(self, url: str) -> requests.Response:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            # Cloudflare returns 403 when it throttles sustained crawling (seen after ~700 requests).
            # Back off and retry instead of recording a failure; urllib3's Retry ignores 403.
            for attempt, pause in enumerate((0, 30, 90)):
                if pause:
                    log.warning("403 from BNM, backing off %ss (attempt %s) %s", pause, attempt + 1, url[-60:])
                    time.sleep(pause)
                resp = self.s.get(url, timeout=self.timeout)
                if resp.status_code != 403:
                    break
            return resp
        finally:
            self._last = time.monotonic()


# ------------------------------------------------------------------------ parsing
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def notice_id(url: str) -> str:
    """Stable id: the slug after /-/ for BNM articles, else a short hash of the URL."""
    m = re.match(re.escape(BASE) + r"/-/([^?#]+)", url)
    if m:
        slug = m.group(1).strip("/")
        if re.fullmatch(r"[A-Za-z0-9._~-]+", slug):  # URL-safe slug only
            return slug
    # a few legacy 2009 URLs embed raw HTML fragments in the slug -> use a stable hash instead
    return "x-" + hashlib.sha1(url.encode()).hexdigest()[:12]


def parse_listing(html: str, year: int) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one('[class*="pagination"], a[href*="_cur="]'):
        log.warning("year %s: pagination markers found - listing may be incomplete", year)
    items, seen = [], set()
    for tr in soup.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        try:
            d = datetime.strptime(_clean(tds[0].get_text()), "%d %b %Y").date()
        except ValueError:
            continue
        links = [(a.get_text(strip=True), urldefrag(urljoin(BASE, a["href"]))[0])
                 for a in tds[1].find_all("a", href=True)]
        if not links:
            continue
        label = next((t for t, _ in links if t), _clean(tds[1].get_text()))
        link_url = next((u for t, u in links if t), links[0][1])
        # canonical = the BNM article page if present, else whatever the row links to
        url = next((u for _, u in links if u.startswith(BASE + "/-/")), link_url)
        if url in seen:
            continue
        seen.add(url)
        items.append({
            "id": notice_id(url), "url": url, "link_url": link_url,
            "title": _clean(label), "published_date": d.isoformat(), "year": d.year,
        })
    return items


def mps_title(d: date, change: str, level: str) -> str:
    when = f"{d.day} {d:%b %Y}"
    try:
        delta = float(change)
    except ValueError:
        return f"Monetary Policy Statement, {when}"
    if delta == 0:
        move = f"OPR unchanged at {level}%"
    else:
        move = f"OPR {'raised' if delta > 0 else 'cut'} by {abs(round(delta * 100))} bps to {level}%"
    return f"Monetary Policy Statement, {when}: {move}"


def parse_opr_listing(html: str) -> list[dict]:
    """Rows of the OPR Decisions table: date | change in OPR | new OPR level | link to the statement."""
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()
    for tr in soup.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        try:
            d = datetime.strptime(_clean(tds[0].get_text()), "%d %b %Y").date()
        except ValueError:
            continue
        a = tds[3].find("a", href=True)
        if a is None:
            continue
        url = urldefrag(urljoin(BASE, a["href"]))[0]
        if url in seen:
            continue
        seen.add(url)
        change, level = _clean(tds[1].get_text()), _clean(tds[2].get_text())
        items.append({
            "id": notice_id(url), "url": url, "link_url": url,
            "title": mps_title(d, change, level), "published_date": d.isoformat(), "year": d.year,
            "category": "mps", "opr_change": change, "opr_level": level,
        })
    return items


_DOC_RE = re.compile(r"(/documents/\d+/\d+/[^/?#]+?\.[A-Za-z0-9]+)")
_DATE_RE = re.compile(r"\d{1,2} [A-Za-z]{3} \d{4}")


def _attachment(href: str, label: str) -> dict:
    path = href.split("?")[0]
    m = _DOC_RE.search(path)
    name = m.group(1).rsplit("/", 1)[-1] if m else path.rstrip("/").rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {"url": href, "label": _clean(label), "filename": name, "ext": ext}


def parse_policy_listing(html: str, sector: str) -> list[dict]:
    """Rows of a sector's policy table: date | title (one or more PDF links) | type | year.
    Rows link straight to documents, so each record's attachments come from the row itself."""
    soup = BeautifulSoup(html, "lxml")
    table = next((t for t in soup.find_all("table")
                  if t.find("th") and {"Date", "Type"} <= {_clean(c.get_text()) for c in t.find("tr").find_all(["th", "td"])}),
                 None)
    if table is None:
        return []
    items = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        m = _DATE_RE.findall(tds[0].get_text(" "))
        try:
            d = datetime.strptime(m[-1], "%d %b %Y").date()
        except (IndexError, ValueError):
            continue
        links, seen = [], set()
        for a in tr.find_all("a", href=True):
            u = urljoin(BASE, a["href"]).split("#")[0]
            if u not in seen:
                seen.add(u)
                links.append((_clean(a.get_text()), u))
        if not links:
            continue
        label, url = next(((t, u) for t, u in links if t), links[0])
        found = _DOC_RE.search(url.split("?")[0])
        key = (found.group(1) if found else url.split("?")[0].rstrip("/")).lower()
        items.append({
            "id": "policy-" + hashlib.sha1(key.encode()).hexdigest()[:12],
            "url": url, "link_url": url, "title": label or _clean(tds[1].get_text()),
            "published_date": d.isoformat(), "year": d.year, "category": "policy",
            "policy_type": _clean(tds[2].get_text()), "sectors": [sector],
            "attachments": [_attachment(u, t) for t, u in links],
        })
    return items


def parse_article(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    # Strict + scoped. The page contains TWO div.journal-content-article: the first is the
    # "Filter by year" widget (site chrome), the real article is inside div.asset-full-content.
    # No fallback to wider containers: that would silently store chrome as "content".
    node = soup.select_one("div.asset-full-content div.journal-content-article")
    if node is None:
        return None
    wrap = soup.select_one("div.asset-full-content")
    m = re.search(r"Release Date:?\s*(\d{1,2} [A-Za-z]{3} \d{4})", wrap.get_text(" ") if wrap else "")
    release = None
    if m:
        try:
            release = datetime.strptime(m.group(1), "%d %b %Y").date().isoformat()
        except ValueError:
            pass
    for t in node.select("script, style"):
        t.decompose()
    attachments, seen = [], set()
    for a in node.find_all("a", href=True):
        href = urldefrag(urljoin(BASE, a["href"]))[0]
        path = href.split("?")[0]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if href in seen or not (ext in ATTACH_EXT or "/documents/" in href):
            continue
        seen.add(href)
        attachments.append({"url": href, "label": _clean(a.get_text()),
                            "filename": path.rsplit("/", 1)[-1], "ext": ext})
    text = "\n".join(ln for ln in (_clean(x) for x in node.get_text("\n").splitlines())
                     if ln and ln not in ("Reading:", "Share:"))
    h2 = soup.select_one("div.asset-full-content h2, div.asset-content h2")
    return {"page_title": _clean(h2.get_text()) if h2 else "", "release_date": release,
            "body_text": text, "attachments": attachments}


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------ storage
def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, obj, indent=1) -> bool:
    """Write only if content changed (keeps git history and Vercel deploys quiet)."""
    new = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == new:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return True


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# -------------------------------------------------------------------------- core
def fetch_article(client: Client, rec: dict) -> None:
    """Fill body/attachments/hashes on rec in place; record failure instead of raising."""
    rec["last_checked_at"] = now_iso()
    if not rec["url"].startswith(BASE + "/-/"):
        # row links straight to a PDF / external site / other BNM page: nothing to scrape
        rec.update(fetch_status="not_an_article", fetch_error=None)
        rec.setdefault("body_text", "")
        rec.setdefault("attachments", [])
        if rec["url"].split("?")[0].lower().rsplit(".", 1)[-1] in ATTACH_EXT:
            rec["attachments"] = [{"url": rec["url"], "label": rec["title"],
                                   "filename": rec["url"].rsplit("/", 1)[-1],
                                   "ext": rec["url"].split("?")[0].rsplit(".", 1)[-1].lower()}]
        rec["body_hash"] = sha(rec["body_text"])
        rec["content_hash"] = sha(rec["body_hash"] + "|" + "|".join(a["url"] for a in rec["attachments"]))
        return
    try:
        r = client.get(rec["url"])
        if r.status_code == 404:  # genuinely dead link: don't retry on every run
            rec.update(fetch_status="not_found", fetch_error="HTTP 404")
            rec.setdefault("body_text", "")
            rec.setdefault("attachments", [])
            rec["body_hash"] = sha(rec["body_text"])
            rec["content_hash"] = sha(rec["body_hash"])
            return
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        parsed = parse_article(r.text)
        if parsed is None:
            raise RuntimeError("article container not found")
    except Exception as e:  # noqa: BLE001 - record and move on
        rec["fetch_status"], rec["fetch_error"] = "error", str(e)[:200]
        rec.setdefault("body_text", "")
        rec.setdefault("attachments", [])
        return
    rec.update(fetch_status="ok", fetch_error=None, body_text=parsed["body_text"],
               attachments=parsed["attachments"], release_date=parsed["release_date"])
    rec["body_hash"] = sha(parsed["body_text"])
    rec["content_hash"] = sha(rec["body_hash"] + "|" + "|".join(sorted(a["url"] for a in parsed["attachments"])))


def is_historical(published: str) -> bool:
    return date.fromisoformat(published) < date.today() - timedelta(days=HISTORICAL_AFTER_DAYS)


def process_year(client: Client, year: int, args, events: list[dict]) -> dict:
    stats = {"year": year, "listed": 0, "stored": 0, "ok": 0, "errors": 0, "not_an_article": 0, "not_found": 0,
             "created": 0, "updated": 0, "removed": 0, "listing_error": None}
    path = DATA / "notices" / f"{year}.json"
    existing = {r["id"]: r for r in read_json(path, [])}

    try:
        resp = client.get(YEAR_URL.format(year=year))
        if resp.status_code == 404:
            raise RuntimeError("HTTP 404 (no page for this year yet)")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        listing = parse_listing(resp.text, year)
        if not listing:
            raise RuntimeError("listing parsed to 0 rows (layout change?)")
    except Exception as e:  # noqa: BLE001
        log.error("year %s listing failed: %s", year, e)
        stats["listing_error"] = str(e)
        stats["stored"] = len(existing)
        return stats  # keep existing data untouched

    stats["listed"] = len(listing)
    ts = now_iso()
    recheck_cutoff = date.today() - timedelta(days=args.recheck_days)
    listed_ids = set()

    for item in listing[: args.limit or None]:
        listed_ids.add(item["id"])
        rec = existing.get(item["id"])
        if rec is None:
            rec = {**item, "first_seen_at": ts, "last_changed_at": ts, "listed": True}
            if not args.listing_only:
                fetch_article(client, rec)
            existing[rec["id"]] = rec
            stats["created"] += 1
            events.append(_event("notice.created", rec, ts))
            for a in rec.get("attachments", []):
                events.append(_event("attachment.created", rec, ts, attachment=a["url"]))
            continue

        changed = False
        rec["listed"] = True
        for k in ("title", "published_date", "link_url"):
            if rec.get(k) != item[k]:
                rec[k], changed = item[k], True
        rec["year"] = item["year"]

        due = (not args.listing_only and (
            rec.get("fetch_status") in ("error", None)
            or args.recheck_all
            or date.fromisoformat(rec["published_date"]) >= recheck_cutoff))
        if due:
            before_atts = {a["url"] for a in rec.get("attachments", [])}
            before_hash, before_status = rec.get("content_hash"), rec.get("fetch_status")
            fetch_article(client, rec)
            if rec["fetch_status"] != "error" and before_status != "error" and rec.get("content_hash") != before_hash:
                changed = True
                after_atts = {a["url"] for a in rec["attachments"]}
                for u in sorted(after_atts - before_atts):
                    events.append(_event("attachment.created", rec, ts, attachment=u))
                for u in sorted(before_atts - after_atts):
                    events.append(_event("attachment.removed", rec, ts, attachment=u))
        if changed:
            rec["last_changed_at"] = ts
            stats["updated"] += 1
            events.append(_event("notice.updated", rec, ts))

    # notices that disappeared from the listing
    if not args.limit:
        for nid, rec in existing.items():
            if nid not in listed_ids and rec.get("listed", True):
                rec["listed"], rec["last_changed_at"] = False, ts
                stats["removed"] += 1
                events.append(_event("notice.removed", rec, ts))

    records = sorted(existing.values(), key=lambda r: (r["published_date"], r["id"]), reverse=True)
    write_json(path, records)
    stats["stored"] = len(records)
    stats["ok"] = sum(r.get("fetch_status") == "ok" for r in records)
    stats["errors"] = sum(r.get("fetch_status") == "error" for r in records)
    stats["not_an_article"] = sum(r.get("fetch_status") == "not_an_article" for r in records)
    stats["not_found"] = sum(r.get("fetch_status") == "not_found" for r in records)
    return stats


# Sources other than the per-year Public Notices listing share one engine. A source says where its
# listing pages are and how to parse them; the engine does create / edit / removal bookkeeping, writes
# data/<dir>/{year}.json and emits the same events as notices.
CATEGORY_ORDER = ["mps", "policy", "fmc", "fep", "speech", "press"]   # also the dedupe priority after "notice"
DATA_DIRS = {"notice": "notices", "mps": "mps", "policy": "policy", "fmc": "fmc", "fep": "fep",
             "speech": "speeches", "press": "press"}


def source_years(args, first: int) -> list[int]:
    this_year = date.today().year
    if args.years:
        return parse_years(args.years)
    if args.full:
        return list(range(first, this_year + 1))
    return [this_year - 1, this_year] if date.today().month <= 2 else [this_year]


def _get_ok(client: Client, url: str) -> str:
    resp = client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {url[-50:]}")
    return resp.text


def _tag(items: list[dict], category: str) -> list[dict]:
    for it in items:
        it["category"] = category
    return items


def listing_single(url: str, parser, category: str):
    def run(client, args):
        items = _tag(parser(_get_ok(client, url)), category)
        if not items:
            raise RuntimeError("listing parsed to 0 rows (layout change?)")
        if args.years:
            wanted = set(parse_years(args.years))
            items = [i for i in items if i["year"] in wanted]
        return items, None
    return run


def listing_years(url_fmt: str, first: int, category: str):
    def run(client, args):
        items, covered, errors = [], set(), []
        for y in source_years(args, first):
            try:
                # a year with no tag page falls back to the all-years list, so keep only rows of year y
                rows = [i for i in parse_listing(_get_ok(client, url_fmt.format(year=y)), y) if i["year"] == y]
            except Exception as e:  # noqa: BLE001
                errors.append(f"{y}: {e}")
                log.warning("%s %s listing failed: %s", category, y, e)
                continue
            items += _tag(rows, category)
            covered.add(y)
        if not covered:
            raise RuntimeError("; ".join(errors) or "no years fetched")
        return items, covered
    return run


def listing_policy(client, args):
    merged: dict[str, dict] = {}
    for sector, url in POLICY_PAGES.items():
        rows = parse_policy_listing(_get_ok(client, url), sector)
        if not rows:
            raise RuntimeError(f"policy table for {sector} parsed to 0 rows (layout change?)")
        for it in rows:
            if it["id"] in merged:
                merged[it["id"]]["sectors"] = sorted(set(merged[it["id"]]["sectors"] + it["sectors"]))
            else:
                merged[it["id"]] = it
    items = list(merged.values())
    if args.years:
        wanted = set(parse_years(args.years))
        items = [i for i in items if i["year"] in wanted]
    return items, None


SOURCES = {
    "mps": listing_single(OPR_URL, parse_opr_listing, "mps"),
    "policy": listing_policy,
    "fmc": listing_single(FMC_URL, lambda h: parse_listing(h, 0), "fmc"),
    "fep": listing_single(FEP_URL, lambda h: parse_listing(h, 0), "fep"),
    "speech": listing_years(SPEECH_URL, FIRST_YEAR_SPEECH, "speech"),
    "press": listing_years(PRESS_URL, FIRST_YEAR_PRESS, "press"),
}


def finalize_direct(rec: dict) -> None:
    """Rows that link straight to documents have no article page: attachments come from the listing.
    last_checked_at only moves when the content does; otherwise every run would rewrite every record
    and commit (and redeploy) for nothing."""
    before = rec.get("content_hash")
    rec.update(fetch_status="not_an_article", fetch_error=None, body_text="")
    rec["body_hash"] = sha("")
    rec["content_hash"] = sha(rec["body_hash"] + "|" + "|".join(sorted(a["url"] for a in rec["attachments"])))
    if before is None or before != rec["content_hash"]:
        rec["last_checked_at"] = now_iso()


def load_owners() -> dict[str, str]:
    """id -> category for everything already stored, so a document listed in two places is kept once."""
    owners: dict[str, str] = {}
    for cat, sub in DATA_DIRS.items():
        for f in sorted((DATA / sub).glob("*.json")):
            for r in read_json(f, []):
                owners.setdefault(r["id"], cat)
    return owners


def process_source(client: Client, args, events: list[dict], name: str, owners: dict[str, str]) -> dict:
    """Same lifecycle as process_year (create / edit / removal) for one non-notice source."""
    stats = {"listed": 0, "stored": 0, "ok": 0, "errors": 0, "not_an_article": 0, "created": 0,
             "updated": 0, "removed": 0, "listing_error": None, "partial": False}
    ddir = DATA / DATA_DIRS[name]
    existing: dict[str, dict] = {}
    for f in sorted(ddir.glob("*.json")):
        existing.update({r["id"]: r for r in read_json(f, [])})
    try:
        listing, covered = SOURCES[name](client, args)
    except Exception as e:  # noqa: BLE001
        log.error("%s listing failed: %s", name, e)
        stats["listing_error"] = str(e)[:300]
        stats["stored"] = len(existing)
        return stats  # keep existing data untouched

    listing = [i for i in listing if owners.get(i["id"], name) == name]   # skip docs another source already owns
    stats["listed"] = len(listing)
    # per-year sources list only some years unless --full: that count says nothing about the whole source
    stats["partial"] = covered is not None and not args.full
    ts = now_iso()
    recheck_cutoff = date.today() - timedelta(days=args.recheck_days)
    listed_ids = set()

    for item in listing[: args.limit or None]:
        listed_ids.add(item["id"])
        direct = "attachments" in item
        rec = existing.get(item["id"])
        if rec is None:
            rec = {**item, "first_seen_at": ts, "last_changed_at": ts, "listed": True}
            if direct:
                finalize_direct(rec)
            elif not args.listing_only:
                fetch_article(client, rec)
            existing[rec["id"]] = rec
            owners[rec["id"]] = name
            stats["created"] += 1
            events.append(_event("notice.created", rec, ts))
            for a in rec.get("attachments", []):
                events.append(_event("attachment.created", rec, ts, attachment=a["url"]))
            continue

        changed = False
        rec["listed"] = True
        for k, v in item.items():
            if k in ("id", "url", "attachments"):
                continue
            if rec.get(k) != v:
                rec[k], changed = v, True

        before_atts = {a["url"] for a in rec.get("attachments", [])}
        before_hash, before_status = rec.get("content_hash"), rec.get("fetch_status")
        if direct:
            rec["attachments"] = item["attachments"]
            finalize_direct(rec)
            refreshed = True
        else:
            refreshed = (not args.listing_only and (
                rec.get("fetch_status") in ("error", None)
                or args.recheck_all
                or date.fromisoformat(rec["published_date"]) >= recheck_cutoff))
            if refreshed:
                fetch_article(client, rec)
        if refreshed and rec["fetch_status"] != "error" and before_status != "error" \
                and rec.get("content_hash") != before_hash:
            changed = True
            after_atts = {a["url"] for a in rec["attachments"]}
            for u in sorted(after_atts - before_atts):
                events.append(_event("attachment.created", rec, ts, attachment=u))
            for u in sorted(before_atts - after_atts):
                events.append(_event("attachment.removed", rec, ts, attachment=u))
        if changed:
            rec["last_changed_at"] = ts
            stats["updated"] += 1
            events.append(_event("notice.updated", rec, ts))

    if not args.limit:   # only rows of years we actually listed can be judged "removed"
        scope_years = covered if covered is not None else (set(parse_years(args.years)) if args.years else None)
        for nid, rec in existing.items():
            if (scope_years is None or rec["year"] in scope_years) and nid not in listed_ids and rec.get("listed", True):
                rec["listed"], rec["last_changed_at"] = False, ts
                stats["removed"] += 1
                events.append(_event("notice.removed", rec, ts))

    by_year: dict[int, list[dict]] = {}
    for r in existing.values():
        by_year.setdefault(r["year"], []).append(r)
    for y, rows in by_year.items():
        rows.sort(key=lambda r: (r["published_date"], r["id"]), reverse=True)
        write_json(ddir / f"{y}.json", rows)
    stats["stored"] = len(existing)
    stats["ok"] = sum(r.get("fetch_status") == "ok" for r in existing.values())
    stats["errors"] = sum(r.get("fetch_status") == "error" for r in existing.values())
    stats["not_an_article"] = sum(r.get("fetch_status") == "not_an_article" for r in existing.values())
    return stats


def _event(kind: str, rec: dict, ts: str, **extra) -> dict:
    return {"ts": ts, "event_type": kind, "notice_id": rec["id"], "title": rec["title"],
            "published_date": rec["published_date"], "url": rec["url"],
            "historical": is_historical(rec["published_date"]),
            **({"category": rec["category"]} if rec.get("category") else {}), **extra}


def rebuild_index() -> int:
    rows = []
    seen = set()
    for f in sorted(f for sub in DATA_DIRS.values() for f in (DATA / sub).glob("*.json")):
        for r in read_json(f, []):
            if r["id"] in seen:   # one document listed in two places: keep the first (notices sort first)
                continue
            seen.add(r["id"])
            slim = {k: v for k, v in r.items() if k != "body_text"}
            slim.setdefault("category", "notice")
            slim["body_length"] = len(r.get("body_text", ""))
            slim["attachment_count"] = len(r.get("attachments", []))
            rows.append(slim)
    rows.sort(key=lambda r: (r["published_date"], r["id"]), reverse=True)
    write_json(DATA / "index.json", rows, indent=None)
    return len(rows)


def append_events(events: list[dict]) -> None:
    if not events:
        return
    p = DATA / "changes.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")


def update_coverage(all_stats: list[dict], source_stats: dict[str, dict] | None = None) -> None:
    old = read_json(DATA / "coverage.json", {})
    cov = {"years": dict(old.get("years", {})), "sources": dict(old.get("sources", {}))}
    for name, st in (source_stats or {}).items():
        entry = {k: st[k] for k in ("listed", "stored", "ok", "errors", "not_an_article", "listing_error")}
        prev = cov["sources"].get(name)
        if st.get("partial") and prev and not st["listing_error"]:
            entry["listed"] = prev["listed"]   # keep the last full-listing count
        cov["sources"][name] = entry
    for s in all_stats:
        # state only - per-run counters (created/updated/removed) would change every run and spam commits
        cov["years"][str(s["year"])] = {k: s[k] for k in
            ("listed", "stored", "ok", "errors", "not_an_article", "not_found", "listing_error")}
    cov["total_stored"] = sum(y["stored"] for y in cov["years"].values())
    # timestamp only moves when numbers move, so no-op runs create no commit
    unchanged = {k: v for k, v in old.items() if k != "updated_at"} == cov
    cov["updated_at"] = old.get("updated_at") if unchanged else now_iso()
    write_json(DATA / "coverage.json", cov)


def parse_years(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return sorted(set(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help=f"all years {FIRST_YEAR}..current")
    ap.add_argument("--years", help="e.g. 2015-2018 or 2009,2020")
    ap.add_argument("--recheck-days", type=int, default=0,
                    help="re-fetch articles published within N days to detect edits")
    ap.add_argument("--recheck-all", action="store_true", help="re-fetch every article")
    ap.add_argument("--listing-only", action="store_true", help="discover only; skip article fetch")
    ap.add_argument("--sources", default="all",
                    help="comma list of notice,mps,policy,fmc,fep,speech,press (default: all)")
    ap.add_argument("--delay", type=float, default=0.8, help="seconds between requests")
    ap.add_argument("--limit", type=int, default=0, help="max rows per year (testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    this_year = date.today().year
    if args.years:
        years = parse_years(args.years)
    elif args.full:
        years = list(range(FIRST_YEAR, this_year + 1))
    else:
        years = [this_year - 1, this_year] if date.today().month <= 2 else [this_year]

    initial_import = not (DATA / "index.json").exists()  # empty store => everything is a backfill
    client, events, all_stats = Client(delay=args.delay), [], []
    names = ["notice", *CATEGORY_ORDER] if args.sources == "all" else [n.strip() for n in args.sources.split(",")]
    unknown = [n for n in names if n != "notice" and n not in SOURCES]
    if unknown:
        ap.error(f"unknown source(s): {', '.join(unknown)}")
    for y in (years if "notice" in names else []):
        s = process_year(client, y, args, events)
        all_stats.append(s)
        log.info("%s listed=%s stored=%s created=%s updated=%s errors=%s%s", y, s["listed"], s["stored"],
                 s["created"], s["updated"], s["errors"], f" LISTING_ERROR={s['listing_error']}" if s["listing_error"] else "")

    source_stats: dict[str, dict] = {}
    owners = load_owners() if any(n in SOURCES for n in names) else {}
    for name in CATEGORY_ORDER:   # fixed order: a document in two listings is kept by the first source
        if name in names:
            st = source_stats[name] = process_source(client, args, events, name, owners)
            log.info("%s listed=%s stored=%s created=%s updated=%s removed=%s errors=%s%s", name, st["listed"],
                     st["stored"], st["created"], st["updated"], st["removed"], st["errors"],
                     f" LISTING_ERROR={st['listing_error']}" if st["listing_error"] else "")

    if initial_import:
        for e in events:
            e["historical"] = True
    append_events(events)
    n = rebuild_index()
    update_coverage(all_stats, source_stats)
    log.info("done: %s events, %s notices in index", len(events), n)
    return 1 if any(s["listing_error"] and s["year"] != this_year for s in all_stats) else 0


if __name__ == "__main__":
    sys.exit(main())
