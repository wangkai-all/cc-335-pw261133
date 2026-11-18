"""The rule-epoch state machine and the single-writer publication gate.

The design in one paragraph
---------------------------
The crawler captures bytes once and assigns each new page version an *epoch* at
fetch time (`page_fetches.epoch_id`).  That immutable assignment is the switch
boundary: it records which fetch happened while which rule was live.  Parsers
independently walk every raw page version and write `parsed_documents` keyed by
(fetch, rule), so both rules can process the same page without interacting.  A
separate table, `publication_assignments`, says which *rule's document* the
publisher may use for each page version.  Only the publisher inserts into
`publish_ledger`, whose UNIQUE(version_key) makes "one page version contributes
once" a database invariant rather than a convention.

Mode changes
------------
* start canary: the crawler keeps running; new fetch versions in the sampled
  scope are assigned to the canary rule.  Versions from an earlier, unapproved
  canary are re-homed onto the new canary; their raw bytes are untouched.
* approve / promote: every not-yet-published version in the canary -- including
  pages whose parse landed late or whose fetch preceded approval -- is
  reassigned to the promotion epoch.  The publisher catches up from the shared
  raw store.  Nothing is re-fetched or re-parsed, and nothing publishes twice.
* rollback: versions fetched during the canary that were never published move
  to a recovery epoch pinned to the old rule, so pages already processed by the
  new rule are still delivered -- through the old rule's document.  Versions
  fetched after the rollback are assigned to recovery at crawl time.  Anything
  already published stays published and can never be published again.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from . import rule_sources


class StateError(RuntimeError):
    """A requested transition is not valid in the current state."""


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# events / settings
# ---------------------------------------------------------------------------

def event(conn, actor: str, kind: str, detail: str, **extra) -> None:
    payload: dict[str, Any] = {"message": detail}
    payload.update(extra)
    conn.execute(
        "INSERT INTO events(at, actor, kind, detail) VALUES(?,?,?,?)",
        (now(), actor, kind, json.dumps(payload, ensure_ascii=False)),
    )


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

def ensure_rules(conn) -> tuple[int, int]:
    """Seed the two built-in rule versions once.  Returns (old_id, new_id)."""
    def seed(label: str, source: str, notes: str) -> int:
        sha = hashlib.sha256(source.encode()).hexdigest()
        row = conn.execute("SELECT * FROM rule_versions WHERE label=?", (label,)).fetchone()
        if row:
            if row["source_sha"] != sha:
                raise StateError(
                    f"Built-in rule {label} changed on disk. Add a new version "
                    "instead of mutating one that has parsed documents.")
            return row["id"]
        cur = conn.execute(
            "INSERT INTO rule_versions(label, source, source_sha, created_at, notes) "
            "VALUES(?,?,?,?,?)",
            (label, source, sha, now(), notes),
        )
        return cur.lastrowid

    old_id = seed("parse-old", rule_sources.OLD_RULE,
                  "Original selectors: one .row per item, .brand/.stock as siblings.")
    new_id = seed("parse-new", rule_sources.NEW_RULE,
                  "Redesigned markup: .row-group with sibling title/price parts; "
                  "brand/sku/stock moved to attributes.")
    return old_id, new_id


def rule_row(conn, rule_id: int):
    return conn.execute("SELECT * FROM rule_versions WHERE id=?", (rule_id,)).fetchone()


# ---------------------------------------------------------------------------
# epochs
# ---------------------------------------------------------------------------

def active_epoch(conn):
    return conn.execute(
        "SELECT * FROM rule_epochs WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def canary_epoch(conn):
    return conn.execute(
        "SELECT * FROM rule_epochs WHERE is_canary=1 AND status IN ('active','closed') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def baseline_rule_id(conn, epoch) -> int | None:
    if epoch["origin"] in ("baseline", "recovery"):
        return epoch["rule_id"]
    return epoch["fallback_rule_id"]


def ensure_baseline(conn) -> int:
    ep = active_epoch(conn)
    if ep is not None:
        return ep["id"]
    old_id, _ = ensure_rules(conn)
    reusable = conn.execute(
        "SELECT * FROM rule_epochs WHERE origin IN ('baseline','recovery') "
        "AND rule_id=? AND status='closed' ORDER BY id DESC LIMIT 1", (old_id,)
    ).fetchone()
    if reusable is not None:
        conn.execute(
            "UPDATE rule_epochs SET status='active', closed_at=NULL, "
            "max_fetch_id_at_close=NULL WHERE id=?", (reusable["id"],))
        return reusable["id"]
    cur = conn.execute(
        "INSERT INTO rule_epochs(rule_id, origin, is_canary, publish_mode, status, "
        "opened_at, sample_percent, note) VALUES(?,?,?,?,?,?,?,?)",
        (old_id, "baseline", 0, "auto", "active", now(), 100,
         "Initial corpus fetched with the original rule."),
    )
    eid = cur.lastrowid
    event(conn, "system", "epoch_baseline",
          f"Baseline epoch #{eid} opened with parse-old.", epoch_id=eid)
    return eid


def epoch_summary(conn, ep) -> dict[str, Any]:
    parsed = conn.execute(
        "SELECT COUNT(DISTINCT fetch_id) AS n FROM parsed_documents WHERE rule_id=?",
        (ep["rule_id"],),
    ).fetchone()["n"]
    assigned = conn.execute(
        "SELECT COUNT(*) AS n FROM publication_assignments WHERE epoch_id=?",
        (ep["id"],),
    ).fetchone()["n"]
    published = conn.execute(
        "SELECT COUNT(*) AS n FROM publish_ledger WHERE epoch_id=?", (ep["id"],)
    ).fetchone()["n"]
    rule = rule_row(conn, ep["rule_id"])
    out = {k: ep[k] for k in ep.keys()}
    out["rule_label"] = rule["label"] if rule else "?"
    out["parsed_pages"] = parsed
    out["assigned_versions"] = assigned
    out["published_versions"] = published
    out["scope"] = json.loads(ep["scope_json"] or "{}")
    return out


# ---------------------------------------------------------------------------
# scope sampling
# ---------------------------------------------------------------------------

def in_scope(page_id: str, percent: int, kind: str = "item") -> bool:
    """Deterministic by page identity so a page never straddles the boundary.

    Sampling applies to item pages only: list pages are tiny, must be crawled
    to keep the frontier moving, and carry no inventory fields.
    """
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    if kind != "item":
        return True
    h = hashlib.sha256(page_id.encode()).hexdigest()
    return int(h[:8], 16) % 100 < percent


# ---------------------------------------------------------------------------
# epoch lifecycle
# ---------------------------------------------------------------------------

def start_canary(conn, *, percent: int, actor: str = "operator",
                 rule_id: int | None = None) -> int:
    percent = max(1, min(100, int(percent)))
    if canary_epoch(conn) is not None:
        raise StateError("A canary is already open. Approve or roll it back first.")
    base = active_epoch(conn)
    if base is None:
        raise StateError("No active epoch; start the crawler first.")
    _, default_rule_id = ensure_rules(conn)
    new_id = int(rule_id) if rule_id is not None else default_rule_id
    target = rule_row(conn, new_id)
    if target is None:
        raise StateError(f"unknown rule id {new_id}")
    if base["rule_id"] == new_id:
        raise StateError("The active epoch is already running this rule.")
    snapshot_progress(conn, base["id"], "canary_start_boundary")
    close_epoch(conn, base["id"], status="closed")

    scope = {"percent": percent, "kind": "item"}
    cur = conn.execute(
        "INSERT INTO rule_epochs(rule_id, origin, is_canary, publish_mode, status, "
        "opened_at, sample_percent, scope_json, baseline_epoch_id, parent_epoch_id, "
        "fallback_rule_id, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id, "canary", 1, "hold", "active", now(), percent,
         json.dumps(scope), base["id"], base["id"], base["rule_id"],
         f"Canary {percent}% on {target['label']}"),
    )
    cid = cur.lastrowid
    event(conn, actor, "canary_started",
          f"Canary epoch #{cid} opened on {target['label']} for {percent}% of "
          f"item pages.", epoch_id=cid, percent=percent, rule=target["label"])
    return cid


def promote_epoch(conn, *, actor: str = "operator", note: str = "",
                  diff_report_id: int | None = None) -> int:
    canary = canary_epoch(conn)
    if canary is None:
        raise StateError("No canary is open to approve.")
    if canary["status"] not in ("active", "closed"):
        raise StateError(f"Canary #{canary['id']} is {canary['status']}, not approvable.")
    snapshot_progress(conn, canary["id"], "approval_boundary")
    close_epoch(conn, canary["id"], status="finalized")
    conn.execute(
        "UPDATE rule_epochs SET approved_at=?, approval_note=?, diff_report_id=? "
        "WHERE id=?", (now(), note, diff_report_id, canary["id"]),
    )
    cur = conn.execute(
        "INSERT INTO rule_epochs(rule_id, origin, is_canary, publish_mode, status, "
        "opened_at, sample_percent, scope_json, baseline_epoch_id, parent_epoch_id, "
        "fallback_rule_id, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (canary["rule_id"], "promotion", 0, "auto", "active", now(), 100,
         json.dumps({"percent": 100}), canary["baseline_epoch_id"], canary["id"],
         canary["fallback_rule_id"], note or "Promotion after canary approval."),
    )
    new_id = cur.lastrowid
    moved = move_unpublished_assignments(conn, canary["id"], new_id,
                                         reason="promotion")
    # Held work that never got assigned to the canary (fetched just before the
    # canary opened, parsed late) also belongs to the promoted rule.
    moved += move_unpublished_assignments(conn, canary["baseline_epoch_id"], new_id,
                                          reason="promotion_carry")
    event(conn, actor, "canary_approved",
          f"Canary #{canary['id']} approved; promotion epoch #{new_id} will publish "
          f"{moved} carried-over version(s) from the shared raw store.",
          epoch_id=new_id, from_epoch=canary["id"], carried=moved)
    return new_id


def rollback_epoch(conn, *, actor: str = "operator", note: str = "") -> int:
    canary = canary_epoch(conn)
    if canary is None:
        raise StateError("No canary is open to roll back.")
    if canary["status"] not in ("active", "closed"):
        raise StateError(f"Canary #{canary['id']} is {canary['status']}, not active.")
    old_rule = canary["fallback_rule_id"] or baseline_rule_id(conn, canary)
    if old_rule is None:
        raise StateError("Canary has no fallback rule recorded.")
    snapshot_progress(conn, canary["id"], "rollback_boundary")
    close_epoch(conn, canary["id"], status="rolled_back")
    cur = conn.execute(
        "INSERT INTO rule_epochs(rule_id, origin, is_canary, publish_mode, status, "
        "opened_at, sample_percent, scope_json, baseline_epoch_id, parent_epoch_id, "
        "fallback_rule_id, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (old_rule, "recovery", 0, "auto", "active", now(), 100,
         json.dumps({"percent": 100}), canary["baseline_epoch_id"], canary["id"],
         old_rule, note or "Rollback recovery."),
    )
    new_id = cur.lastrowid
    # Every unpublished version the canary touched -- including pages already
    # fetched and parsed by the new rule -- is delivered through the old rule.
    moved = move_unpublished_assignments(conn, canary["id"], new_id, reason="rollback")
    already = conn.execute(
        "SELECT COUNT(*) AS n FROM publish_ledger WHERE epoch_id=?", (canary["id"],)
    ).fetchone()["n"]
    event(conn, actor, "canary_rolled_back",
          f"Canary #{canary['id']} rolled back. {moved} already-processed version(s) "
          f"recovered via parse-old in recovery epoch #{new_id}; {already} "
          f"version(s) already published stay published and are not counted again.",
          epoch_id=new_id, from_epoch=canary["id"], moved=moved,
          already_published=already)
    return new_id


def reconcile_assignments(conn) -> int:
    """Re-home unpublished versions stranded by a boundary change.

    A crawler write can land just after a promotion/rollback transaction
    committed, pointing at the epoch that was live a moment earlier.  Unpublished
    versions pointing at a terminal epoch (rolled-back canary, finalized canary,
    closed promotion, or a baseline superseded by promotion/recovery) are moved
    to the active epoch, so they are delivered by the rule that is now live.
    Versions assigned to a *closed baseline while a canary is open* are left
    alone: that assignment is the deliberate out-of-scope routing.
    """
    active = active_epoch(conn)
    if active is None:
        return 0
    canary_open = canary_epoch(conn) is not None
    rows = conn.execute(
        """SELECT a.fetch_id, a.epoch_id, e.origin, e.status, e.is_canary
             FROM publication_assignments a
             JOIN rule_epochs e ON e.id = a.epoch_id
             LEFT JOIN page_fetches f ON f.id = a.fetch_id
             LEFT JOIN publish_ledger l ON l.version_key = f.version_key
            WHERE a.epoch_id <> ? AND l.seq IS NULL""",
        (active["id"],),
    ).fetchall()
    moved = 0
    for row in rows:
        terminal = (
            row["status"] == "rolled_back"
            or (row["is_canary"] and row["status"] == "finalized")
            or (row["origin"] == "promotion" and row["status"] == "closed")
            or (row["origin"] == "baseline" and row["status"] == "closed"
                and not canary_open)
        )
        if not terminal:
            continue
        conn.execute(
            "UPDATE publication_assignments SET epoch_id=?, rule_id=?, "
            "assigned_at=?, reassigned_from=COALESCE(reassigned_from, epoch_id), "
            "reason=? WHERE fetch_id=? AND epoch_id=?",
            (active["id"], active["rule_id"], now(), "reconcile",
             row["fetch_id"], row["epoch_id"]),
        )
        moved += 1
    if moved:
        event(conn, "publisher", "assignments_reconciled",
              f"{moved} stranded version(s) re-homed to epoch #{active['id']}.",
              moved=moved)
    return moved


def close_epoch(conn, epoch_id: int, *, status: str) -> None:
    conn.execute(
        "UPDATE rule_epochs SET status=?, closed_at=?, max_fetch_id_at_close=? "
        "WHERE id=? AND status='active'",
        (status, now(), max_fetch_id(conn), epoch_id),
    )


def snapshot_progress(conn, epoch_id: int, note: str = "") -> dict:
    """Record per-rule coverage at a boundary; used by the recovery UI."""
    epoch = conn.execute("SELECT * FROM rule_epochs WHERE id=?", (epoch_id,)).fetchone()
    if epoch is None:
        return {}
    done = conn.execute(
        "SELECT COUNT(*) AS n FROM parsed_documents WHERE rule_id=?", (epoch["rule_id"],)
    ).fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM page_fetches").fetchone()["n"]
    latest = conn.execute(
        "SELECT COALESCE(MAX(fetch_id),0) AS n FROM parsed_documents WHERE rule_id=?",
        (epoch["rule_id"],),
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO progress_checkpoints(at, epoch_id, rule_id, processed, "
        "total_versions, latest_fetch_id, note) VALUES(?,?,?,?,?,?,?)",
        (now(), epoch_id, epoch["rule_id"], done, total, latest, note),
    )
    return {"epoch_id": epoch_id, "rule_id": epoch["rule_id"], "processed": done,
            "total": total, "latest_fetch_id": latest}


def max_fetch_id(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id),0) AS n FROM page_fetches").fetchone()
    return row["n"]


def move_unpublished_assignments(conn, from_epoch: int, to_epoch: int,
                                 reason: str) -> int:
    """Move assignment rows whose versions have no ledger entry yet.

    Already-published versions are untouched: their ledger row is the record of
    what was published and the UNIQUE key prevents any second contribution.
    """
    if from_epoch is None:
        return 0
    target = conn.execute("SELECT * FROM rule_epochs WHERE id=?", (to_epoch,)).fetchone()
    if target is None:
        return 0
    rows = conn.execute(
        """SELECT a.fetch_id FROM publication_assignments a
             JOIN page_fetches f ON f.id = a.fetch_id
             LEFT JOIN publish_ledger l ON l.version_key = f.version_key
            WHERE a.epoch_id=? AND l.seq IS NULL""",
        (from_epoch,),
    ).fetchall()
    ids = [r["fetch_id"] for r in rows]
    for fid in ids:
        conn.execute(
            "UPDATE publication_assignments SET epoch_id=?, rule_id=?, "
            "assigned_at=?, reassigned_from=COALESCE(reassigned_from, epoch_id), "
            "reason=? WHERE fetch_id=? AND epoch_id=?",
            (to_epoch, target["rule_id"], now(), reason, fid, from_epoch),
        )
    return len(ids)


# ---------------------------------------------------------------------------
# the single-writer gate
# ---------------------------------------------------------------------------

@dataclass
class PublishResult:
    published: bool
    reason: str = ""
    version_key: str = ""


def publish_document(conn, fetch_id: int, doc_id: int) -> PublishResult:
    """Publish one parsed document into the inventory, exactly once per version.

    The UNIQUE constraints make concurrent publishers safe under SQLite's own
    locking:
      * publish_ledger.version_key -> one payload publishes once, ever
      * inventory.fetch_id check  -> a late older version never regresses a page
    """
    doc = conn.execute(
        "SELECT d.*, f.version_key, f.page_id, f.epoch_id AS fetch_epoch, f.kind "
        "FROM parsed_documents d JOIN page_fetches f ON f.id=d.fetch_id "
        "WHERE d.id=?",
        (doc_id,),
    ).fetchone()
    if doc is None:
        return PublishResult(False, "document_missing")
    assignment = conn.execute(
        "SELECT * FROM publication_assignments WHERE fetch_id=?", (fetch_id,)
    ).fetchone()
    if assignment is None:
        return PublishResult(False, "no_assignment", doc["version_key"])
    if assignment["rule_id"] != doc["rule_id"]:
        reject(conn, doc, "rule_not_assigned")
        return PublishResult(False, "rule_not_assigned", doc["version_key"])
    epoch = conn.execute(
        "SELECT * FROM rule_epochs WHERE id=?", (assignment["epoch_id"],)
    ).fetchone()
    if epoch is None:
        return PublishResult(False, "epoch_missing", doc["version_key"])
    if epoch["status"] == "rolled_back" or (
        epoch["is_canary"] and epoch["status"] != "finalized"
    ):
        reject(conn, doc, "epoch_not_approved")
        return PublishResult(False, "epoch_not_approved", doc["version_key"])

    inv = conn.execute(
        "SELECT * FROM inventory WHERE page_id=?", (doc["page_id"],)
    ).fetchone()
    if inv is not None and inv["fetch_id"] > fetch_id:
        reject(conn, doc, "superseded")
        return PublishResult(False, "superseded_by_newer_fetch", doc["version_key"])

    try:
        conn.execute(
            "INSERT INTO publish_ledger(version_key, fetch_id, doc_id, epoch_id, "
            "rule_id, published_at) VALUES(?,?,?,?,?,?)",
            (doc["version_key"], fetch_id, doc_id, epoch["id"], doc["rule_id"], now()),
        )
    except sqlite3.IntegrityError:
        reject(conn, doc, "already_published")
        return PublishResult(False, "already_published", doc["version_key"])

    data = json.loads(doc["doc_json"])
    payload = {"kind": data.get("kind"), "fields": data.get("fields", {}),
               "rows": data.get("rows", []), "url": data.get("url")}
    conn.execute(
        "INSERT INTO inventory(page_id, fetch_id, doc_id, epoch_id, rule_id, "
        "data_json, updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(page_id) DO UPDATE SET fetch_id=excluded.fetch_id, "
        "doc_id=excluded.doc_id, epoch_id=excluded.epoch_id, "
        "rule_id=excluded.rule_id, data_json=excluded.data_json, "
        "updated_at=excluded.updated_at",
        (doc["page_id"], fetch_id, doc_id, epoch["id"], doc["rule_id"],
         json.dumps(payload, ensure_ascii=False), now()),
    )
    return PublishResult(True, "published", doc["version_key"])


def reject(conn, doc, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO publish_rejections(version_key, fetch_id, doc_id, "
        "epoch_id, rule_id, reason, at) VALUES(?,?,?,?,?,?,?)",
        (doc["version_key"], doc["fetch_id"], doc["id"], doc["epoch_id"],
         doc["rule_id"], reason, now()),
    )
