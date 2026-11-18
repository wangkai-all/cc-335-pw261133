"""Crawler: fetch the practice site, store raw bytes, bind a rule at fetch time.

Run as its own OS process:

    python -m canary.crawler

The crawler owns two decisions and nothing else:

1. **What was fetched.**  Every URL response is hashed; identical bytes are
   stored once (content-addressed) and referenced by a single `page_fetches`
   row.  A page that has not changed since the last visit produces no new unit
   of work.
2. **Which rule regime the fetch belongs to.**  `epoch_id` on the fetch row is
   the immutable switch boundary for auditing.  A parallel
   `publication_assignments` row is what the publisher trusts; it can later be
   migrated on promotion/rollback without touching raw bytes or the audit trail.

The crawler never parses and never publishes.  Parser workers and the publisher
are separate processes reading the shared database and raw store.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from . import config, db, rawstore, state
from .ratelimit import RateLimiter

USER_AGENT = "CanaryPracticeCrawler/1.0 (+local demo)"
MAX_PAGES_PER_PASS = 500


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append((attrs["href"], attrs.get("class") or ""))

    handle_startendtag = handle_starttag


def extract_links(markup: str, base_url: str) -> tuple[list[str], list[str]]:
    parser = _LinkCollector()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    list_links: list[str] = []
    item_links: list[str] = []
    for href, _cls in parser.links:
        full = urljoin(base_url, href)
        path = urlparse(full).path
        if path.startswith("/item/"):
            item_links.append(full)
        elif path.startswith("/category/"):
            list_links.append(full)
    return _dedupe(list_links), _dedupe(item_links)


def canonical(url: str) -> str:
    """URL key for the crawl frontier: strip fragments only."""
    p = urlparse(url)
    return p._replace(fragment="").geturl()


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def page_id_for(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if path.startswith("category/"):
        return "list:" + path.split("/", 1)[1]
    if path.startswith("item/"):
        return path.split("/", 1)[1]
    return path or "/"


def classify(url: str) -> str:
    path = urlparse(url).path
    if path.startswith("/item/"):
        return "item"
    if path.startswith("/category/"):
        return "list"
    return "other"


def fetch(url: str, etag: str | None = None) -> tuple[int, bytes, str | None, bool]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=config.fetch_timeout()) as resp:
            body = resp.read()
            return resp.status, body, resp.headers.get("ETag"), False
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", etag, True
        return exc.code, (exc.read() or b""), exc.headers.get("ETag"), False


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def ingest(conn, *, url: str, body: bytes, status_code: int, etag: str | None,
           epoch_id: int) -> tuple[int | None, bool, str]:
    """Store one raw payload and reference it by a page_fetches row.

    Returns (fetch_id, is_new, reason).  `is_new` is True when this observation
    differs from the page's currently published/latest payload, i.e. there is
    new work for parsers.

    Unchanged payloads reuse the existing row, so polling does not multiply
    work.  A payload identical to an *older* version (the site reverted) is a
    new observation and gets its own fetch row: the inventory should move back
    to it.  Its bytes still exist once in the content-addressed store.
    """
    page_id = page_id_for(url)
    kind = classify(url)
    sha, rel = rawstore.put(body)
    page = conn.execute("SELECT last_fetch_id FROM pages WHERE id=?",
                        (page_id,)).fetchone()
    latest_id = page["last_fetch_id"] if page else None
    if latest_id is not None:
        latest = conn.execute(
            "SELECT id, content_sha, version_key FROM page_fetches WHERE id=?",
            (latest_id,)).fetchone()
        if latest is not None and latest["content_sha"] == sha:
            return latest_id, False, "unchanged_payload"
        if latest is None:
            latest_id = None

    version_key = f"{page_id}:{sha}"
    clash = conn.execute(
        "SELECT id FROM page_fetches WHERE version_key=?", (version_key,)
    ).fetchone()
    if clash is not None:
        n = conn.execute("SELECT COUNT(*) FROM page_fetches WHERE page_id=?",
                         (page_id,)).fetchone()[0]
        version_key = f"{page_id}:{sha}@{n + 1}"

    now = state.now()
    # Re-resolve the live epoch at the moment of the write.  A crawl pass can
    # take longer than a promotion/rollback; the version must belong to the rule
    # that is live when it is stored, not the one that was live when the pass
    # started, or that page would be stuck on the old rule forever.
    live = state.active_epoch(conn)
    if live is not None and live["id"] != epoch_id:
        epoch_id = live["id"]
    conn.execute(
        "INSERT INTO pages(id, url, kind, first_seen_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET url=excluded.url",
        (page_id, url, kind, now),
    )
    cur = conn.execute(
        "INSERT INTO page_fetches(page_id, url, kind, version_key, content_sha, "
        "raw_path, http_status, etag, fetched_at, epoch_id, is_list_page) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (page_id, url, kind, version_key, sha, rel, status_code, etag, now,
         epoch_id, 1 if kind == "list" else 0),
    )
    fetch_id = cur.lastrowid
    conn.execute("UPDATE pages SET last_fetch_id=? WHERE id=?", (fetch_id, page_id))
    # Decide which rule may publish this version.  Canary sampling happens here,
    # once, keyed by page identity so a page never straddles the boundary.
    _assign(conn, fetch_id, page_id, kind, epoch_id)
    return fetch_id, True, "new_observation"


def _assign(conn, fetch_id: int, page_id: str, kind: str, epoch_id: int) -> None:
    epoch = conn.execute("SELECT * FROM rule_epochs WHERE id=?", (epoch_id,)).fetchone()
    if epoch is None:
        return
    target_rule = epoch["rule_id"]
    target_epoch = epoch_id
    if epoch["is_canary"]:
        if not state.in_scope(page_id, epoch["sample_percent"], kind):
            # Outside the canary sample: the previous rule keeps publishing.
            target_rule = epoch["fallback_rule_id"] or epoch["rule_id"]
            if epoch["baseline_epoch_id"]:
                target_epoch = epoch["baseline_epoch_id"]
    conn.execute(
        "INSERT INTO publication_assignments(fetch_id, epoch_id, rule_id, "
        "assigned_at, reason) VALUES(?,?,?,?,?)",
        (fetch_id, target_epoch, target_rule, state.now(), "fetch"),
    )


def set_site_mutation(conn, variant: str, edition: int, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO site_mutations(at, variant, edition, detail) VALUES(?,?,?,?)",
        (state.now(), variant, edition, detail),
    )


# ---------------------------------------------------------------------------
# a crawl pass
# ---------------------------------------------------------------------------

def crawl_once(conn, base_url: str | None = None) -> dict:
    base_url = (base_url or config.site_url()).rstrip("/")
    epoch = state.active_epoch(conn)
    if epoch is None:
        state.ensure_baseline(conn)
        epoch = state.active_epoch(conn)
    if epoch is None:
        raise RuntimeError("no active epoch")
    started = state.now()
    cur = conn.execute(
        "INSERT INTO crawl_runs(started_at, status) VALUES(?, 'running')", (started,)
    )
    run_id = cur.lastrowid
    seen = new = unchanged = errors = 0
    limiter = RateLimiter(rate_per_second=8, burst=2)

    try:
        # Frontier: home -> category pages -> paginated categories -> items.
        queue: list[tuple[str, str]] = [(base_url + "/", "frontier")]
        visited: set[str] = set()
        while queue and seen < MAX_PAGES_PER_PASS:
            url, hint = queue.pop(0)
            url = canonical(url)
            if url in visited:
                continue
            visited.add(url)
            limiter.acquire()
            try:
                status, body, etag, not_modified = fetch(url)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors += 1
                state.event(conn, "crawler", "fetch_error",
                            f"{url}: {exc}", url=url)
                continue
            if not_modified:
                continue
            if status >= 400 or not body:
                continue
            seen += 1
            markup = body.decode("utf-8", "ignore")
            list_links, item_links = extract_links(markup, url)
            # The home page is crawl frontier plumbing, not inventory content:
            # follow it but do not store it as a page version.
            if hint != "frontier":
                _fid, is_new, _reason = ingest(
                    conn, url=url, body=body, status_code=status, etag=etag,
                    epoch_id=epoch["id"],
                )
                if is_new:
                    new += 1
                else:
                    unchanged += 1
            queue.extend((u, "list") for u in list_links)
            queue.extend((u, "item") for u in item_links)
    except BaseException:
        conn.execute(
            "UPDATE crawl_runs SET finished_at=?, status='failed', pages_seen=?, "
            "pages_new=?, pages_unchanged=?, error=? WHERE id=?",
            (state.now(), seen, new, unchanged, "interrupted", run_id),
        )
        raise
    conn.execute(
        "UPDATE crawl_runs SET finished_at=?, status='done', pages_seen=?, "
        "pages_new=?, pages_unchanged=? WHERE id=?",
        (state.now(), seen, new, unchanged, run_id),
    )
    return {"seen": seen, "new": new, "unchanged": unchanged,
            "errors": errors, "run_id": run_id}


# ---------------------------------------------------------------------------
# process entry point
# ---------------------------------------------------------------------------

def acquire_lock(name: str):
    config.ensure_dirs()
    fh = open(config.var_dir() / f"{name}.lock", "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(f"{name} is already running")
    return fh


def main() -> int:
    conn = db.init_db()
    lock = acquire_lock("crawler")
    state.ensure_baseline(conn)
    stop = {"flag": False}

    def handle(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    interval = config.crawl_interval()
    state.event(conn, "crawler", "crawler_started",
                f"Crawler started against {config.site_url()}")
    while not stop["flag"]:
        try:
            result = crawl_once(conn)
            conn.execute(
                "INSERT INTO service_heartbeats(service, at, detail_json) "
                "VALUES('crawler', ?, ?) ON CONFLICT(service) DO UPDATE SET "
                "at=excluded.at, detail_json=excluded.detail_json",
                (state.now(), json.dumps(result)),
            )
        except Exception as exc:  # keep the loop alive; surface the error
            state.event(conn, "crawler", "crawl_failed", str(exc))
            time.sleep(min(5.0, interval))
        deadline = time.time() + interval
        while time.time() < deadline and not stop["flag"]:
            time.sleep(0.1)
    state.event(conn, "crawler", "crawler_stopped", "Crawler stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
