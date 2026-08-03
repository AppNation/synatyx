# Claude Code Rules

## Synatyx Memory — Run at Session Start

At the start of every new conversation, before doing anything else:

1. Call `context_brief` (user_id: derive from `whoami`, session_id: project slug) — one call returning identity, last session, project knowledge, recent changes, failed attempts, open tasks, and stats. It also reports the active project.
2. If the brief shows no/wrong project, call `context_set_project` with the workspace folder name
3. If the user's first message asks about something specific, add one focused `context_retrieve` with that as the query (top_k: 5)
4. Inject the briefing into your reasoning before responding

If a `context_retrieve` ever comes back empty, read its `diagnostics` block — it distinguishes "nothing stored" from "filters/layers missed" and tells you what to do next.

## Synatyx Memory — During the Session

- **MANDATORY: Always call `context_retrieve` BEFORE reading any file, searching code, or answering any question about the project** — if memory has sufficient context, answer directly without touching the filesystem. Only fall through to file reads or code searches if memory explicitly lacks the answer or user mentioned read directly from files.
- Call `context_store` silently whenever a decision, fact, or convention is established — pass `origin` ("user-stated" when the user said it, "agent-inferred" for your own conclusions, "web-search" for facts found online)
- When a fact refers to specific files, pass `metadata.files: [paths]` (hashed for staleness detection) and `metadata.fact_type` ("file-location" | "config" | "architecture" | "preference") for type-aware decay. Treat retrieved memories flagged `possibly_stale` as hypotheses — verify against the file first
- After a failed approach, store an attempt record: L2 with metadata `{type: "attempt", goal, approach, outcome: "failed", why}` — `context_brief` surfaces these next session
- Never ask the user whether to store something — just do it
- Use `session_id` as the project slug for all project-scoped operations
- **Always pass `project` (the project slug) explicitly on store/get/retrieve/list/deprecate calls** — it routes the call directly to that project's collection, overriding the active-project pointer. The pointer is one shared value per user, so concurrent sessions in different workspaces overwrite each other's routing; the explicit `project` argument is immune to that race
- Store user-level preferences as L4 (they go to `ctx_users` automatically)
- At the end of a long session, call `context_summarize` to compress into L2

## context-mode Indexing → Synatyx Sync

`ctx_index` (and `ctx_batch_execute`) store content in an **in-memory FTS5 SQLite database** — session-only, lost when Claude exits. After indexing any significant file (plan, architecture doc, schema), also persist the key facts into Synatyx:

1. After `ctx_index` on a plan or spec file → call `context_store` (L3) with the key decisions, step list, or architecture extracted from it
2. After `ctx_batch_execute` that reveals new facts about the codebase → call `context_store` (L3) to record what was found
3. After `ctx_search` returns significant findings (e.g. confirms a feature is implemented, reveals a pattern, uncovers a design decision) → call `context_store` (L3) to persist the finding
4. This ensures the next session can retrieve the same knowledge without re-indexing or re-searching

## Synatyx Memory — Keeping Memory Up to Date

- **Deprecate stale memories** — if you discover that a stored fact is outdated or wrong (e.g. a file was renamed, an API changed, a decision was reversed), call `context_deprecate` on the old item immediately
- **Update on change** — whenever the user changes a decision, preference, or convention, store the new version and deprecate the old one
- **Re-ingest on significant file changes** — if a key file (architecture, config, schema) is heavily modified during the session, re-ingest it so memory reflects the current state
- **Checkpoint milestones** — after completing a significant feature, refactor, or architectural decision, call `context_checkpoint` to pin it as a named snapshot (importance=1.0)
- **GC check** — periodically call `context_gc_stats` to surface items expiring soon or pending cleanup, and deprecate anything no longer relevant
- **Never let contradictions accumulate** — if retrieved context conflicts with what you observe in the code, trust the code, update memory, and note the correction

## Memory Layers

- `L1` — transient/session scratch notes
- `L2` — episodic (conversation summaries)
- `L3` — semantic (stable project knowledge, architecture, conventions)
- `L4` — procedural user preferences (global across all projects)
