# Synatyx — MCP Tools Reference

Synatyx exposes **26 MCP tools** over stdio and SSE, compatible with any MCP-compliant client (Augment Code, Cursor, Claude Desktop, Claude Code).

---

## Project Management

### `context_set_project`
Activate a project. All subsequent memory operations are scoped to a dedicated Qdrant collection (`ctx_<slug>`). Persisted in Redis — survives server restarts.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `project` | string | ✅ | Project name — slugified automatically |

### `context_get_project`
Return the currently active project, or suggest one based on the workspace folder name.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |

---

## Memory

### `context_brief`
One-call session-start digest — replaces the get_project → retrieve → task_list startup dance. Returns a token-budgeted briefing: `identity` (L4 preferences), `last_session` (recent L2), `project_knowledge` (pinned checkpoints + top L3), `recent_changes`, `recent_attempts` (tried-and-failed records), `open_tasks`, and `stats`. See [Session Brief & Trust](session-brief.md).

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `session_id` | string | — | Project slug — scopes open tasks |
| `project` | string | — | Qdrant-level project filter |
| `max_tokens` | integer | — | Budget for the whole briefing (default: 2000) |
| `recent_days` | integer | — | Window for `recent_changes` (default: 7) |

### `context_store`
Save a fact, decision, or note into the appropriate memory layer.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | ✅ | Content to store |
| `user_id` | string | ✅ | User identifier |
| `memory_layer` | L1\|L2\|L3\|L4 | ✅ | Target layer |
| `importance` | float | — | 0.0–1.0 (default: 0.5) |
| `session_id` | string | — | Project slug for scoping |
| `metadata` | object | — | Extra metadata |
| `confidence` | float | — | 0.0–1.0 (default: 1.0) |
| `origin` | string | — | Provenance: `user-stated`, `agent-inferred` (default), `web-search` — see [Session Brief & Trust](session-brief.md) |
| `items` | array | — | **Batch mode**: store many entries in one call — see [Efficiency Improvements](efficiency-improvements.md) |

To record a failed approach, store an L2 item with `metadata: {type: "attempt", goal, approach, outcome: "failed", why}` — `context_brief` surfaces these so future sessions don't repeat dead ends.

### `context_retrieve`
Hybrid semantic search across memory layers — dense + BM25 + MMR + score fusion.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | ✅ | Search query |
| `user_id` | string | ✅ | User identifier |
| `session_id` | string | — | Project slug to scope results |
| `project` | string | — | Qdrant-level project filter |
| `top_k` | integer | — | Max results (default: 10) |
| `memory_layers` | array | — | Filter to specific layers (default: all) |
| `expand_relations` | boolean | — | Also return 1-hop related memories, tagged `via_relation` — see [Memory Relations](memory-relations.md) |

When the result is empty, the response includes a `diagnostics` block (item counts by layer, filters applied, and a hint) that distinguishes "nothing stored" from "filters/layers missed" — see [Session Brief & Trust](session-brief.md).

### `context_summarize`
Compress session working memory into an L2 episodic summary via LLM. Runs async.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | ✅ | Session to summarize |
| `user_id` | string | ✅ | User identifier |
| `max_tokens` | integer | — | Summary length cap (default: 500) |
| `focus` | string | — | What to focus on |

### `context_score`
Re-rank a list of context items by relevance to a query.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | array | ✅ | Context items to score |
| `query` | string | ✅ | Query to score against |

---

## Knowledge

### `context_checkpoint`
Save a named, pinned L3 snapshot with importance=1.0. Never excluded from retrieval.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✅ | Checkpoint name |
| `content` | string | ✅ | What to snapshot |
| `user_id` | string | ✅ | User identifier |
| `project` | string | — | Project scope |
| `session_id` | string | — | Project slug |

### `context_deprecate`
Mark an item as superseded. It stays in the store but is excluded from retrieval.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `item_id` | string | ✅ | ID of item to deprecate |
| `user_id` | string | ✅ | User identifier |
| `reason` | string | — | Why it's deprecated |
| `superseded_by` | string | — | ID of the replacing item — also creates a `supersedes` relation edge |

### `context_list`
Browse stored items without vector search — for reviewing checkpoints or finding items to deprecate.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `memory_layer` | L1\|L2\|L3\|L4 | — | Filter by layer |
| `checkpoints_only` | boolean | — | Return only checkpoints |
| `include_deprecated` | boolean | — | Include deprecated items |
| `project` | string | — | Filter by project |
| `limit` | integer | — | Max results (default: 50) |

### `context_ingest`
Parse any file or URL into chunks and store them automatically.

Supports: `.docx`, `.pdf`, `.md`, `.py`, `.ts`, `.go`, `.rs`, and any `http(s)://` URL.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | ✅ | Absolute file path or URL |
| `user_id` | string | ✅ | User identifier |
| `memory_layer` | L1\|L2\|L3\|L4 | — | Target layer (default: L3) |
| `importance` | float | — | 0.0–1.0 (default: 0.8) |
| `project` | string | — | Project tag |
| `session_id` | string | — | Project slug |

---

## Relations & Graph

> Full guides: [Memory Relations](memory-relations.md) · [Memory Visualization](memory-visualization.md)

