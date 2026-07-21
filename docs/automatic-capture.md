# Automatic Session Capture

Memory that relies on the agent remembering to write is a diary nobody keeps. Agents forget, get interrupted, or run out of context — and the project's memory silently stays empty. Automatic capture removes the discipline dependency: **the session gets captured even when the agent stored nothing.**

---

## How it works

```
Claude Code session ends
        │  SessionEnd hook (stdin: session_id, transcript_path, cwd)
        ▼
scripts/capture_session.py          ← stdlib-only, always exits 0
        │  builds a digest: first user request + last assistant summary + turn count
        ▼
POST /capture  (Synatyx HTTP server, admin-key protected)
        │  same pipeline as context_store: sanitization, chunking,
        │  provenance, alternative detection
        ▼
L2 episodic memory in ctx_<project>   (metadata.type = "session-capture")
```

The last assistant message of a session is usually a summary of what was done — the single most capture-worthy text in the transcript. That, plus the opening request, is a genuinely useful episodic record, with no LLM call required.

---

## The `/capture` endpoint

`POST /capture` on the HTTP transport (`RUN_MODE=mcp-http`). Protected by the same admin-key middleware as the MCP routes.

| Field | Required | Default | Notes |
|---|---|---|---|
| `user_id` | ✅ | — | Memory user |
| `content` | ✅ | — | The digest / fact to store |
| `session_id` | — | `null` | Project slug |
| `project` | — | `null` | Stored into `metadata.project` |
| `memory_layer` | — | `L2` | Any layer; `L4` routes to `ctx_users` |
| `importance` | — | `0.6` | |
| `metadata` | — | `{}` | `{"source": "capture"}` is always added |
| `origin` | — | `agent-inferred` | Provenance tag |

It is a general ingestion point — session-end hooks, CI jobs ("deployed v2.3 to prod"), and cron scripts can all push memories without an MCP handshake.

---

## Setting up the Claude Code hook

1. Run the HTTP server somewhere reachable (`RUN_MODE=mcp-http`, `make run`).
2. Export the environment for the hook (e.g. in your shell profile):

```bash
export SYNATYX_URL=http://localhost:9000
export SYNATYX_AUTH_KEY=<your AUTH_ADMIN_KEY>
# SYNATYX_USER_ID defaults to your OS username
```

3. Add the hook to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/synatyx/scripts/capture_session.py"
          }
        ]
      }
    ]
  }
}
```

The script is deliberately boring: standard library only (no venv), 10-second network timeout, and it **always exits 0** — a broken capture can never block a session from ending. Failures go to stderr and are dropped.

## What gets stored

```
Session capture (my-api, 7 user turns).
Started with: "add JWT auth to the login endpoint"
Ended with: Done — auth middleware added in src/auth.ts, 12 tests green, pushed as a1b2c3d.
```

Stored as L2 with `metadata: {type: "session-capture", source: "claude-code-hook", claude_session_id, cwd}`, `origin: agent-inferred`, project derived from the workspace folder name. `context_brief` picks it up in `last_session` at the next session start, and the consolidation job (see [Memory Hygiene](memory-hygiene.md)) later merges accumulated captures into stable L3 facts.
