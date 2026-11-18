"""Background process supervision for the demo app.

The web app starts the mutable practice site, the crawler, the two parser
workers (old rule + canary rule) and the publisher as real OS subprocesses.
Each parser is a separate process so "kill a parser" in the UI exercises real
crash recovery, not an in-process exception.

Everything is local.  Parser workers are restarted by the supervisor only if the
operator asks for it (or explicitly resumes one); a deliberate crash stays
crashed and the UI shows which pages are still uncovered.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db, state

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Managed:
    name: str
    kind: str                  # site | crawler | parser | publisher
    proc: subprocess.Popen | None = None
    rule: str | None = None
    started_at: float = 0.0
    restarts: int = 0
    last_exit: int | None = None
    log_path: Path | None = None
    expected_stop: bool = False
    extra: dict = field(default_factory=dict)


class Supervisor:
    def __init__(self):
        self.procs: dict[str, Managed] = {}
        self.site = None
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    # -- environment ----------------------------------------------------
    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"]
                                       if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        if extra:
            env.update(extra)
        return env

    def _spawn(self, name: str, kind: str, argv: list[str], *,
               rule: str | None = None, env: dict[str, str] | None = None,
               cwd: Path | None = None) -> Managed:
        config.ensure_dirs()
        log_path = config.log_dir() / f"{name}.log"
        log = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            argv, cwd=str(cwd or ROOT), env=self._env(env),
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        log.close()  # the child owns the descriptor; do not leak it in the app
        managed = Managed(name=name, kind=kind, proc=proc, rule=rule,
                          started_at=time.time(), log_path=log_path)
        with self.lock:
            self.procs[name] = managed
        return managed

    # -- site -----------------------------------------------------------
    def start_site(self, port: int | None = None) -> None:
        with self.lock:
            if self.site is not None:
                return
            from .site import PracticeSite
            port = port or int(os.environ.get("CANARY_SITE_PORT", "7789"))
            self.site = PracticeSite(port=port).start()
            self.site_url = self.site.url

    def site_state(self):
        return self.site.state if self.site else None

    def stop_site(self) -> None:
        with self.lock:
            if self.site is not None:
                self.site.stop()
                self.site = None

    # -- processes ------------------------------------------------------
    def start_crawler(self) -> dict:
        self.ensure_db()
        with self.lock:
            m = self.procs.get("crawler")
            if m and m.proc and m.proc.poll() is None:
                return {"ok": True, "already": True}
            if self.site is not None:
                env = {"CANARY_SITE_URL": self.site.url}
            else:
                env = {}
            self._spawn("crawler", "crawler", [sys.executable, "-m", "canary.crawler"],
                        env=env)
            return {"ok": True}

    def start_parser(self, rule_label: str, name: str | None = None) -> dict:
        self.ensure_db()
        # A crashed instance keeps its name so the restart resumes its progress;
        # the old record is marked crashed/superseded by the worker.
        name = name or f"{rule_label}"
        with self.lock:
            m = self.procs.get(name)
            if m and m.proc and m.proc.poll() is None:
                return {"ok": True, "already": True, "name": name}
            self._spawn(name, "parser",
                        [sys.executable, "-m", "canary.worker", "--rule", rule_label,
                         "--name", name],
                        rule=rule_label)
            return {"ok": True, "name": name}

    def stop_parser(self, name: str, *, kill: bool = False) -> dict:
        with self.lock:
            m = self.procs.get(name)
            if not m or not m.proc or m.proc.poll() is not None:
                return {"ok": False, "error": "not running"}
            m.expected_stop = not kill
            try:
                if kill:
                    os.killpg(os.getpgid(m.proc.pid), signal.SIGKILL)
                else:
                    os.killpg(os.getpgid(m.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            return {"ok": True, "name": name, "signal": "KILL" if kill else "TERM"}

    def kill_parser(self, name: str | None = None) -> dict:
        """Simulate an abrupt parser crash for a rule's parser process."""
        name = name or "parse-new"
        self.ensure_db()
        with self.lock:
            m = self.procs.get(name)
            if m and m.proc and m.proc.poll() is None:
                m.expected_stop = False
                os.killpg(os.getpgid(m.proc.pid), signal.SIGKILL)
                return {"ok": True, "name": name, "killed": True}
            # start one if it is not running so the demo always has a crash
            self.start_parser("parse-new", name)
            time.sleep(0.4)
            m = self.procs.get(name)
            if m and m.proc:
                m.expected_stop = False
                os.killpg(os.getpgid(m.proc.pid), signal.SIGKILL)
                return {"ok": True, "name": name, "killed": True, "started_first": True}
            return {"ok": False, "error": "could not start parser to crash"}

    def start_publisher(self) -> dict:
        self.ensure_db()
        with self.lock:
            m = self.procs.get("publisher")
            if m and m.proc and m.proc.poll() is None:
                return {"ok": True, "already": True}
            self._spawn("publisher", "publisher",
                        [sys.executable, "-m", "canary.publisher", "--interval", "0.7"])
            return {"ok": True}

    def ensure_db(self):
        conn = db.init_db()
        state.ensure_baseline(conn)
        return conn

    def start_all(self) -> None:
        self.start_site()
        self.ensure_db()
        self.start_crawler()
        self.start_parser("parse-old", "parse-old")
        self.start_parser("parse-new", "parse-new")
        self.start_publisher()
        with self.lock:
            if self._monitor is None:
                self._monitor = threading.Thread(target=self._monitor_loop,
                                                 daemon=True, name="supervisor-monitor")
                self._monitor.start()

    def _monitor_loop(self) -> None:
        conn = None
        while not self._stop.is_set():
            with self.lock:
                items = list(self.procs.items())
            for name, m in items:
                if m.proc is None or m.proc.poll() is None:
                    continue
                code = m.proc.returncode
                if m.last_exit != code:
                    m.last_exit = code
                    try:
                        conn = conn or db.connect()
                        if m.kind == "parser":
                            status = "stopped" if m.expected_stop else "crashed"
                            # Guard on pid: a restart may already have reused the
                            # same instance name with a new process.
                            conn.execute(
                                "UPDATE parser_instances SET status=? WHERE id=? "
                                "AND status='running' AND pid=?",
                                (status, name, m.proc.pid))
                            state.event(conn, "supervisor",
                                        "parser_process_exit",
                                        f"{name} exited with code {code} "
                                        f"({'requested' if m.expected_stop else 'unexpected'}).",
                                        exit_code=code, expected=m.expected_stop)
                        else:
                            state.event(conn, "supervisor", "process_exit",
                                        f"{name} exited with code {code}.",
                                        exit_code=code)
                    except Exception:
                        pass
            time.sleep(0.5)
        if conn is not None:
            conn.close()

    # -- status ---------------------------------------------------------
    def reap(self) -> list[dict]:
        """Mark parser instances whose OS process vanished as crashed.

        The supervisor's monitor loop normally does this on exit; this sweep
        also catches workers started outside the supervisor (CLI or tests) and
        keeps the UI honest across an app restart.
        """
        conn = self.ensure_db()
        reaped = []
        for inst in conn.execute(
                "SELECT * FROM parser_instances WHERE status='running'").fetchall():
            managed = self.procs.get(inst["id"])
            alive = bool(managed and managed.proc and managed.proc.poll() is None)
            if alive:
                continue
            if managed is not None:
                # supervisor owns it and knows it is not running: it will be
                # marked by the monitor loop; force it here for immediacy
                pass
            try:
                os.kill(inst["pid"], 0)
                pid_alive = True
            except OSError:
                pid_alive = False
            if not pid_alive:
                conn.execute(
                    "UPDATE parser_instances SET status='crashed', "
                    "last_error=COALESCE(last_error, 'process disappeared') "
                    "WHERE id=? AND status='running'", (inst["id"],))
                reaped.append({"instance": inst["id"], "pid": inst["pid"]})
        return reaped

    def status(self) -> dict:
        with self.lock:
            procs = []
            for name, m in self.procs.items():
                alive = bool(m.proc and m.proc.poll() is None)
                procs.append({
                    "name": name,
                    "kind": m.kind,
                    "rule": m.rule,
                    "pid": m.proc.pid if m.proc else None,
                    "alive": alive,
                    "exit_code": m.proc.returncode if m.proc and not alive else None,
                    "started_at": m.started_at,
                    "expected_stop": m.expected_stop,
                    "restarts": m.restarts,
                    "log": str(m.log_path) if m.log_path else None,
                })
            site = {
                "running": self.site is not None,
                "url": self.site.url if self.site else None,
            }
            if self.site is not None:
                st = self.site.state
                site.update({
                    "variant": st.variant,
                    "edition": st.edition,
                    "items": len(st.items),
                    "list_pages": len(st.list_pages()),
                    "requests": st.request_count,
                })
        return {"processes": procs, "site": site, "at": time.time()}

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            for name, m in list(self.procs.items()):
                if m.proc and m.proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(m.proc.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        time.sleep(0.4)
        with self.lock:
            for name, m in list(self.procs.items()):
                if m.proc and m.proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(m.proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.stop_site()


SUPERVISOR = Supervisor()
