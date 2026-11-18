"""Publisher: the only process that writes the published inventory.

    python -m canary.publisher --once     # drain once (used by app + tests)

It repeatedly asks the single-writer gate in `state.publish_document` to publish
documents whose page version is assigned to a rule epoch.  Because the gate is
keyed by `version_key` (page + content hash) and the ledger column is UNIQUE,
concurrent or repeated publishers cannot double-count a page version: the
loser gets an IntegrityError and is recorded as a rejection.

The publisher never re-fetches and never re-parses.  Promotion catch-up is just
a publisher drain over the raw store after assignments moved.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from . import db, state


def drain(conn, *, limit: int = 200, actor: str = "publisher") -> dict:
    """Publish every ready document whose epoch is allowed to write.

    Ready means: a parsed document exists for the assigned rule and the page
    version has no ledger row yet.
    """
    moved = state.reconcile_assignments(conn)
    rows = conn.execute(
        """SELECT d.id AS doc_id, f.id AS fetch_id, a.epoch_id, a.rule_id,
                  e.is_canary, e.status AS epoch_status, e.approved_at
             FROM publication_assignments a
             JOIN page_fetches f ON f.id = a.fetch_id
             JOIN parsed_documents d
                  ON d.fetch_id = f.id AND d.rule_id = a.rule_id
             JOIN rule_epochs e ON e.id = a.epoch_id
             LEFT JOIN publish_ledger l ON l.version_key = f.version_key
            WHERE l.seq IS NULL
              AND f.kind IN ('item', 'list')
              AND (e.is_canary = 0 OR e.approved_at IS NOT NULL)
              AND e.status <> 'rolled_back'
            ORDER BY f.id
            LIMIT ?""",
        (limit,),
    ).fetchall()
    published = 0
    rejected = 0
    reasons: dict[str, int] = {}
    for row in rows:
        result = state.publish_document(conn, row["fetch_id"], row["doc_id"])
        if result.published:
            published += 1
        else:
            rejected += 1
            reasons[result.reason] = reasons.get(result.reason, 0) + 1
    if published:
        state.event(conn, actor, "publish_drain",
                    f"Published {published} version(s); {rejected} held.",
                    published=published, rejected=rejected)
    return {"published": published, "rejected": rejected, "reasons": reasons,
            "scanned": len(rows), "reassigned": moved}


def queue_counts(conn) -> dict:
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM publication_assignments a
                JOIN rule_epochs e ON e.id=a.epoch_id
                LEFT JOIN publish_ledger l ON l.version_key=(
                    SELECT version_key FROM page_fetches WHERE id=a.fetch_id)
               WHERE l.seq IS NULL AND e.is_canary=1 AND e.approved_at IS NULL)
                AS held_by_canary,
             (SELECT COUNT(*) FROM publish_ledger) AS published_total,
             (SELECT COUNT(*) FROM page_fetches
               WHERE kind IN ('item','list')) AS versions_total,
             (SELECT COUNT(*) FROM publication_assignments a
                JOIN page_fetches f ON f.id=a.fetch_id
                JOIN parsed_documents d
                  ON d.fetch_id=a.fetch_id AND d.rule_id=a.rule_id
                LEFT JOIN publish_ledger l ON l.version_key=f.version_key
               WHERE l.seq IS NULL AND f.kind IN ('item','list')) AS ready_unpublished"""
    ).fetchone()
    return dict(row)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Drain parsed documents into inventory.")
    ap.add_argument("--once", action="store_true", help="drain and exit")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args(argv)
    conn = db.init_db()
    if args.once:
        print(json.dumps(drain(conn), ensure_ascii=False))
        return 0
    stop = {"flag": False}

    def handle(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    while not stop["flag"]:
        drain(conn, limit=args.limit)
        conn.execute(
            "INSERT INTO service_heartbeats(service, at, detail_json) "
            "VALUES('publisher', ?, ?) ON CONFLICT(service) DO UPDATE SET "
            "at=excluded.at, detail_json=excluded.detail_json",
            (state.now(), json.dumps(queue_counts(conn))),
        )
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
