#!/usr/bin/env python3
"""Push-index a local project into a remote Synatyx code/doc index.

The server can't read your filesystem, so this script does the walking:
hash every indexable file, ask the server what changed (POST /index/diff),
upload only those files (POST /index/files), and prune deletions.
Idempotent and cheap — an unchanged repo is one small diff request.

Stdlib only. Designed for hooks (git post-commit, Claude Code SessionStart,
cron) — always exits 0 so it can never break the calling workflow.

Usage:
    python scripts/index_project.py [--root DIR] [--project NAME] [--force] [--dry-run]

Env:
    SYNATYX_URL       server base URL      (default: http://localhost:9000)
    SYNATYX_AUTH_KEY  admin key            (same as AUTH_ADMIN_KEY)
    SYNATYX_USER_ID   memory user id       (default: OS username)
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

# Mirror of the server-side filters (src/core/index.py) — keep in sync.
EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cpp", ".c",
    ".rb", ".php", ".swift", ".kt", ".yml", ".yaml", ".toml", ".json",
    ".md", ".mdx", ".markdown",
}
EXCLUDED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", "target", ".next", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".idea", ".vscode",
}
EXCLUDED_FILES = {"uv.lock", "package-lock.json", "yarn.lock", "poetry.lock"}
MAX_FILE_BYTES = 200_000
UPLOAD_BATCH_FILES = 16
UPLOAD_BATCH_BYTES = 800_000


def list_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            return [root / line for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        found.extend(Path(dirpath) / name for name in filenames)
    return found


def indexable(path: Path) -> bool:
    if path.suffix.lower() not in EXTENSIONS or path.name in EXCLUDED_FILES:
        return False
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return False
        with open(path, "rb") as fh:
            return b"\x00" not in fh.read(1024)
    except OSError:
        return False


def post(url: str, key: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Auth-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("--project", default=None, help="project slug (default: root folder name)")
    ap.add_argument("--force", action="store_true", help="re-upload everything")
    ap.add_argument("--dry-run", action="store_true", help="show the diff, upload nothing")
    args = ap.parse_args()

    base = os.getenv("SYNATYX_URL", "http://localhost:9000").rstrip("/")
    key = os.getenv("SYNATYX_AUTH_KEY", "")
    user_id = os.getenv("SYNATYX_USER_ID") or getpass.getuser()

    root = Path(args.root).resolve()
    project = args.project or root.name

    files = [f for f in list_files(root) if indexable(f)]
    manifest: dict[str, str] = {}
    for f in files:
        try:
            rel = str(f.resolve().relative_to(root))
            manifest[rel] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        except (OSError, ValueError):
            continue

    diff = post(f"{base}/index/diff", key, {
        "user_id": user_id, "project": project, "files": manifest,
    })
    changed = list(manifest) if args.force else diff.get("changed", [])
    removed = diff.get("removed", [])
    print(f"[synatyx-index] {project}: {len(manifest)} files — "
          f"{len(changed)} to upload, {len(removed)} to prune, "
          f"{diff.get('unchanged', 0)} unchanged")

    if args.dry_run or (not changed and not removed):
        return

    totals = {"files_indexed": 0, "files_failed": 0, "chunks_upserted": 0, "chunks_pruned": 0}
    batch: list[dict] = []
    batch_bytes = 0
    pending_prune = list(removed)

    def flush() -> None:
        nonlocal batch, batch_bytes, pending_prune
        if not batch and not pending_prune:
            return
        result = post(f"{base}/index/files", key, {
            "user_id": user_id, "project": project,
            "files": batch, "prune": pending_prune, "force": args.force,
        })
        for k in totals:
            totals[k] += result.get(k, 0)
        batch, batch_bytes, pending_prune = [], 0, []

    for rel in changed:
        try:
            content = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        batch.append({"path": rel, "content": content})
        batch_bytes += len(content)
        if len(batch) >= UPLOAD_BATCH_FILES or batch_bytes >= UPLOAD_BATCH_BYTES:
            flush()
    flush()

    print(f"[synatyx-index] done: {totals['files_indexed']} indexed, "
          f"{totals['chunks_upserted']} chunks, {totals['chunks_pruned']} pruned"
          + (f", {totals['files_failed']} FAILED" if totals["files_failed"] else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # hook-safe: never break the caller
        print(f"[synatyx-index] skipped: {exc}", file=sys.stderr)
    sys.exit(0)
