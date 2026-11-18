"""Core invariants of the rule-canary pipeline (no web layer)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from canary import crawler, diff, publisher, state, worker  # noqa: E402


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.conn = harness.fresh(self.id().split(".")[-1])
        self.srv, self.site = harness.make_site("v1")
        state.ensure_baseline(self.conn)

    def tearDown(self):
        self.srv.stop()

    def crawl(self):
        return crawler.crawl_once(self.conn, self.srv.url)

    def parse_both(self):
        harness.run_parser(self.conn, "parse-old", "old-test")
        harness.run_parser(self.conn, "parse-new", "new-test")

    # -- shared raw page store -----------------------------------------
    def test_raw_pages_are_shared_and_deduped(self):
        self.crawl()
        first = harness.counts(self.conn)
        self.crawl()
        second = harness.counts(self.conn)
        self.assertEqual(first["fetches"], second["fetches"],
                         "unchanged payloads must not create new work")
        files = list(Path(harness.config.raw_dir()).rglob("*.gz"))
        self.assertEqual(len(files), first["fetches"],
                         "one content-addressed file per distinct payload")
        # both rules read the same rows without duplicating storage
        self.parse_both()
        docs = self.conn.execute(
            "SELECT COUNT(*) FROM parsed_documents").fetchone()[0]
        self.assertEqual(docs, first["fetches"] * 2)
        self.assertEqual(harness.counts(self.conn)["fetches"], first["fetches"])

    # -- single writer --------------------------------------------------
    def test_same_version_never_published_twice(self):
        self.crawl()
        self.parse_both()
        publisher.drain(self.conn)
        # Both rules produced documents for every page.  Only one may publish
        # each version; the ledger key is the version, not the document.
        docs = self.conn.execute(
            "SELECT COUNT(*) FROM parsed_documents").fetchone()[0]
        ledger = self.conn.execute(
            "SELECT COUNT(*) FROM publish_ledger").fetchone()[0]
        self.assertGreater(docs, ledger)
        dupes = self.conn.execute(
            "SELECT version_key, COUNT(*) n FROM publish_ledger "
            "GROUP BY version_key HAVING n > 1").fetchall()
        self.assertEqual(dupes, [])
        inv_dupes = self.conn.execute(
            "SELECT page_id, COUNT(*) n FROM inventory GROUP BY page_id "
            "HAVING n > 1").fetchall()
        self.assertEqual(inv_dupes, [])

    def test_second_publisher_is_refused_not_double_counted(self):
        self.crawl()
        self.parse_both()
        publisher.drain(self.conn)
        before = harness.counts(self.conn)["published"]
        # Force a second drain and also directly re-attempt a known document.
        publisher.drain(self.conn)
        row = self.conn.execute(
            "SELECT fetch_id, doc_id FROM publish_ledger LIMIT 1").fetchone()
        result = state.publish_document(self.conn, row["fetch_id"], row["doc_id"])
        self.assertFalse(result.published)
        self.assertEqual(result.reason, "already_published")
        self.assertEqual(harness.counts(self.conn)["published"], before)

    # -- canary holds output -------------------------------------------
    def test_canary_output_is_held_until_approved(self):
        self.crawl()
        self.parse_both()
        publisher.drain(self.conn)
        baseline_published = harness.counts(self.conn)["published"]

        self.site.add_series("b")
        self.site.set_variant("v2")
        cid = state.start_canary(self.conn, percent=100)
        self.crawl()
        harness.run_parser(self.conn, "parse-new", "new-canary")
        held = publisher.drain(self.conn)
        self.assertEqual(held["published"], 0,
                         "canary epoch must not write inventory before approval")
        self.assertEqual(harness.counts(self.conn)["published"], baseline_published)

        # all new versions got documents from both rules; canary waits
        pending = self.conn.execute(
            """SELECT COUNT(*) FROM publication_assignments a
                 JOIN parsed_documents d ON d.fetch_id=a.fetch_id AND d.rule_id=a.rule_id
                 LEFT JOIN publish_ledger l ON l.version_key=(
                     SELECT version_key FROM page_fetches WHERE id=a.fetch_id)
                WHERE a.epoch_id=? AND l.seq IS NULL""", (cid,)).fetchone()[0]
        self.assertGreater(pending, 0)

    # -- approval boundary + catch-up ----------------------------------
    def test_approval_catches_up_versions_fetched_before_approval(self):
        self.crawl()
        self.parse_both()
        publisher.drain(self.conn)
        published_before = harness.counts(self.conn)["published"]

        self.site.set_variant("v2")
        self.site.add_series("b")
        cid = state.start_canary(self.conn, percent=100)
        self.crawl()                      # new versions fetched during canary
        # Approve before either parser has processed the new versions.  The
        # versions are already fetched and assigned; approval must not strand
        # them, and the publisher must catch up once parsing ends.
        report = diff.compare(self.conn, harness.rule_ids(self.conn)["parse-old"],
                              harness.rule_ids(self.conn)["parse-new"], epoch_id=cid)
        rid = diff.save_report(self.conn, cid, report)
        pid = state.promote_epoch(self.conn, diff_report_id=rid)
        self.assertEqual(publisher.drain(self.conn)["published"], 0,
                         "nothing parsed yet for the new versions")

        # Parsers keep working after approval -- including on versions that were
        # fetched while the canary was live.
        self.parse_both()
        drain = publisher.drain(self.conn)
        self.assertGreater(drain["published"], 0)

        fetches = harness.counts(self.conn)["fetches"]
        ledger = harness.counts(self.conn)["published"]
        self.assertEqual(ledger, fetches,
                         "every fetched page version must publish exactly once")
        self.assertGreater(ledger, published_before)
        # inventory holds the newest version of each page, none published by the
        # rolled-back canary
        newest = self.conn.execute(
            """SELECT f.page_id, MAX(f.id) AS fid FROM page_fetches f
                GROUP BY f.page_id""").fetchall()
        inv = {r["page_id"]: r for r in self.conn.execute(
            "SELECT page_id, fetch_id FROM inventory").fetchall()}
        for row in newest:
            self.assertEqual(inv[row["page_id"]]["fetch_id"], row["fid"])
        canary_published = self.conn.execute(
            "SELECT COUNT(*) FROM publish_ledger WHERE epoch_id=?", (cid,)
        ).fetchone()[0]
        self.assertEqual(canary_published, 0)
        self.assertGreater(
            self.conn.execute("SELECT COUNT(*) FROM publish_ledger WHERE epoch_id=?",
                              (pid,)).fetchone()[0], 0)

    def test_versions_parsed_during_canary_publish_once_on_approval(self):
        self.crawl()
        self.parse_both()
        publisher.drain(self.conn)
        self.site.set_variant("v2")
        cid = state.start_canary(self.conn, percent=100)
        self.crawl()
        # New rule parses the new versions *while the canary is still open*.
        harness.run_parser(self.conn, "parse-new", "new-preapproval")
        self.assertEqual(publisher.drain(self.conn)["published"], 0)
        pid = state.promote_epoch(self.conn)
        drain = publisher.drain(self.conn)
        self.assertGreater(drain["published"], 0)
        # The documents that were waiting are exactly what got published.
        waiting = self.conn.execute(
            """SELECT COUNT(*) FROM publication_assignments a
                 JOIN parsed_documents d ON d.fetch_id=a.fetch_id
                      AND d.rule_id=a.rule_id
                 LEFT JOIN publish_ledger l ON l.version_key=(
                     SELECT version_key FROM page_fetches WHERE id=a.fetch_id)
                WHERE l.seq IS NULL""").fetchone()[0]
        self.assertEqual(waiting, 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM publish_ledger WHERE epoch_id=?",
                (pid,)).fetchone()[0], drain["published"])
        # second drain is a no-op
        self.assertEqual(publisher.drain(self.conn)["published"], 0)

    # -- rollback keeps already-processed new pages -------------------------------------------------
    def test_rollback_delivers_pages_already_parsed_by_new_rule(self):
        self.crawl()
        self.parse_both()
        publisher.drain(self.conn)
        published_before = harness.counts(self.conn)["published"]

        self.site.add_series("b")
        self.site.set_variant("v2")
        cid = state.start_canary(self.conn, percent=100)
        self.crawl()
        # Both rules process every new page; canary work is held.
        harness.run_parser(self.conn, "parse-old", "old-during")
        harness.run_parser(self.conn, "parse-new", "new-during")
        publisher.drain(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM publish_ledger WHERE epoch_id=?", (cid,)
        ).fetchone()[0], 0)

        rec = state.rollback_epoch(self.conn)
        drain = publisher.drain(self.conn)
        self.assertGreater(drain["published"], 0)

        # every page fetched before the rollback boundary is now published,
        # through parse-old -- including the pages the new rule already handled
        boundary = self.conn.execute(
            "SELECT max_fetch_id_at_close FROM rule_epochs WHERE id=?", (cid,)
        ).fetchone()[0]
        unpublished = self.conn.execute(
            """SELECT f.id FROM page_fetches f
                 LEFT JOIN publish_ledger l ON l.version_key=f.version_key
                WHERE f.kind='item' AND f.id<=? AND l.seq IS NULL""",
            (boundary,)).fetchall()
        # Pages outside the canary's baseline that were only ever seen while the
        # canary was live must not be stranded.
        self.assertEqual(
            [r["id"] for r in unpublished], [],
            f"versions left unpublished after rollback: {unpublished}")
        rules_used = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT rule_id FROM publish_ledger WHERE epoch_id=?", (rec,))}
        ids = harness.rule_ids(self.conn)
        self.assertEqual(rules_used, {ids["parse-old"]})
        self.assertGreaterEqual(harness.counts(self.conn)["published"], published_before)
        # no version was published twice
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM (SELECT version_key FROM publish_ledger "
                "GROUP BY version_key HAVING COUNT(*)>1)").fetchone()[0], 0)

    # -- crash and resume ----------------------------------------------
    def test_parser_crash_resumes_without_losing_or_repeating(self):
        self.crawl()
        first = harness.run_parser(self.conn, "parse-new", "new-crash")
        # simulate a hard crash mid-way: a second worker starts, the first one's
        # partial state is what persists in the database
        progress_before = worker.progress(self.conn, first.rule_id)
        docs_before = self.conn.execute(
            "SELECT COUNT(*) FROM parsed_documents WHERE rule_id=?",
            (first.rule_id,)).fetchone()[0]
        second = harness.run_parser(self.conn, "parse-new", "new-crash")
        docs_after = self.conn.execute(
            "SELECT COUNT(*) FROM parsed_documents WHERE rule_id=?",
            (first.rule_id,)).fetchone()[0]
        self.assertGreaterEqual(docs_after, docs_before)
        self.assertEqual(worker.progress(self.conn, first.rule_id)["remaining"], 0)
        # no duplicate documents
        dupes = self.conn.execute(
            "SELECT fetch_id, rule_id, COUNT(*) n FROM parsed_documents "
            "GROUP BY fetch_id, rule_id HAVING n>1").fetchall()
        self.assertEqual(dupes, [])

    def test_progress_survives_new_fetches(self):
        self.crawl()
        harness.run_parser(self.conn, "parse-new", "new-1")
        rule_id = harness.rule_ids(self.conn)["parse-new"]
        before = worker.progress(self.conn, rule_id)
        self.site.add_series("b")
        self.site.set_variant("v2")
        self.crawl()
        after = worker.progress(self.conn, rule_id)
        self.assertGreater(after["total"], before["total"])
        self.assertGreater(after["remaining"], 0)
        harness.run_parser(self.conn, "parse-new", "new-1")
        self.assertEqual(worker.progress(self.conn, rule_id)["remaining"], 0)


class ResetAndEdgeTest(unittest.TestCase):
    def setUp(self):
        self.conn = harness.fresh(self.id().split(".")[-1] + "-edge")
        self.srv, self.site = harness.make_site("v1")
        state.ensure_baseline(self.conn)

    def tearDown(self):
        self.srv.stop()

    def test_rollback_without_canary_is_a_clean_error(self):
        with self.assertRaises(state.StateError):
            state.rollback_epoch(self.conn)
        with self.assertRaises(state.StateError):
            state.promote_epoch(self.conn)

    def test_double_canary_is_refused(self):
        crawler.crawl_once(self.conn, self.srv.url)
        state.start_canary(self.conn, percent=50)
        with self.assertRaises(state.StateError):
            state.start_canary(self.conn, percent=50)

    def test_second_canary_must_wait_for_the_first(self):
        crawler.crawl_once(self.conn, self.srv.url)
        state.start_canary(self.conn, percent=100)
        with self.assertRaises(state.StateError):
            state.start_canary(self.conn, percent=100)
        # A rollback closes the canary so a new one can start; the recovery
        # epoch carries the unpublished work.
        rec = state.rollback_epoch(self.conn)
        new = state.start_canary(self.conn, percent=100)
        self.assertGreater(new, rec)
        self.assertIsNone(state.canary_epoch(self.conn)["approved_at"])

    def test_parser_error_is_isolated_and_retried_with_bound(self):
        crawler.crawl_once(self.conn, self.srv.url)
        # corrupt one raw page so the rule raises on it
        row = self.conn.execute(
            "SELECT * FROM page_fetches WHERE kind='item' LIMIT 1").fetchone()
        self.conn.execute("UPDATE page_fetches SET content_sha='deadbeef' WHERE id=?",
                          (row["id"],))
        w = harness.run_parser(self.conn, "parse-old", "old-err")
        self.assertGreaterEqual(w.errors, 1)
        # other pages still parsed
        self.assertGreater(
            self.conn.execute("SELECT COUNT(*) FROM parsed_documents").fetchone()[0], 0)
        # the bad page has bounded attempts and does not grow unboundedly
        attempts = self.conn.execute(
            "SELECT COUNT(*) FROM parse_attempts WHERE fetch_id=? AND status='error'",
            (row["id"],)).fetchone()[0]
        self.assertLessEqual(attempts, w.max_attempts)
        # and it does not appear repeatedly in a pending batch
        self.assertEqual(
            len([f for f in w.pending(50) if f["id"] == row["id"]]), 0)

    def test_inventory_reflects_reverted_content(self):
        crawler.crawl_once(self.conn, self.srv.url)
        harness.run_parser(self.conn, "parse-old")
        publisher.drain(self.conn)
        original = self.conn.execute(
            "SELECT data_json FROM inventory WHERE page_id='a1'").fetchone()[0]
        self.site.set_variant("v2")
        self.site.set_price("a1", "999.00")
        crawler.crawl_once(self.conn, self.srv.url)
        harness.run_parser(self.conn, "parse-old", "old-rev")
        publisher.drain(self.conn)
        mid = self.conn.execute(
            "SELECT data_json FROM inventory WHERE page_id='a1'").fetchone()[0]
        self.assertNotEqual(mid, original)
        self.assertEqual(__import__("json").loads(mid)["fields"]["price"], "999.00")
        # revert the site fully
        self.site.set_variant("v1")
        self.site.set_price("a1", "459.00")
        crawler.crawl_once(self.conn, self.srv.url)
        harness.run_parser(self.conn, "parse-old", "old-rev2")
        publisher.drain(self.conn)
        back = self.conn.execute(
            "SELECT data_json FROM inventory WHERE page_id='a1'").fetchone()[0]
        self.assertEqual(__import__("json").loads(back)["fields"]["price"], "459.00")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM (SELECT version_key FROM publish_ledger "
                "GROUP BY version_key HAVING COUNT(*)>1)").fetchone()[0], 0)


class BoundaryRaceTest(unittest.TestCase):
    """A boundary change mid-crawl must not strand an unpublished version."""

    def setUp(self):
        self.conn = harness.fresh(self.id().split(".")[-1] + "-race")
        self.srv, self.site = harness.make_site("v1")
        state.ensure_baseline(self.conn)
        crawler.crawl_once(self.conn, self.srv.url)
        harness.run_parser(self.conn, "parse-old")
        harness.run_parser(self.conn, "parse-new")
        publisher.drain(self.conn)

    def tearDown(self):
        self.srv.stop()

    def crawl(self):
        return crawler.crawl_once(self.conn, self.srv.url)

    def test_stale_epoch_assignment_is_reconciled(self):
        # Simulate a page whose write landed just after the promotion committed:
        # it is fetched under the finalized canary, but the canary is no longer
        # allowed to publish.
        self.site.set_variant("v2")
        self.site.add_series("b")
        cid = state.start_canary(self.conn, percent=100)
        self.crawl()
        harness.run_parser(self.conn, "parse-old", "old-race")
        harness.run_parser(self.conn, "parse-new", "new-race")
        stale = self.conn.execute(
            "SELECT id FROM page_fetches WHERE epoch_id=? LIMIT 1", (cid,)).fetchone()[0]
        pid = state.promote_epoch(self.conn)
        # force one assignment back to the now-finalized canary
        self.conn.execute(
            "UPDATE publication_assignments SET epoch_id=? WHERE fetch_id=?",
            (cid, stale))
        drain = publisher.drain(self.conn)
        self.assertGreaterEqual(drain["reassigned"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT epoch_id FROM publication_assignments WHERE fetch_id=?",
                (stale,)).fetchone()[0], pid)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT seq FROM publish_ledger l JOIN page_fetches f "
                "ON f.version_key=l.version_key WHERE f.id=?", (stale,)).fetchone())

    def test_out_of_scope_baseline_is_not_rehomed_during_canary(self):
        self.site.set_variant("v2")
        self.site.add_series("b")
        state.start_canary(self.conn, percent=10)
        self.crawl()
        out_of_scope = self.conn.execute(
            """SELECT a.fetch_id, a.epoch_id FROM publication_assignments a
                 JOIN rule_epochs e ON e.id=a.epoch_id
                WHERE e.is_canary=0 AND e.status='closed' LIMIT 1""").fetchone()
        self.assertIsNotNone(out_of_scope,
                             "10% sample should leave most versions on the baseline")
        moved = state.reconcile_assignments(self.conn)
        self.assertEqual(moved, 0)
        still = self.conn.execute(
            "SELECT epoch_id FROM publication_assignments WHERE fetch_id=?",
            (out_of_scope["fetch_id"],)).fetchone()[0]
        self.assertEqual(still, out_of_scope["epoch_id"])


class DiffGateTest(unittest.TestCase):
    """The comparison must catch field loss and split rows, not just row counts."""

    def setUp(self):
        self.conn = harness.fresh(self.id().split(".")[-1] + "-diff")
        self.srv, self.site = harness.make_site("v1")
        state.ensure_baseline(self.conn)
        crawler.crawl_once(self.conn, self.srv.url)

    def tearDown(self):
        self.srv.stop()

    def _draft_rule(self):
        import hashlib
        from canary import rule_sources
        src = rule_sources.DRAFT_RULE
        sha = hashlib.sha256(src.encode()).hexdigest()
        cur = self.conn.execute(
            "INSERT INTO rule_versions(label, source, source_sha, created_at, notes) "
            "VALUES('parse-draft',?,?,?,?)", (src, sha, state.now(), "test"))
        return cur.lastrowid

    def test_identical_rules_pass(self):
        harness.run_parser(self.conn, "parse-old")
        harness.run_parser(self.conn, "parse-new")
        ids = harness.rule_ids(self.conn)
        rep = diff.compare(self.conn, ids["parse-old"], ids["parse-new"], sample=100)
        self.assertEqual(rep["summary"]["status"], "pass")
        self.assertEqual(rep["summary"]["field_loss"], [])

    def test_field_loss_fails_even_when_rows_match(self):
        # The draft's list bug only shows on redesigned markings; for the field
        # loss check we want item documents, so stay on v1.
        draft = self._draft_rule()
        harness.run_parser(self.conn, "parse-old")
        harness.run_parser(self.conn, "parse-draft")
        ids = harness.rule_ids(self.conn)
        rep = diff.compare(self.conn, ids["parse-old"], draft, sample=100)
        self.assertEqual(rep["summary"]["status"], "fail")
        lost = {x["field"] for x in rep["summary"]["field_loss"]}
        self.assertIn("stock", lost)
        self.assertIn("description", rep["summary"]["truncated_fields"],
                      "a shortened description is truncation, not a benign "
                      "value difference")

    def test_split_rows_detected_on_redesigned_listing(self):
        self.site.set_variant("v2")
        self.site.add_series("b")
        # a fresh crawl captures the redesigned listing
        crawler.crawl_once(self.conn, self.srv.url)
        ids = harness.rule_ids(self.conn)
        harness.run_parser(self.conn, "parse-old", "old-l")
        harness.run_parser(self.conn, "parse-new", "new-l")
        rep = diff.compare(self.conn, ids["parse-old"], ids["parse-new"], sample=100)
        self.assertIn("split_parts", rep["summary"]["row_issue_kinds"],
                      "old rule must see two half-rows for redesigned groups")
        self.assertGreater(rep["summary"]["split_rows_seen"], 0)
        # The new rule fixes the split (it groups the sibling parts), so this is
        # a structural change to review, not a regression by the new rule.
        self.assertEqual(rep["summary"]["row_regressions"], 0)
        self.assertEqual(rep["summary"]["status"], "review")
        # Compare in the other direction: the splitter is then the candidate and
        # must fail the gate.
        reversed_rep = diff.compare(self.conn, ids["parse-new"], ids["parse-old"],
                                    sample=100)
        self.assertEqual(reversed_rep["summary"]["status"], "fail")
        self.assertGreater(reversed_rep["summary"]["row_regressions"], 0)
        # The old rule sees twice as many rows, each missing half its fields;
        # a total-row-count check alone would not describe where the loss is.
        old_rows = sum(len(p["old_rows"]) for p in rep["pages"])
        new_rows = sum(len(p["new_rows"]) for p in rep["pages"])
        self.assertGreater(old_rows, new_rows)
        half_rows = [r for p in rep["pages"] for r in p["old_rows"]
                     if r["fields"].get("title") and not r["fields"].get("price")]
        self.assertGreater(len(half_rows), 0,
                           "split halves must show up as rows with one side empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