### `context_relate`
Link two memories with a typed, directed edge (`related_to`, `supersedes`, `part_of`, `depends_on`, `caused_by`, or custom).

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `source_id` | string | ✅ | Item the edge starts from |
| `target_id` | string | ✅ | Item the edge points to |
| `user_id` | string | ✅ | User identifier |
| `relation_type` | string | — | Edge type (default: `related_to`) |
| `metadata` | object | — | Extra context on the edge |

### `context_unrelate`
Delete edge(s) by relation ID or by source+target pair.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `relation_id` | string | — | Exact edge to delete |
| `source_id` / `target_id` | string | — | Endpoint pair (alternative) |
| `relation_type` | string | — | Narrow deletion to this type |

### `context_related`
List memories linked to an item plus the connecting edges. Follows supersedes chains into deprecated items.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `item_id` | string | ✅ | Anchor item |
| `user_id` | string | ✅ | User identifier |
| `relation_type` | string | — | Only follow this edge type |
| `direction` | string | — | `out`, `in`, or `both` (default) |

### `context_get`
Fetch one memory directly by ID — no vector search. Checks the project collection, then `ctx_users`.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `item_id` | string | ✅ | Item to fetch |
| `user_id` | string | ✅ | User identifier |

### `context_visualize`
Render the memory graph as a Mermaid flowchart — nodes colored by layer, deprecated dashed, pinned bold, edges labeled by type.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `project` | string | — | Filter by project |
| `memory_layer` | L1\|L2\|L3\|L4 | — | Filter by layer (L4 reads `ctx_users`) |
| `relations_only` | boolean | — | Hide isolated nodes |
| `include_deprecated` | boolean | — | Show deprecated items (default: true) |
| `direction` | string | — | `LR` (default) or `TD` |
| `limit` | integer | — | Max items (default: 50) |

### `context_alternatives`
Answer "what can I use for X?" — semantic search for a purpose, grouping each match with its alternatives (`alternative_to` / `used_for` neighbors). Alternatives are detected automatically at store time — see [Alternative Detection](alternatives.md).

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `query` | string | ✅ | Purpose to search, e.g. "approve button component" |
| `top_k` | integer | — | Max groups (default: 5) |

---

## Tasks

### `context_task_add`
Add a persistent task that survives across sessions.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | ✅ | Short task title |
| `user_id` | string | ✅ | User identifier |
| `description` | string | — | Detailed description |
| `priority` | low\|medium\|high | — | Priority (default: medium) |
| `project` | string | — | Project scope |

### `context_task_list`
List tasks, optionally filtered by status, priority, or project.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `status` | pending\|in_progress\|done\|cancelled | — | Filter by status |
| `priority` | low\|medium\|high | — | Filter by priority |
| `project` | string | — | Filter by project |
| `limit` | integer | — | Max results (default: 50) |

### `context_task_update`
Update a task's status, priority, title, or description.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | ✅ | Task ID |
| `user_id` | string | ✅ | User identifier |
| `status` | pending\|in_progress\|done\|cancelled | — | New status |
| `priority` | low\|medium\|high | — | New priority |
| `title` | string | — | Updated title |
| `description` | string | — | Updated description |

---

## Skills

Skills are named agent role definitions (system prompt + capabilities) stored in PostgreSQL and indexed in Qdrant for RAG-based discovery.

### `context_skill_store`
Save a skill definition. Writes full content to PostgreSQL and embeds only the description into Qdrant L3.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✅ | Skill name (e.g. `nodejs-developer`) |
| `description` | string | ✅ | One-line description for RAG matching |
| `content` | string | ✅ | Full skill content (system prompt + instructions) |
| `user_id` | string | ✅ | User identifier |
| `project` | string | — | Project scope (null = global) |
| `frontmatter` | object | — | Parsed YAML frontmatter fields |

### `context_skill_find`
RAG search — embed query → search Qdrant L3 (type=skill) → fetch full content from PostgreSQL.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | ✅ | Task description to match |
| `user_id` | string | ✅ | User identifier |
| `project` | string | — | Limit to a specific project |
| `top_k` | integer | — | Max results (default: 3) |

### `context_skill_get`
Fetch a skill by exact name or slug from PostgreSQL.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✅ | Skill name or slug |
| `user_id` | string | ✅ | User identifier |
| `project` | string | — | Project scope filter |

### `context_skill_list`
List all stored skills for a user, optionally scoped to a project.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |
| `project` | string | — | Filter by project |
| `limit` | integer | — | Max results (default: 50) |

### `context_skill_delete`
Remove a skill from PostgreSQL and deprecate its Qdrant embedding.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✅ | Skill name or slug |
| `user_id` | string | ✅ | User identifier |

---

## Garbage Collection

### `context_gc_stats`
Return GC statistics for the active project — how many items are expiring soon, already deprecated, pending hard delete, or protected from GC.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User identifier |

**Response:**
```json
{
  "total_items": 1240,
  "protected": 310,
  "expiring_soon_14d": 42,
  "already_deprecated": 18,
  "pending_hard_delete": 6,
  "gc_enabled": true,
  "l2_base_ttl_days": 30,
  "l3_base_ttl_days": 90,
  "grace_period_days": 30
}
```

> The GC daemon runs as a separate Docker service (`synatyx-gc`). It does not need to be triggered manually — it runs on a configurable interval (default: 24h). Use `context_gc_stats` to monitor its state.

