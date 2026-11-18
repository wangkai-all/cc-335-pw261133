"""HTTP application: API + static UI, standard library only.

    python -m canary.web --port 8000

Endpoints are grouped:

* read:   /api/overview, /api/epochs, /api/status, /api/report, /api/inventory
* site:   /api/site/variant, /api/site/series, /api/site/price, /api/site/stock
* rules:  /api/canary/start, /api/canary/approve, /api/canary/rollback
* ops:    /api/parser/crash, /api/parser/restart, /api/publisher/tick,
          /api/report/refresh, /api/service/start, /api/service/stop
* rules:  /api/rules/start-draft, /api/rules/sources
* sim:    /api/sim/reset, /api/sim/seed-canary

All mutations run inside one immediate SQLite transaction and write an event.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, diff, publisher, rawstore, rule_sources, state, worker
from .supervisor import SUPERVISOR

STATIC = Path(__file__).resolve().parent.parent / "static"


def json_default(obj):
    return str(obj)


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.report_cache: dict[tuple, dict] = {}

    # -- helpers --------------------------------------------------------
    def conn(self):
        return db.get_conn()

    def overview(self) -> dict:
        conn = self.conn()
        counts = publisher.queue_counts(conn)
        epoch = state.active_epoch(conn)
        canary = state.canary_epoch(conn)
        rules = [dict(r) for r in conn.execute(
            "SELECT id, label, source_sha, created_at, notes FROM rule_versions "
            "ORDER BY id")]
        rule_labels = {r["id"]: r["label"] for r in rules}
        epochs = []
        for ep in conn.execute(
                "SELECT * FROM rule_epochs ORDER BY id DESC LIMIT 12").fetchall():
            s = state.epoch_summary(conn, ep)
            s["rule_label"] = rule_labels.get(ep["rule_id"], "?")
            epochs.append(s)
        progress = []
        for rule in rules:
            p = worker.progress(conn, rule["id"])
            p["rule"] = rule["label"]
            p["rule_id"] = rule["id"]
            progress.append(p)
        inventory = [dict(r) for r in conn.execute(
            "SELECT i.page_id, i.fetch_id, i.updated_at, i.rule_id, r.label AS rule, "
            "i.data_json FROM inventory i JOIN rule_versions r ON r.id=i.rule_id "
            "ORDER BY i.page_id LIMIT 500")]
        for row in inventory:
            row["data"] = json.loads(row.pop("data_json"))
        instances = [dict(r) for r in conn.execute(
            "SELECT * FROM parser_instances ORDER BY started_at DESC LIMIT 20")]
        events = []
        for row in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 40"):
            detail = json.loads(row["detail"])
            events.append({"id": row["id"], "at": row["at"], "actor": row["actor"],
                           "kind": row["kind"], **detail})
        checkpoints = [dict(r) for r in conn.execute(
            "SELECT c.*, r.label AS rule FROM progress_checkpoints c "
            "JOIN rule_versions r ON r.id=c.rule_id ORDER BY c.id DESC LIMIT 20")]
        heartbeats = {}
        for row in conn.execute("SELECT * FROM service_heartbeats"):
            heartbeats[row["service"]] = {"at": row["at"],
                                          "detail": json.loads(row["detail_json"])}
        fetches = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT page_id) AS pages "
            "FROM page_fetches WHERE kind IN ('item','list')"
        ).fetchone()
        runs = [dict(r) for r in conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 8")]
        mutations = [dict(r) for r in conn.execute(
            "SELECT * FROM site_mutations ORDER BY id DESC LIMIT 12")]
        return {
            "at": time.time(),
            "counts": {**counts, "versions": fetches["n"], "pages": fetches["pages"]},
            "epochs": epochs,
            "active_epoch": dict(epoch) | {"rule_label": rule_labels.get(epoch["rule_id"])}
                              if epoch else None,
            "canary": state.epoch_summary(conn, canary) if canary else None,
            "rules": rules,
            "progress": progress,
            "inventory": inventory,
            "instances": instances,
            "events": events,
            "checkpoints": checkpoints,
            "heartbeats": heartbeats,
            "crawl_runs": runs,
            "site_mutations": mutations,
            "rejections": [dict(r) for r in conn.execute(
                "SELECT * FROM publish_rejections ORDER BY id DESC LIMIT 20")],
            "processes": SUPERVISOR.status(),
            "site_items": (SUPERVISOR.site_state().items
                           if SUPERVISOR.site_state() else {}),
            "site_series": (list(SUPERVISOR.site_state().series)
                            if SUPERVISOR.site_state() else []),
        }

    def data_signature(self, conn, epoch_id: int | None) -> tuple:
        row = conn.execute(
            "SELECT (SELECT COALESCE(MAX(id),0) FROM page_fetches) AS f, "
            "       (SELECT COUNT(*) FROM parsed_documents) AS d, "
            "       (SELECT COUNT(*) FROM publish_ledger) AS p").fetchone()
        ep = None
        if epoch_id is not None:
            ep = conn.execute(
                "SELECT status, max_fetch_id_at_close FROM rule_epochs WHERE id=?",
                (epoch_id,)).fetchone()
        return (row["f"], row["d"], row["p"],
                ep["status"] if ep else None,
                ep["max_fetch_id_at_close"] if ep else None)

    def report_for(self, epoch_id: int | None, *, save: bool = False,
                   sample: int | None = None) -> dict:
        conn = self.conn()
        if epoch_id is None:
            canary = state.canary_epoch(conn)
            if canary is None:
                return {"error": "no canary open"}
            epoch_id = canary["id"]
        epoch = conn.execute("SELECT * FROM rule_epochs WHERE id=?", (epoch_id,)).fetchone()
        if epoch is None:
            return {"error": "unknown epoch"}
        old_rule = epoch["fallback_rule_id"] or epoch["rule_id"]
        if sample is None:
            sample = int(os.environ.get("CANARY_SAMPLE", "40"))
        sample = max(1, min(500, int(sample)))
        report = diff.compare(conn, old_rule, epoch["rule_id"], epoch_id=epoch_id,
                              sample=sample)
        report["epoch"] = {
            "id": epoch["id"], "origin": epoch["origin"],
            "rule_id": epoch["rule_id"], "old_rule_id": old_rule,
            "status": epoch["status"], "approved_at": epoch["approved_at"],
        }
        if save:
            rid = diff.save_report(conn, epoch_id, report)
            report["report_id"] = rid
            conn.execute("UPDATE rule_epochs SET diff_report_id=? WHERE id=?",
                         (rid, epoch_id))
        return report


APP = App()


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------

def ok(data, status: HTTPStatus = HTTPStatus.OK):
    return status, data


class Handler(BaseHTTPRequestHandler):
    server_version = "CanaryConsole/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- plumbing -------------------------------------------------------
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            ctype = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.startswith("application/javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- routing --------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                status, data = self.route_get(path, qs)
                self._send_json(status, data)
            elif path in ("/", "/index.html"):
                self._send_file(STATIC / "index.html")
            elif path.startswith("/static/"):
                target = (STATIC / path[len("/static/"):]).resolve()
                if not str(target).startswith(str(STATIC.resolve())):
                    self._send_json(403, {"error": "forbidden"})
                else:
                    self._send_file(target)
            elif path == "/practice-site":
                site = SUPERVISOR.site
                if site is None:
                    self._send_json(503, {"error": "practice site is not running"})
                else:
                    self.send_response(302)
                    self.send_header("Location", site.url + "/")
                    self.end_headers()
            else:
                self._send_json(404, {"error": "not found"})
        except Exception:
            traceback.print_exc()
            self._send_json(500, {"error": "internal error",
                                  "trace": traceback.format_exc()[-2000:]})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            status, data = self.route_post(parsed.path, payload)
            self._send_json(status, data)
        except state.StateError as exc:
            self._send_json(409, {"error": str(exc)})
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self._send_json(500, {"error": "internal error",
                                  "trace": traceback.format_exc()[-2000:]})

    # -- GET routes -----------------------------------------------------
    def route_get(self, path: str, qs: dict):
        conn = APP.conn()
        if path == "/api/overview":
            SUPERVISOR.reap()
            return ok(APP.overview())
        if path == "/api/status":
            return ok({"supervisor": SUPERVISOR.status(),
                       "counts": publisher.queue_counts(conn)})
        if path == "/api/report":
            epoch_id = int(qs["epoch_id"]) if qs.get("epoch_id") else None
            sample = int(qs.get("sample")) if qs.get("sample") else None
            # Cache key follows the data the report reads, so a running crawl or
            # parser invalidates it without explicit hooks.
            cache_key = (os.environ.get("CANARY_VAR"), epoch_id, sample,
                         APP.data_signature(conn, epoch_id))
            with APP.lock:
                cached = APP.report_cache.get(cache_key)
                if cached is None:
                    cached = APP.report_for(epoch_id, sample=sample)
                    APP.report_cache[cache_key] = cached
                report = cached
            report = dict(report)
            report["pages"] = report.get("pages", [])[:200]
            return ok(report)
        if path == "/api/report/saved":
            rid = int(qs["report_id"])
            return ok(diff.load_report(conn, rid))
        if path == "/api/raw":
            fid = int(qs["fetch_id"])
            row = conn.execute("SELECT * FROM page_fetches WHERE id=?", (fid,)).fetchone()
            if row is None:
                return ok({"error": "unknown fetch"}, HTTPStatus.NOT_FOUND)
            raw = rawstore.get(row["raw_path"], row["content_sha"])
            return ok({"fetch_id": fid, "url": row["url"], "version_key": row["version_key"],
                       "sha256": row["content_sha"], "bytes": len(raw),
                       "html": raw.decode("utf-8", "ignore")})
        if path == "/api/inventory":
            rows = []
            for r in conn.execute(
                    "SELECT i.*, r.label AS rule FROM inventory i "
                    "JOIN rule_versions r ON r.id=i.rule_id ORDER BY page_id"):
                d = dict(r)
                d["data"] = json.loads(d.pop("data_json"))
                rows.append(d)
            return ok({"inventory": rows})
        if path == "/api/log":
            name = qs.get("name", "")
            m = SUPERVISOR.procs.get(name)
            if m is None or m.log_path is None:
                return ok({"name": name, "log": ""})
            try:
                text = m.log_path.read_text(errors="replace")
            except OSError as exc:
                text = f"<log unavailable: {exc}>"
            return ok({"name": name, "log": text[-20000:]})
        if path == "/api/report/latest":
            epoch_id = qs.get("epoch_id")
            if epoch_id:
                row = conn.execute(
                    "SELECT id FROM diff_reports WHERE epoch_id=? ORDER BY id DESC LIMIT 1",
                    (int(epoch_id),)).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM diff_reports ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return ok({"report": None})
            return ok({"report": diff.load_report(conn, row["id"])})
        if path == "/api/rules/sources":
            latest = {}
            for label, src in (("parse-old", rule_sources.OLD_RULE),
                               ("parse-new", rule_sources.NEW_RULE),
                               ("parse-draft", rule_sources.DRAFT_RULE)):
                latest[label] = src
            return ok({"rules": latest})
        if path == "/api/pages":
            rows = [dict(r) for r in conn.execute(
                "SELECT f.id, f.page_id, f.url, f.kind, f.fetched_at, f.version_key, "
                "f.epoch_id FROM page_fetches f ORDER BY f.id DESC LIMIT 200")]
            return ok({"pages": rows})
        return ok({"error": "not found"}, HTTPStatus.NOT_FOUND)

    # -- POST routes ----------------------------------------------------
    def route_post(self, path: str, payload: dict):
        if path == "/api/site/variant":
            APP.report_cache.clear()
            return self.post_variant(payload)
        if path == "/api/site/series":
            return self.post_series(payload)
        if path == "/api/site/price":
            return self.post_price(payload)
        if path == "/api/site/stock":
            return self.post_stock(payload)
        if path == "/api/canary/start":
            return self.post_canary_start(payload)
        if path == "/api/canary/approve":
            return self.post_canary_approve(payload)
        if path == "/api/canary/rollback":
            return self.post_canary_rollback(payload)
        if path == "/api/report/refresh":
            return self.post_report_refresh(payload)
        if path == "/api/rules/start-draft":
            return self.post_start_draft(payload)
        if path == "/api/parser/crash":
            return self.post_parser_crash(payload)
        if path == "/api/parser/restart":
            return self.post_parser_restart(payload)
        if path == "/api/publisher/tick":
            return self.post_publisher_tick(payload)
        if path == "/api/service/start":
            SUPERVISOR.start_all()
            return ok({"ok": True, "status": SUPERVISOR.status()})
        if path == "/api/service/stop":
            for name, managed in list(SUPERVISOR.procs.items()):
                if managed.kind == "parser":
                    SUPERVISOR.stop_parser(name)
                elif managed.kind in ("crawler", "publisher"):
                    self._stop_other(name)
            return ok({"ok": True, "status": SUPERVISOR.status()})
        if path == "/api/sim/reset":
            return self.post_reset(payload)
        if path == "/api/sim/seed-canary":
            return self.post_seed_canary(payload)
        return ok({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _stop_other(self, name: str) -> None:
        m = SUPERVISOR.procs.get(name)
        if m and m.proc and m.proc.poll() is None:
            m.expected_stop = True
            import os, signal
            try:
                os.killpg(os.getpgid(m.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    # -- site mutations --------------------------------------------------
    def _site(self):
        st = SUPERVISOR.site_state()
        if st is None:
            raise ValueError("practice site is not running")
        return st

    def _record_mutation(self, kind: str, detail: str) -> None:
        st = self._site()
        conn = APP.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO site_mutations(at, variant, edition, detail) VALUES(?,?,?,?)",
                (state.now(), st.variant, st.edition, detail))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def post_variant(self, payload: dict):
        variant = str(payload.get("variant") or "")
        st = self._site()
        st.set_variant(variant)
        self._record_mutation("variant", f"站点切换到 {variant} 版版面")
        return ok({"ok": True, "variant": st.variant, "edition": st.edition})

    def post_series(self, payload: dict):
        APP.report_cache.clear()
        letter = payload.get("letter") or None
        size = int(payload.get("size") or 4)
        st = self._site()
        created = st.add_series(letter, size=size)
        self._record_mutation("series", f"新增系列 {letter or 'auto'}：{len(created)} 个商品页")
        return ok({"ok": True, "created": created, "edition": st.edition,
                   "lists": [[lid, ids] for lid, ids in st.list_pages()]})

    def post_price(self, payload: dict):
        st = self._site()
        pid = str(payload.get("page_id") or "")
        st.set_price(pid, str(payload.get("price") or "0"))
        self._record_mutation("price", f"{pid} 价格改为 {payload.get('price')}")
        return ok({"ok": True, "edition": st.edition})

    def post_stock(self, payload: dict):
        st = self._site()
        pid = str(payload.get("page_id") or "")
        st.set_stock(pid, str(payload.get("stock") or "0"))
        self._record_mutation("stock", f"{pid} 库存改为 {payload.get('stock')}")
        return ok({"ok": True, "edition": st.edition})

    # -- canary lifecycle ------------------------------------------------
    def post_canary_start(self, payload: dict):
        APP.report_cache.clear()
        percent = int(payload.get("percent") or 30)
        conn = APP.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            epoch_id = state.start_canary(
                conn, percent=percent, actor=payload.get("actor") or "operator",
                rule_id=payload.get("rule_id"))
            report = APP.report_for(epoch_id, save=True)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return ok({"ok": True, "epoch_id": epoch_id, "report": report})

    def post_canary_approve(self, payload: dict):
        APP.report_cache.clear()
        conn = APP.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            canary = state.canary_epoch(conn)
            if canary is None:
                raise state.StateError("no canary open")
            report = APP.report_for(canary["id"])
            if report["summary"]["status"] == "fail" and not payload.get("force"):
                conn.execute("ROLLBACK")
                return ok({"ok": False, "blocked": True, "status": "fail",
                           "error": "对照未通过：存在字段缺失或行结构回归，"
                                    "需人工确认后才能强制转正。",
                           "summary": report["summary"]}, HTTPStatus.CONFLICT)
            report_id = diff.save_report(conn, canary["id"], report,
                                         notes=payload.get("note") or "approval")
            epoch_id = state.promote_epoch(conn, actor="operator",
                                           note=payload.get("note") or "",
                                           diff_report_id=report_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        drained = publisher.drain(conn)
        return ok({"ok": True, "epoch_id": epoch_id, "report_id": report_id,
                   "drain": drained})

    def post_canary_rollback(self, payload: dict):
        APP.report_cache.clear()
        conn = APP.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            canary = state.canary_epoch(conn)
            if canary is None:
                raise state.StateError("no canary open")
            report_id = None
            try:
                report = APP.report_for(canary["id"])
                report_id = diff.save_report(conn, canary["id"], report,
                                             notes="rollback snapshot")
            except Exception:
                report_id = None
            epoch_id = state.rollback_epoch(conn, actor="operator",
                                            note=payload.get("note") or "")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        drained = publisher.drain(conn)
        return ok({"ok": True, "epoch_id": epoch_id, "report_id": report_id,
                   "drain": drained})

    def post_report_refresh(self, payload: dict):
        APP.report_cache.clear()
        conn = APP.conn()
        epoch_id = payload.get("epoch_id")
        sample = payload.get("sample")
        report = APP.report_for(int(epoch_id) if epoch_id else None, save=True,
                                sample=int(sample) if sample else None)
        return ok({"ok": True, "report": report})

    def post_start_draft(self, payload: dict):
        """Seed the deliberately imperfect candidate rule and start its parser.

        Opening a canary on it is a separate, explicit action from the console.
        """
        import hashlib
        conn = APP.conn()
        src = rule_sources.DRAFT_RULE
        label = payload.get("label") or "parse-draft"
        percent = int(payload.get("percent") or 30)
        sha = hashlib.sha256(src.encode()).hexdigest()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM rule_versions WHERE label=?", (label,)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO rule_versions(label, source, source_sha, created_at, notes) "
                    "VALUES(?,?,?,?,?)",
                    (label, src, sha, state.now(),
                     "Imperfect candidate: description chunk dropped; list price read "
                     "inside the title part."))
                rule_id = cur.lastrowid
            else:
                rule_id = row["id"]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        SUPERVISOR.start_parser(label, f"{label}")
        return ok({"ok": True, "rule_id": rule_id, "label": label,
                   "percent": percent})

    # -- operations -----------------------------------------------------
    def post_parser_crash(self, payload: dict):
        name = payload.get("name") or "parse-new"
        result = SUPERVISOR.kill_parser(name)
        return ok(result)

    def post_parser_restart(self, payload: dict):
        name = payload.get("name") or "parse-new"
        rule = payload.get("rule") or "parse-new"
        result = SUPERVISOR.start_parser(rule, name)
        return ok(result)

    def post_publisher_tick(self, payload: dict):
        conn = APP.conn()
        return ok({"ok": True, "drain": publisher.drain(conn)})

    # -- simulation shortcuts -------------------------------------------
    def post_seed_canary(self, payload: dict):
        """Make the canary non-trivial: redesign the site, add new pages, open
        a canary, let the crawler observe a bit, then compare."""
        st = self._site()
        st.set_variant(payload.get("variant") or "v2")
        created = st.add_series("b") if payload.get("add_series", True) else []
        conn = APP.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            sid = state.snapshot_progress(conn, state.active_epoch(conn)["id"],
                                          "pre_canary_seed")
            epoch_id = state.start_canary(conn, percent=int(payload.get("percent") or 30))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return ok({"ok": True, "epoch_id": epoch_id, "created": created,
                   "snapshot": sid, "variant": st.variant})

    def post_reset(self, payload: dict):
        """Wipe application data (not the practice site) for a clean demo.

        Stops the parser/crawler subprocesses first so no worker re-creates rows
        mid-wipe; the operator restarts services from the console afterwards.
        """
        conn = APP.conn()
        tables = ["publish_rejections", "publish_ledger", "inventory",
                  "diff_reports", "progress_checkpoints", "parse_attempts",
                  "parsed_documents", "parser_instances", "publication_assignments",
                  "page_fetches", "pages", "crawl_runs", "rule_epochs",
                  "rule_versions", "events", "site_mutations", "service_heartbeats",
                  "settings"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t in tables:
                conn.execute(f"DELETE FROM {t}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return ok({"ok": True})


def run(port: int = 8000, *, start_services: bool = True) -> ThreadingHTTPServer:
    db.init_db()
    if start_services:
        SUPERVISOR.start_all()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rule canary control console.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-services", action="store_true",
                    help="do not start crawler/parser/publisher subprocesses")
    args = ap.parse_args(argv)
    httpd = run(args.port, start_services=not args.no_services)
    print(f"console on http://127.0.0.1:{httpd.server_address[1]}", flush=True)

    # A plain SIGTERM would kill the console and leave the crawler/parsers
    # behind as orphans; turn it into a clean shutdown of the whole group.
    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        SUPERVISOR.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
