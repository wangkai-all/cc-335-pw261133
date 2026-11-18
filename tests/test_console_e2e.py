"""End-to-end tests against the real HTTP console and real subprocesses.

These start `canary.web` in-process (its Handler plus the Supervisor that spawns
the practice site, crawler, two parser workers and publisher as separate OS
processes), then drive it exactly as the browser does.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="canary-e2e-")
os.environ["CANARY_VAR"] = _TMP
os.environ.pop("CANARY_DB", None)
os.environ.pop("CANARY_RAW_DIR", None)
os.environ.setdefault("CANARY_CRAWL_INTERVAL", "1")
os.environ.setdefault("CANARY_PARSER_POLL", "0.15")
os.environ.setdefault("CANARY_SITE_PORT", "0")

from canary import db, state, web  # noqa: E402
from canary.supervisor import SUPERVISOR  # noqa: E402


def http(method: str, url: str, payload=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class ConsoleE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other suites change CANARY_VAR while they run; this class owns its own
        # data directory for the whole class, including spawned subprocesses.
        os.environ["CANARY_VAR"] = _TMP
        os.environ.pop("CANARY_DB", None)
        os.environ.pop("CANARY_RAW_DIR", None)
        cls.httpd = web.run(0, start_services=False)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        status, _ = http("POST", cls.base + "/api/service/start", {})
        assert status == 200, status
        # wait for the initial crawl + both parsers to drain
        deadline = time.time() + 40
        while time.time() < deadline:
            _, ov = http("GET", cls.base + "/api/overview")
            if (ov["counts"]["published_total"] >= 8
                    and all(p["remaining"] == 0 for p in ov["progress"])):
                break
            time.sleep(0.4)
        cls.initial = ov

    @classmethod
    def tearDownClass(cls):
        try:
            http("POST", cls.base + "/api/service/stop", {})
        except Exception:
            pass
        SUPERVISOR.shutdown()
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass

    def test_01_site_is_served_over_http(self):
        _, status = http("GET", self.base + "/api/status")
        self.assertTrue(status["supervisor"]["site"]["running"])
        site_url = status["supervisor"]["site"]["url"]
        with urllib.request.urlopen(site_url + "/", timeout=10) as resp:
            html = resp.read().decode()
        self.assertIn("示例家居资料站", html)

    def test_02_initial_inventory_published_once_per_version(self):
        _, ov = http("GET", self.base + "/api/overview")
        self.assertGreaterEqual(ov["counts"]["published_total"], 8)
        page_ids = [row["page_id"] for row in ov["inventory"]]
        self.assertEqual(len(page_ids), len(set(page_ids)))
        self.assertTrue(all(row["rule"] == "parse-old" for row in ov["inventory"]))

    def test_03_canary_scope_holds_output_and_reports_fields(self):
        # Mutate the live site the way an operator would.
        http("POST", self.base + "/api/site/variant", {"variant": "v2"})
        http("POST", self.base + "/api/site/series", {"letter": "b"})
        status, started = http("POST", self.base + "/api/canary/start", {"percent": 50})
        self.assertEqual(status, 200, started)
        epoch_id = started["epoch_id"]

        deadline = time.time() + 45
        report = None
        while time.time() < deadline:
            _, report = http("GET", f"{self.base}/api/report?epoch_id={epoch_id}&sample=24")
            paired = report["summary"].get("total_pairs", 0)
            assigned = report["coverage"].get("canary_assigned_versions", 0)
            if paired >= 15 and assigned >= 4 and not report["summary"]["missing_in_new"]:
                break
            time.sleep(0.5)
        self.assertIsNotNone(report)
        self.assertGreater(report["summary"]["total_pairs"], 0)
        # Per-field presence is reported, not just totals.
        by_field = {f["field"]: f for f in report["fields"]}
        self.assertIn("title", by_field)
        self.assertIn("row.title", by_field)
        self.assertGreater(by_field["title"]["old_present"], 0)
        self.assertEqual(by_field["title"]["agreement"], 1.0)
        # The redesign is visible structurally: the old rule splits rows.
        kinds = report["summary"]["row_issue_kinds"]
        self.assertIn("split_parts", kinds)
        self.assertEqual(report["summary"]["row_regressions"], 0)
        # Canary output is held.
        deadline = time.time() + 20
        held = 0
        while time.time() < deadline:
            _, ov = http("GET", self.base + "/api/overview")
            held = ov["counts"]["held_by_canary"]
            if held >= 1:
                break
            time.sleep(0.4)
        self.assertGreaterEqual(held, 1)
        canary_pub = [e for e in ov["epochs"] if e["id"] == epoch_id][0]["published_versions"]
        self.assertEqual(canary_pub, 0)

    def test_04_parser_crash_is_real_and_resumes(self):
        _, ov = http("GET", self.base + "/api/overview")
        parser = next(p for p in ov["processes"]["processes"] if p["name"] == "parse-new")
        pid = parser["pid"]
        status, killed = http("POST", self.base + "/api/parser/crash", {"name": "parse-new"})
        self.assertEqual(status, 200)
        self.assertTrue(killed["killed"])

        deadline = time.time() + 15
        dead = False
        while time.time() < deadline:
            _, status_now = http("GET", self.base + "/api/status")
            p = next((x for x in status_now["supervisor"]["processes"]
                      if x["name"] == "parse-new"), None)
            if p and not p["alive"]:
                dead = True
                break
            time.sleep(0.3)
        self.assertTrue(dead, "SIGKILLed parser process should be gone")
        deadline = time.time() + 10
        inst = None
        while time.time() < deadline:
            _, ov2 = http("GET", self.base + "/api/overview")
            inst = next((i for i in ov2["instances"] if i["id"] == "parse-new"), None)
            if inst and inst["status"] in ("crashed", "stopped"):
                break
            time.sleep(0.3)
        self.assertIsNotNone(inst)
        self.assertIn(inst["status"], ("crashed", "stopped"))

        # New pages arrive while the parser is down.
        http("POST", self.base + "/api/site/series", {"letter": "c"})
        http("POST", self.base + "/api/site/price", {"page_id": "a3", "price": "275.00"})
        time.sleep(2.5)

        status, _ = http("POST", self.base + "/api/parser/restart",
                         {"name": "parse-new", "rule": "parse-new"})
        self.assertEqual(status, 200)
        deadline = time.time() + 30
        while time.time() < deadline:
            _, ov3 = http("GET", self.base + "/api/overview")
            prog = next(p for p in ov3["progress"] if p["rule"] == "parse-new")
            if prog["remaining"] == 0 and prog["total"] > ov["counts"]["versions"]:
                break
            time.sleep(0.5)
        _, ov3 = http("GET", self.base + "/api/overview")
        prog = next(p for p in ov3["progress"] if p["rule"] == "parse-new")
        self.assertEqual(prog["remaining"], 0)
        # Its own progress count never exceeds the shared version count (no
        # duplicate parse accounting).
        self.assertLessEqual(prog["processed"], prog["total"])

    def test_05_rollback_publishes_processed_new_pages_and_no_duplicates(self):
        _, ov = http("GET", self.base + "/api/overview")
        canary = ov["canary"]
        self.assertIsNotNone(canary, "test_03 should have left a canary open")
        old_rule = "parse-old"
        before_inv = {r["page_id"]: r["fetch_id"] for r in ov["inventory"]}

        status, rolled = http("POST", self.base + "/api/canary/rollback", {})
        self.assertEqual(status, 200, rolled)
        recovery_id = rolled["epoch_id"]

        deadline = time.time() + 40
        while time.time() < deadline:
            _, ov2 = http("GET", self.base + "/api/overview")
            ready = ov2["counts"]["ready_unpublished"]
            if ready == 0:
                break
            time.sleep(0.5)

        _, ov2 = http("GET", self.base + "/api/overview")
        # Every item/list version observed before the rollback boundary is
        # published exactly once, through the old rule.
        import sqlite3
        conn = sqlite3.connect(str(Path(os.environ["CANARY_VAR"]) / "canary.db"))
        conn.row_factory = sqlite3.Row
        boundary = conn.execute(
            "SELECT max_fetch_id_at_close FROM rule_epochs WHERE id=?",
            (canary["id"],)).fetchone()[0]
        stranded = conn.execute(
            """SELECT COUNT(*) FROM page_fetches f
                LEFT JOIN publish_ledger l ON l.version_key=f.version_key
               WHERE f.id<=? AND f.kind IN ('item','list') AND l.seq IS NULL""",
            (boundary,)).fetchone()[0]
        self.assertEqual(stranded, 0, "rollback must not leave processed pages unpublished")
        dupes = conn.execute(
            "SELECT COUNT(*) FROM (SELECT version_key FROM publish_ledger "
            "GROUP BY version_key HAVING COUNT(*)>1)").fetchone()[0]
        self.assertEqual(dupes, 0)
        recovery_pub = conn.execute(
            "SELECT COUNT(*) FROM publish_ledger WHERE epoch_id=?",
            (recovery_id,)).fetchone()[0]
        self.assertGreater(recovery_pub, 0)
        rules = {r[0] for r in conn.execute(
            "SELECT DISTINCT rule_id FROM publish_ledger WHERE epoch_id=?",
            (recovery_id,))}
        old_id = conn.execute(
            "SELECT id FROM rule_versions WHERE label=?", (old_rule,)).fetchone()[0]
        self.assertEqual(rules, {old_id})
        # The pages that were already published before rollback were not
        # re-published (same fetch id per page, or a newer one).
        inv = {r["page_id"]: r["fetch_id"] for r in conn.execute(
            "SELECT page_id, fetch_id FROM inventory")}
        for page_id, fid in before_inv.items():
            self.assertGreaterEqual(inv[page_id], fid)
        conn.close()

    def test_06_bad_candidate_rule_is_blocked_by_field_diff(self):
        status, draft = http("POST", self.base + "/api/rules/start-draft", {})
        self.assertEqual(status, 200)
        # Wait for the draft parser to catch up on existing raw pages, then open
        # a canary explicitly on that rule.
        deadline = time.time() + 30
        while time.time() < deadline:
            _, ov = http("GET", self.base + "/api/overview")
            prog = next((p for p in ov["progress"] if p["rule"] == "parse-draft"), None)
            if prog and prog["remaining"] == 0:
                break
            time.sleep(0.4)
        status, started = http("POST", self.base + "/api/canary/start",
                               {"percent": 60, "rule_id": draft["rule_id"]})
        self.assertEqual(status, 200, started)
        epoch_id = started["epoch_id"]
        deadline = time.time() + 45
        report = None
        while time.time() < deadline:
            _, report = http("GET", f"{self.base}/api/report?epoch_id={epoch_id}&sample=24")
            if (report["summary"].get("total_pairs", 0) >= 10
                    and not report["summary"]["missing_in_new"]):
                break
            time.sleep(0.5)
        self.assertEqual(report["summary"]["status"], "fail")
        lost = {f["field"] for f in report["summary"]["field_loss"]}
        self.assertIn("stock", lost)
        status, blocked = http("POST", self.base + "/api/canary/approve", {})
        self.assertEqual(status, 409)
        self.assertTrue(blocked["blocked"])
        # force override still works and does not double-publish
        status, forced = http("POST", self.base + "/api/canary/approve",
                              {"force": True, "note": "test override"})
        self.assertEqual(status, 200, forced)
        import sqlite3
        conn = sqlite3.connect(str(Path(os.environ["CANARY_VAR"]) / "canary.db"))
        dupes = conn.execute(
            "SELECT COUNT(*) FROM (SELECT version_key FROM publish_ledger "
            "GROUP BY version_key HAVING COUNT(*)>1)").fetchone()[0]
        self.assertEqual(dupes, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
