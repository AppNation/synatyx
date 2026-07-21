#!/usr/bin/env python3
"""Synatyx automatic session capture — Claude Code SessionEnd/Stop hook.

Reads the hook payload from stdin (session_id, transcript_path, cwd), builds
a compact digest of the session from the transcript, and POSTs it to the
Synatyx `/capture` endpoint as an L2 episodic memory. Memory capture stops
depending on the agent remembering to store anything.

Stdlib only — no synatyx imports, no venv needed. Always exits 0 so a broken
capture can never block a session from ending.

Configuration (environment):
    SYNATYX_URL       base URL of the Synatyx HTTP server (default http://localhost:9000)
    SYNATYX_AUTH_KEY  admin key, sent as X-Auth-Key (omit if auth is disabled)
    SYNATYX_USER_ID   memory user id (default: current OS username)

Claude Code settings.json:
    {
      "hooks": {
        "SessionEnd": [{"hooks": [{"type": "command",
          "command": "python3 /path/to/synatyx/scripts/capture_session.py"}]}]
      }
    }
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

MAX_FIRST_CHARS = 300
MAX_LAST_CHARS = 1200


def extract_text(content: Any) -> str:
    """Pull plain text out of a transcript message content field.

    Content is either a plain string or a list of blocks; only text blocks
    matter (tool calls and results are noise for a digest).
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def read_transcript(path: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def build_digest(entries: list[dict[str, Any]], cwd: str) -> str | None:
    """Compose the session digest: opening request + closing summary.

    The last assistant message is usually a summary of what was done — the
    single most capture-worthy text in the whole transcript.
    """
    user_texts: list[str] = []
    assistant_texts: list[str] = []

    for entry in entries:
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = extract_text(message.get("content"))
        if not text:
            continue
        role = message.get("role") or entry.get("type")
        if role == "user":
            # skip tool results and system-injected content
            if not text.startswith(("<", "[Request interrupted")):
                user_texts.append(text)
        elif role == "assistant":
            assistant_texts.append(text)

    if not user_texts and not assistant_texts:
        return None

    workspace = Path(cwd).name if cwd else "unknown"
    parts = [f"Session capture ({workspace}, {len(user_texts)} user turns)."]
    if user_texts:
        parts.append(f'Started with: "{user_texts[0][:MAX_FIRST_CHARS]}"')
    if assistant_texts:
        parts.append(f"Ended with: {assistant_texts[-1][:MAX_LAST_CHARS]}")
    return "\n".join(parts)


def post_capture(payload: dict[str, Any]) -> bool:
    url = os.getenv("SYNATYX_URL", "http://localhost:9000").rstrip("/") + "/capture"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    auth_key = os.getenv("SYNATYX_AUTH_KEY")
    if auth_key:
        request.add_header("X-Auth-Key", auth_key)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        print(f"synatyx capture: POST failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return 0

    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")

    digest = build_digest(read_transcript(transcript_path), cwd)
    if not digest:
        return 0

    project = Path(cwd).name.lower().replace(" ", "-") if cwd else None
    payload = {
        "user_id": os.getenv("SYNATYX_USER_ID") or getpass.getuser(),
        "content": digest,
        "session_id": project,
        "project": project,
        "memory_layer": "L2",
        "importance": 0.6,
        "origin": "agent-inferred",
        "metadata": {
            "type": "session-capture",
            "source": "claude-code-hook",
            "claude_session_id": hook_input.get("session_id"),
            "cwd": cwd,
        },
    }
    post_capture(payload)
    return 0  # never block session exit


if __name__ == "__main__":
    sys.exit(main())
