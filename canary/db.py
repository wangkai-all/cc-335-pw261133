"""SQLite bootstrap and small transaction helpers.

The schema makes the two invariants this app exists for explicit:

* `page_fetches.version_key` is UNIQUE.  A page payload is stored once and
  shared by every parser process; re-fetching identical bytes never creates a
  second unit of work.
* `publish_ledger.version_key` is UNIQUE.  No matter how many rule epochs or
  parser processes produced a document for that payload, it contributes to the
  published inventory at most once.  First approved writer wins.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS pages (
    id            TEXT PRIMARY KEY,          -- stable path identity, e.g. item/a1
    url           TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- list | item | other
    list_url      TEXT,                      -- page of origin (items)
    first_seen_at REAL NOT NULL,
    last_fetch_id INTEGER
);

-- One row per distinct raw payload ever fetched.  Raw bytes live on disk under
-- raw/<h2>/<sha>.gz; this row is the shared hand-off between crawler and all
-- parser processes.
CREATE TABLE IF NOT EXISTS page_fetches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id       TEXT NOT NULL REFERENCES pages(id),
    url           TEXT NOT NULL,
    kind          TEXT NOT NULL,
    version_key   TEXT NOT NULL UNIQUE,      -- page_id + ':' + sha256(raw)
    content_sha   TEXT NOT NULL,
    raw_path      TEXT NOT NULL,
    http_status   INTEGER NOT NULL,
    etag          TEXT,
    fetched_at    REAL NOT NULL,
    -- immutable crawl-time epoch: the switch boundary audit for each version
    epoch_id      INTEGER NOT NULL REFERENCES rule_epochs(id),
    is_list_page  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fetch_page ON page_fetches(page_id, id);
CREATE INDEX IF NOT EXISTS idx_fetch_epoch ON page_fetches(epoch_id);
CREATE INDEX IF NOT EXISTS idx_fetch_kind ON page_fetches(kind);

CREATE TABLE IF NOT EXISTS rule_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,
    source_sha  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    notes       TEXT NOT NULL DEFAULT ''
);

-- An epoch is one publication regime.  `is_canary=1` epochs hold their output
-- until a human approves the field-and-row comparison; baseline/promotion/
-- recovery epochs publish automatically.  scope_json decides which newly
-- fetched pages are assigned to the canary.
CREATE TABLE IF NOT EXISTS rule_epochs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id        INTEGER NOT NULL REFERENCES rule_versions(id),
    origin         TEXT NOT NULL DEFAULT 'baseline', -- baseline|canary|promotion|recovery
    is_canary      INTEGER NOT NULL DEFAULT 0,
    publish_mode   TEXT NOT NULL DEFAULT 'auto',     -- auto | hold
    status         TEXT NOT NULL DEFAULT 'active',   -- active|closed|finalized|rolled_back
    opened_at      REAL NOT NULL,
    closed_at      REAL,
    sample_percent INTEGER NOT NULL DEFAULT 100,
    scope_json     TEXT NOT NULL DEFAULT '{}',
    baseline_epoch_id INTEGER REFERENCES rule_epochs(id),
    parent_epoch_id INTEGER REFERENCES rule_epochs(id),
    fallback_rule_id INTEGER REFERENCES rule_versions(id),
    approved_at    REAL,
    approval_note  TEXT NOT NULL DEFAULT '',
    diff_report_id INTEGER,
    -- safety net: highest page_fetches.id that existed when this epoch stopped
    -- being assignable.  Anything at or below it belongs to this epoch's audit
    -- window even if parsers are still catching up.
    max_fetch_id_at_close INTEGER,
    note           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_epoch_status ON rule_epochs(status, is_canary);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The publisher reads this table, never page_fetches.epoch_id, to decide which
-- rule's document may be published.  Rollback moves not-yet-published rows to
-- a recovery epoch; already-published versions are never moved and can never
-- contribute again (ledger uniqueness).
CREATE TABLE IF NOT EXISTS publication_assignments (
    fetch_id      INTEGER PRIMARY KEY REFERENCES page_fetches(id),
    epoch_id      INTEGER NOT NULL REFERENCES rule_epochs(id),
    rule_id       INTEGER NOT NULL REFERENCES rule_versions(id),
    assigned_at   REAL NOT NULL,
    reassigned_from INTEGER REFERENCES rule_epochs(id),
    reason        TEXT NOT NULL DEFAULT 'fetch'
);
CREATE INDEX IF NOT EXISTS idx_assignment_epoch ON publication_assignments(epoch_id);

-- One row per parser OS process.  Progress survives a crash in
-- parsed_documents; the cursor is recomputed from that durable table, while
-- last_fetch_id is just the human-readable position.
CREATE TABLE IF NOT EXISTS parser_instances (
    id          TEXT PRIMARY KEY,
    rule_id     INTEGER NOT NULL REFERENCES rule_versions(id),
    pid         INTEGER NOT NULL,
    started_at  REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running', -- running | stopped | crashed
    last_fetch_id INTEGER NOT NULL DEFAULT 0,
    parsed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    exit_note   TEXT
);
CREATE INDEX IF NOT EXISTS idx_parser_rule ON parser_instances(rule_id, started_at);

CREATE TABLE IF NOT EXISTS parse_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       INTEGER NOT NULL REFERENCES rule_versions(id),
    parser_id     TEXT NOT NULL REFERENCES parser_instances(id),
    fetch_id      INTEGER NOT NULL REFERENCES page_fetches(id),
    attempt_no    INTEGER NOT NULL,
    status        TEXT NOT NULL,             -- ok | error
    error         TEXT,
    parsed_at     REAL NOT NULL,
    UNIQUE(fetch_id, rule_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_attempt_rule ON parse_attempts(rule_id, fetch_id);

-- One row per (fetch, rule) that completed successfully.  Immutable: a page
-- parsed under both rules yields two documents, never two inventory rows.
CREATE TABLE IF NOT EXISTS parsed_documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id      INTEGER NOT NULL REFERENCES page_fetches(id),
    rule_id       INTEGER NOT NULL REFERENCES rule_versions(id),
    epoch_id      INTEGER REFERENCES rule_epochs(id),
    doc_json      TEXT NOT NULL,
    fields_json   TEXT NOT NULL,
    rows_json     TEXT NOT NULL,
    row_count     INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    UNIQUE(fetch_id, rule_id)
);
CREATE INDEX IF NOT EXISTS idx_doc_rule ON parsed_documents(rule_id, id);
CREATE INDEX IF NOT EXISTS idx_doc_fetch ON parsed_documents(fetch_id);

CREATE TABLE IF NOT EXISTS diff_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch_id      INTEGER NOT NULL REFERENCES rule_epochs(id),
    old_rule_id   INTEGER NOT NULL,
    new_rule_id   INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    status        TEXT NOT NULL,             -- pass | review | fail
    sample_size   INTEGER NOT NULL,
    signature     TEXT NOT NULL,
    summary_json  TEXT NOT NULL,
    field_json    TEXT NOT NULL,
    row_json      TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    pages_json    TEXT NOT NULL,
    boundary_fetch_id INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_diff_epoch ON diff_reports(epoch_id, id);

-- THE single-writer gate for the in-database store.
CREATE TABLE IF NOT EXISTS publish_ledger (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_key   TEXT NOT NULL UNIQUE,
    fetch_id      INTEGER NOT NULL REFERENCES page_fetches(id),
    doc_id        INTEGER NOT NULL REFERENCES parsed_documents(id),
    epoch_id      INTEGER NOT NULL REFERENCES rule_epochs(id),
    rule_id       INTEGER NOT NULL REFERENCES rule_versions(id),
    published_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_epoch ON publish_ledger(epoch_id, seq);
CREATE INDEX IF NOT EXISTS idx_ledger_fetch ON publish_ledger(fetch_id);

-- The published inventory: exactly one current version per page.  The publisher
-- is the only writer.  fetch_id only moves forward, so a late-arriving older
-- canary version can never regress the published state.
CREATE TABLE IF NOT EXISTS inventory (
    page_id       TEXT PRIMARY KEY REFERENCES pages(id),
    fetch_id      INTEGER NOT NULL REFERENCES page_fetches(id),
    doc_id        INTEGER NOT NULL REFERENCES parsed_documents(id),
    epoch_id      INTEGER NOT NULL REFERENCES rule_epochs(id),
    rule_id       INTEGER NOT NULL REFERENCES rule_versions(id),
    data_json     TEXT NOT NULL,
    updated_at    REAL NOT NULL
);

-- Deduplicated audit of the gate's refusals, so the UI can explain why a
-- document did not double-publish.
CREATE TABLE IF NOT EXISTS publish_rejections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_key   TEXT NOT NULL,
    fetch_id      INTEGER NOT NULL,
    doc_id        INTEGER NOT NULL,
    epoch_id      INTEGER NOT NULL,
    rule_id       INTEGER NOT NULL,
    reason        TEXT NOT NULL,           -- already_published|rule_not_assigned|epoch_not_approved|no_assignment
    at            REAL NOT NULL,
    UNIQUE(version_key, doc_id, reason)
);

-- Durable progress snapshot taken at every rule switch.  Parsers recompute
-- their cursor from parsed_documents, but these rows are the audit trail of
-- what each rule had covered when the boundary closed.
CREATE TABLE IF NOT EXISTS progress_checkpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    epoch_id    INTEGER NOT NULL REFERENCES rule_epochs(id),
    rule_id     INTEGER NOT NULL REFERENCES rule_versions(id),
    processed   INTEGER NOT NULL,
    total_versions INTEGER NOT NULL,
    latest_fetch_id INTEGER NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_epoch ON progress_checkpoints(epoch_id, id);

CREATE TABLE IF NOT EXISTS service_heartbeats (
    service     TEXT PRIMARY KEY,
    at          REAL NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT NOT NULL DEFAULT 'running',
    pages_seen  INTEGER NOT NULL DEFAULT 0,
    pages_new   INTEGER NOT NULL DEFAULT 0,
    pages_unchanged INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL NOT NULL,
    actor     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);

CREATE TABLE IF NOT EXISTS site_mutations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL NOT NULL,
    variant   TEXT NOT NULL,
    edition   INTEGER NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread (parsers use threads in tests)."""
    key = str(config.db_path())
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != key:
        if conn is not None:
            conn.close()
        conn = connect()
        _local.conn = conn
        _local.path = key
    return conn


@contextmanager
def tx(conn: sqlite3.Connection | None = None):
    c = conn or get_conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def init_db(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn
