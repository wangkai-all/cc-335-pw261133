"""Content-addressed raw page store shared by crawler and parsers.

Bytes are gzip-compressed and written atomically.  The file name IS the
SHA-256 of the *uncompressed* bytes, so a parser can verify that what it reads
is exactly what the crawler captured.  Identical payloads fetched years apart
occupy one file and one `page_fetches` row.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from pathlib import Path

from . import config


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def relative_path(sha: str) -> str:
    return f"{sha[:2]}/{sha}.gz"


def put(raw: bytes) -> tuple[str, str]:
    """Store raw bytes; return (sha, relative path). Idempotent."""
    sha = sha256_bytes(raw)
    rel = relative_path(sha)
    target = config.raw_dir() / rel
    if target.exists():
        return sha, rel
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sha, rel


def get(rel: str, expected_sha: str | None = None) -> bytes:
    path = config.raw_dir() / rel
    with gzip.open(path, "rb") as fh:
        raw = fh.read()
    if expected_sha and sha256_bytes(raw) != expected_sha:
        raise ValueError(f"raw payload hash mismatch for {rel}")
    return raw


def path_for(rel: str) -> Path:
    return config.raw_dir() / rel
