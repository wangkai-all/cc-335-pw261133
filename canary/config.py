"""Runtime configuration.

Every path is resolved lazily so tests can point CANARY_VAR at a temporary
directory before they touch the database or raw store.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def var_dir() -> Path:
    return Path(os.environ.get("CANARY_VAR", str(ROOT / "var"))).resolve()


def db_path() -> Path:
    return Path(os.environ.get("CANARY_DB", str(var_dir() / "canary.db")))


def raw_dir() -> Path:
    return Path(os.environ.get("CANARY_RAW_DIR", str(var_dir() / "raw")))


def log_dir() -> Path:
    return Path(os.environ.get("CANARY_LOG_DIR", str(var_dir() / "logs")))


def site_url() -> str:
    return os.environ.get("CANARY_SITE_URL", "http://127.0.0.1:7789").rstrip("/")


def ensure_dirs() -> None:
    var_dir().mkdir(parents=True, exist_ok=True)
    raw_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)


def fetch_timeout() -> float:
    try:
        return float(os.environ.get("CANARY_FETCH_TIMEOUT", "8"))
    except ValueError:
        return 8.0


def crawl_interval() -> float:
    try:
        return float(os.environ.get("CANARY_CRAWL_INTERVAL", "3"))
    except ValueError:
        return 3.0


def parser_poll_interval() -> float:
    try:
        return float(os.environ.get("CANARY_PARSER_POLL", "0.4"))
    except ValueError:
        return 0.4
