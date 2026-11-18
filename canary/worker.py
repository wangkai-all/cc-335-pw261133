"""Parser worker.  There is one process per rule version.

    python -m canary.worker --rule parse-old --name old-1
    python -m canary.worker --rule parse-new --name new-1   # the canary parser

Both processes read the same raw store and the same `page_fetches` queue; they
never see each other's output except in `parsed_documents`, keyed by
(fetch_id, rule_id).  Progress is *derived* on startup from the durable parsed
documents table for this rule -- not from a saved integer.  A cursor integer
would be wrong after a rule is re-run, a process restarts mid-epoch, or a page
is re-parsed; the table is the truth.  The worker only ever moves forward.

A worker does not publish.  It inserts documents and asks the publisher
(command `tick` on canary.publisher, run on a schedule) to drain its output.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
import traceback
import uuid

from . import config, db, rawstore, state, rule_sources
from .htmltree import parse_html


# ---------------------------------------------------------------------------
# rule loading
# ---------------------------------------------------------------------------

def compile_rule(conn, rule_id: int):
    row = state.rule_row(conn, rule_id)
    if row is None:
        raise SystemExit(f"unknown rule id {rule_id}")
    namespace = dict(rule_sources.RULE_BUILTINS)
    namespace["__builtins__"] = {"__build_class__": __build_class__, "__import__": _safe_import}
    exec(compile(row["source"], f"<rule:{row['label']}>", "exec"), namespace)
    return namespace


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    allowed = {"re", "urllib.parse", "json", "math", "html"}
    if name not in allowed:
        raise ImportError(f"rule may not import {name!r}")
    return __import__(name, globals, locals, fromlist, level)


# ---------------------------------------------------------------------------
# parsing one page
# ---------------------------------------------------------------------------

def fields_for(doc: dict) -> dict:
    """Flatten a document into a comparable field map.

    List documents compare structurally: row count plus the fields of each row
    keyed by its declared key, which is what surfaces split rows (the old rule
    sees two .row entries for one redesigned group, with the price or title
    blanked out).
    """
    out: dict[str, object] = {}
    if doc["kind"] == "item":
        for name, value in (doc.get("fields") or {}).items():
            out[name] = value
        out["__kind"] = "item"
        out["description_length"] = len(str(out.get("description") or ""))
        return out
    rows = doc.get("rows") or []
    out["__kind"] = "list"
    out["__row_count"] = len(rows)
    keys = [r.get("key") or f"#row{i}" for i, r in enumerate(rows)]
    out["__row_keys"] = keys
    out["__duplicate_keys"] = sorted({k for k in keys if keys.count(k) > 1})
    for i, row in enumerate(rows):
        key = row.get("key") or f"#row{i}"
        prefix = f"row[{key}]"
        for fname, value in (row.get("fields") or {}).items():
            out[f"{prefix}.{fname}"] = value
    return out


def parse_one(rule_ns, raw: bytes, url: str) -> dict:
    markup = raw.decode("utf-8", "ignore")
    root = parse_html(markup)
    kind = "list" if "/category/" in url else "item"
    if kind == "list":
        doc = rule_ns["parse_list"](root, url)
    else:
        doc = rule_ns["parse_item"](root, url)
    doc["url"] = url
    doc.pop("links", None)
    return doc


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------

def progress(conn, rule_id: int) -> dict:
    """Next fetch id that lacks a document for this rule, plus counters."""
    row = conn.execute(
        """SELECT MIN(f.id) AS next_id,
                  (SELECT COUNT(*) FROM parsed_documents d WHERE d.rule_id=?) AS done,
                  (SELECT MAX(fetch_id) FROM parsed_documents
                    WHERE rule_id=?) AS last_done,
                  (SELECT COUNT(*) FROM page_fetches) AS total
             FROM page_fetches f
             LEFT JOIN parsed_documents d
                    ON d.fetch_id=f.id AND d.rule_id=?
            WHERE d.id IS NULL""",
        (rule_id, rule_id, rule_id),
    ).fetchone()
    next_id = row["next_id"]
    done = row["done"]
    total = row["total"]
    return {
        # next_id is the true resume point; when nothing is pending it is 0 and
        # the UI should show last_done instead of a meaningless zero.
        "next_fetch_id": next_id if next_id is not None else 0,
        "last_processed_fetch_id": row["last_done"] or 0,
        "processed": done,
        "total": total,
        "remaining": max(0, total - done),
    }


def instance_row(conn, instance_id: str):
    return conn.execute("SELECT * FROM parser_instances WHERE id=?", (instance_id,)).fetchone()


def start_instance(conn, rule_id: int, name: str) -> str:
    instance_id = name or f"parser-{uuid.uuid4().hex[:8]}"
    existing = instance_row(conn, instance_id)
    # Reusing a name is allowed (restart); the new pid supersedes the old row.
    conn.execute(
        "INSERT INTO parser_instances(id, rule_id, pid, started_at, heartbeat_at, "
        "status, last_fetch_id) VALUES(?,?,?,?,?, 'running', 0) "
        "ON CONFLICT(id) DO UPDATE SET rule_id=excluded.rule_id, pid=excluded.pid, "
        "started_at=excluded.started_at, heartbeat_at=excluded.heartbeat_at, "
        "status='running', last_error=NULL, exit_note=NULL",
        (instance_id, rule_id, os.getpid(), state.now(), state.now()),
    )
    _ = existing
    return instance_id


def heartbeat(conn, instance_id: str, *, last_fetch_id: int, parsed: int,
              errors: int, last_error: str | None = None) -> None:
    conn.execute(
        "UPDATE parser_instances SET heartbeat_at=?, last_fetch_id=?, "
        "parsed_count=?, error_count=?, last_error=COALESCE(?, last_error), "
        "status='running' WHERE id=?",
        (state.now(), last_fetch_id, parsed, errors, last_error, instance_id),
    )


# ---------------------------------------------------------------------------
# the worker loop
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self, conn, rule_id: int, instance_id: str, *,
                 max_attempts: int = 3, poll_interval: float | None = None):
        self.conn = conn
        self.rule_id = rule_id
        self.instance_id = instance_id
        self.max_attempts = max_attempts
        self.poll_interval = poll_interval if poll_interval is not None else \
            config.parser_poll_interval()
        self.ns = compile_rule(conn, rule_id)
        self.parsed = 0
        self.errors = 0
        self.handled: set[int] = set()

    def pending(self, limit: int = 20) -> list:
        """Versions this rule has not successfully parsed, retry-bounded.

        Written with NOT EXISTS rather than a join on parse_attempts so a page
        with several failed attempts appears exactly once in the batch.
        """
        rows = self.conn.execute(
            """SELECT f.* FROM page_fetches f
                WHERE NOT EXISTS (
                          SELECT 1 FROM parsed_documents d
                           WHERE d.fetch_id=f.id AND d.rule_id=?)
                  AND COALESCE((
                          SELECT MAX(a.attempt_no) FROM parse_attempts a
                           WHERE a.fetch_id=f.id AND a.rule_id=?
                             AND a.status='error'), 0) < ?
                ORDER BY f.id LIMIT ?""",
            (self.rule_id, self.rule_id, self.max_attempts, limit),
        ).fetchall()
        return rows

    def process_fetch(self, fetch) -> bool:
        raw = rawstore.get(fetch["raw_path"], fetch["content_sha"])
        doc = parse_one(self.ns, raw, fetch["url"])
        fields = fields_for(doc)
        with db.tx(self.conn):
            attempt_no = self.conn.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM parse_attempts "
                "WHERE fetch_id=? AND rule_id=?",
                (fetch["id"], self.rule_id),
            ).fetchone()["n"]
            try:
                self.conn.execute(
                    "INSERT INTO parsed_documents(fetch_id, rule_id, epoch_id, "
                    "doc_json, fields_json, rows_json, row_count, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(fetch_id, rule_id) DO NOTHING",
                    (fetch["id"], self.rule_id, fetch["epoch_id"],
                     json.dumps(doc, ensure_ascii=False),
                     json.dumps(fields, ensure_ascii=False),
                     json.dumps(doc.get("rows") or [], ensure_ascii=False),
                     len(doc.get("rows") or []), state.now()),
                )
            except sqlite3.IntegrityError:
                pass
            self.conn.execute(
                "INSERT INTO parse_attempts(rule_id, parser_id, fetch_id, "
                "attempt_no, status, parsed_at) VALUES(?,?,?,?,?,?)",
                (self.rule_id, self.instance_id, fetch["id"], attempt_no, "ok",
                 state.now()),
            )
        self.handled.add(fetch["id"])
        self.parsed += 1
        return True

    def run_once(self, budget: int = 25) -> dict:
        batch = self.pending(limit=budget)
        processed = 0
        for fetch in batch:
            try:
                self.process_fetch(fetch)
                processed += 1
            except Exception as exc:  # noqa: BLE001 - per-page isolation
                self.errors += 1
                detail = f"{type(exc).__name__}: {exc}"
                with db.tx(self.conn):
                    attempt_no = self.conn.execute(
                        "SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM parse_attempts "
                        "WHERE fetch_id=? AND rule_id=?",
                        (fetch["id"], self.rule_id),
                    ).fetchone()["n"]
                    self.conn.execute(
                        "INSERT INTO parse_attempts(rule_id, parser_id, fetch_id, "
                        "attempt_no, status, error, parsed_at) VALUES(?,?,?,?,?,?,?)",
                        (self.rule_id, self.instance_id, fetch["id"], attempt_no,
                         "error", detail, state.now()),
                    )
                    state.event(self.conn, self.instance_id, "parse_error",
                                f"page {fetch['page_id']} version {fetch['id']}: {detail}")
                heartbeat(self.conn, self.instance_id,
                          last_fetch_id=fetch["id"], parsed=self.parsed,
                          errors=self.errors, last_error=detail)
        prog = progress(self.conn, self.rule_id)
        heartbeat(self.conn, self.instance_id,
                  last_fetch_id=prog["next_fetch_id"], parsed=self.parsed,
                  errors=self.errors)
        return {"processed": processed, "remaining": prog["remaining"]}


def setup_signals(stop: dict) -> None:
    def handle(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse raw pages with one rule.")
    parser.add_argument("--rule", required=True, help="rule label, e.g. parse-old")
    parser.add_argument("--name", default=None, help="stable instance id")
    parser.add_argument("--once", action="store_true", help="drain and exit")
    parser.add_argument("--poll", type=float, default=None)
    args = parser.parse_args(argv)

    conn = db.init_db()
    row = conn.execute("SELECT * FROM rule_versions WHERE label=?", (args.rule,)).fetchone()
    if row is None:
        state.ensure_rules(conn)
        row = conn.execute("SELECT * FROM rule_versions WHERE label=?", (args.rule,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown rule {args.rule!r}")
    instance_id = start_instance(conn, row["id"], args.name or f"{args.rule}-{os.getpid()}")
    stop = {"flag": False}
    setup_signals(stop)
    worker = Worker(conn, row["id"], instance_id, poll_interval=args.poll)
    state.event(conn, instance_id, "parser_started",
                f"{args.rule} parser online, instance {instance_id}.",
                rule_id=row["id"])
    exit_status = "stopped"
    try:
        while not stop["flag"]:
            result = worker.run_once()
            if args.once and result["remaining"] == 0:
                break
            if result["processed"] == 0:
                time.sleep(worker.poll_interval)
    except BaseException:
        exit_status = "crashed"
        conn.execute(
            "UPDATE parser_instances SET status='crashed', last_error=? WHERE id=?",
            (traceback.format_exc()[-2000:], instance_id),
        )
        state.event(conn, instance_id, "parser_crashed",
                    f"{args.rule} parser {instance_id} crashed.")
        raise
    finally:
        if exit_status != "crashed":
            conn.execute(
                "UPDATE parser_instances SET status='stopped', exit_note=? WHERE id=?",
                ("drained" if args.once else "signal", instance_id),
            )
            state.event(conn, instance_id, "parser_stopped",
                        f"{args.rule} parser {instance_id} stopped "
                        f"after {worker.parsed} page(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
