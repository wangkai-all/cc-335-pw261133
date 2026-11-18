"""Shared test harness: isolated var dir + real HTTP practice site."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_prev = {k: os.environ.get(k) for k in ("CANARY_VAR", "CANARY_DB", "CANARY_RAW_DIR")}
_TMP = tempfile.mkdtemp(prefix="canary-test-")
os.environ["CANARY_VAR"] = _TMP
os.environ.pop("CANARY_DB", None)
os.environ.pop("CANARY_RAW_DIR", None)

import canary.config as config  # noqa: E402
from canary import crawler, db, diff, publisher, site, state, worker  # noqa: E402


def fresh(name: str = "case"):
    """Give each test its own database and raw store inside the temp dir."""
    base = Path(_TMP) / name
    os.environ["CANARY_VAR"] = str(base)
    os.environ.pop("CANARY_DB", None)
    os.environ.pop("CANARY_RAW_DIR", None)
    config.ensure_dirs()
    return db.init_db()


def make_site(variant: str = "v1"):
    st = site.SiteState()
    st.set_variant(variant)
    srv = site.PracticeSite(st).start()
    return srv, st


def run_parser(conn, label: str, name: str | None = None, *, max_batches: int = 200):
    row = conn.execute("SELECT * FROM rule_versions WHERE label=?", (label,)).fetchone()
    assert row is not None, label
    iid = worker.start_instance(conn, row["id"], name or f"{label}-test")
    w = worker.Worker(conn, row["id"], iid)
    batches = 0
    while True:
        r = w.run_once(50)
        batches += 1
        if r["remaining"] == 0 or batches >= max_batches:
            break
    return w


def rule_ids(conn):
    rows = conn.execute("SELECT id, label FROM rule_versions").fetchall()
    return {r["label"]: r["id"] for r in rows}


def counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "fetches": q("SELECT COUNT(*) FROM page_fetches"),
        "docs": q("SELECT COUNT(*) FROM parsed_documents"),
        "published": q("SELECT COUNT(*) FROM publish_ledger"),
        "inventory": q("SELECT COUNT(*) FROM inventory"),
        "assignments": q("SELECT COUNT(*) FROM publication_assignments"),
    }
