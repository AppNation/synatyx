from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Length of the stored hash prefix — 16 hex chars of sha256 is plenty to
# detect a changed file while keeping payloads small.
_HASH_LEN = 16


def _hash_file(path: str) -> str | None:
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return digest[:_HASH_LEN]
    except OSError:
        return None


def compute_file_hashes(paths: list[str]) -> dict[str, str]:
    """Hash the files a memory refers to, at store time.

    Unreadable/missing paths are skipped — a memory about a file that doesn't
    exist locally (e.g. stored from another machine) simply carries no hash
    for it and is never flagged.
    """
    hashes: dict[str, str] = {}
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        digest = _hash_file(path)
        if digest is not None:
            hashes[path] = digest
    return hashes


def check_stale_files(metadata: dict[str, Any]) -> list[str]:
    """Return the referenced files that changed (or vanished) since store time.

    Reads `metadata.file_hashes` written by StoreService. A file that can no
    longer be read counts as stale — the memory refers to something that is
    gone. Files that still hash identically are fresh.
    """
    file_hashes = metadata.get("file_hashes")
    if not isinstance(file_hashes, dict) or not file_hashes:
        return []

    stale: list[str] = []
    for path, stored_hash in file_hashes.items():
        current = _hash_file(path)
        if current != stored_hash:
            stale.append(path)
    return stale


def annotate_staleness(dumped: dict[str, Any]) -> dict[str, Any]:
    """Attach staleness flags to a serialized item if its files changed.

    No-op (and no new keys) for items without file hashes, so responses stay
    unchanged for the common case.
    """
    try:
        stale = check_stale_files(dumped.get("metadata") or {})
    except Exception:  # never let staleness checks break a response
        logger.debug("Staleness check failed", exc_info=True)
        return dumped
    if stale:
        dumped["possibly_stale"] = True
        dumped["stale_files"] = stale
    return dumped
