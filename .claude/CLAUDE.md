# Synatyx Long-Term Memory

You have access to the Synatyx context engine via MCP tools. Use them to persist and recall information across conversations.

## Available Tools

- `context_set_project` — Set the active project; all memory ops are scoped to its dedicated Qdrant collection (`ctx_<slug>`)
- `context_get_project` — Return the currently active project, or suggest the workspace folder name if none is set
- `context_store` — Save a piece of information to long-term memory. Also accepts a batch `items` array — prefer one batch call over N single calls when storing several facts
- `context_retrieve` — Search and recall relevant memories before answering. Pass `expand_relations: true` to also pull in memories linked to the results (1-hop, tagged `via_relation`)
- `context_get` — Fetch one memory directly by its item ID (no vector search)
- `context_relate` — Link two memories with a typed edge: `related_to`, `supersedes`, `part_of`, `depends_on`, `caused_by`, or any custom type
- `context_unrelate` — Remove a relation by relation ID or by source+target pair
- `context_related` — List the memories linked to an item plus the connecting edges; follows `supersedes` chains into deprecated items
- `context_visualize` — Render the memory graph as a Mermaid flowchart (nodes colored by layer, deprecated dashed, pinned bold, edges labeled by relation type)
- `context_alternatives` — Ask "what can I use for X?" — returns memories matching a purpose, each grouped with its alternatives (linked via `alternative_to`/`used_for` edges)
- `context_summarize` — Summarize and compress working memory for a session
- `context_score` — Re-rank a list of context items by relevance to a query
- `context_checkpoint` — Save a named, pinned snapshot of a decision or milestone (importance=1.0)
- `context_deprecate` — Mark an item as superseded; excluded from retrieval but never deleted. Pass `superseded_by: <new_item_id>` to auto-create a `supersedes` edge from the replacement
- `context_list` — Browse stored items without vector search; filter by layer, project, or checkpoints
- `context_ingest` — Parse any file (.docx, .pdf, .md, .py, .js, .ts, .go, …) or URL into chunks and store them automatically
- `context_task_add` — Add a new task to remember for later (title, description, priority, project)
- `context_task_list` — List pending or all tasks; call at session start to see what work is waiting
- `context_task_update` — Update a task's status, priority, title, or description
- `context_skill_store` — Save a skill definition. Writes full content to PostgreSQL and embeds only the description into Qdrant L3 for RAG matching
- `context_skill_find` — RAG search for the best matching skill(s) for a given task. Embeds the query, searches Qdrant L3 filtered by type='skill', then fetches full content from PostgreSQL
- `context_skill_get` — Fetch a skill by exact name or slug from PostgreSQL
- `context_skill_list` — List all stored skills for the user, optionally scoped to a project
- `context_skill_delete` — Remove a skill from PostgreSQL and deprecate its Qdrant embedding
- `context_gc_stats` — Return GC statistics for the active project (expiring soon, deprecated, pending hard delete, protected)

## Project Namespacing

Each project gets its own dedicated Qdrant collection named `ctx_<slug>` (e.g. `ctx_synatyx`, `ctx_taty_v2`). The active project is persisted in Redis per user and survives server restarts.

- Call `context_set_project` at the start of a session to activate a project
- If unsure of the project name, call `context_get_project` — it will suggest the workspace folder name
- `session_id` still scopes Redis L1 retrieval within a project

### L4 is always user-global
L4 (procedural preferences — coding style, workflow rules, user facts) is **never** project-scoped. It always routes to the shared `ctx_users` collection regardless of the active project. Store user preferences, email, communication style, etc. as L4 — they follow the user across all projects.

## When to Call `context_retrieve`

Call `context_retrieve` at the **start of every new conversation** and whenever the user asks about something that may have been discussed before:

- At conversation start: query with the user's first message to surface relevant past context
- When the user references a previous decision, preference, or task ("like we did before", "as we discussed")
- Before starting any significant new task (architecture decisions, new features, debugging sessions)
- When asked about the project, tech stack, or conventions

Parameters to use:
- `user_id`: derive from system username (`whoami`) or ask the user once if it cannot be determined
- `query`: a short description of what you are looking for
- `session_id`: the project slug (e.g. `"taty-v2"`) to scope results to that project — omit only for cross-project queries
- `project`: the project name (e.g. `"taty-v2"`) for Qdrant-level filtering — use alongside `session_id` for maximum isolation
- `top_k`: `5` for general queries, `10` for broad topic searches

## When to Call `context_store`

Store information **proactively** during or after a conversation whenever something worth remembering is established:

- User decisions: chosen libraries, patterns, architecture choices
- Bugs found and their root causes
- Project conventions or preferences the user states
- Task outcomes: what was built, what was deployed, what was changed
- User preferences for communication style or workflow
- Important facts about the codebase (e.g. "Qdrant runs on port 6333", "RUN_MODE=mcp for stdio")

Parameters to use:
- `user_id`: derive from system username (`whoami`) or ask the user once if it cannot be determined
- `content`: a clear, standalone description (write it so it makes sense without the surrounding conversation)
- `memory_layer`: pick the appropriate layer:
  - `L1` — transient facts for the current session (ephemeral decisions, scratch notes)
  - `L2` — episodic memories (what happened in this conversation, summaries)
  - `L3` — semantic facts (stable knowledge: project structure, tech stack, how something works)
  - `L4` — procedural preferences (user-global: coding style, workflow rules, personal facts) → always stored in `ctx_users`
- `importance`: `0.0`–`1.0` (use `0.9`+ for architectural decisions, `0.5`–`0.7` for useful facts, `0.3` for minor details)
- `session_id`: use the project slug for project-specific facts (e.g. `"taty-v2"`), or a descriptive slug for global/cross-project facts (e.g. `"user-preferences"`)

## When to Use Relations

Link memories whenever facts belong together — related items retrieved as a group are far more useful than isolated fragments:

- **A decision replaces an older one** → store the new fact, then `context_deprecate` the old item with `superseded_by: <new_id>` (creates the `supersedes` edge in one call)
- **A fact depends on another** (e.g. "webhook secret rotation" depends on "payments use Stripe webhooks") → `context_relate` with `depends_on`
- **A bug/root-cause pair** → `caused_by`; **a sub-decision of a bigger architecture choice** → `part_of`
- When storing several facts about the same feature or decision, store them (batch mode), then relate them so future retrieval pulls the full picture
- When retrieving before a significant task, pass `expand_relations: true` so linked context comes along automatically

## Alternative Detection — Act on Store Responses

Every store (L2–L4) automatically checks for existing memories serving the same purpose:

- `auto_linked` in the response → an `alternative_to` edge was already created (near-identical purpose). Nothing to do, but mention it if relevant.
- `suggestions` in the response → probable same-purpose matches. **Review them immediately**: if a suggestion is genuinely the same purpose, confirm it with `context_relate` (type `alternative_to`); if not, ignore it. Do not ask the user — judge from content.
- When the user asks "what can I use for X?" or "what are my options for X?", call `context_alternatives` with the purpose as the query.

## When to Call `context_visualize`

Call it whenever the user asks to "see", "show", "map", or "visualize" their memories, decisions, or how things connect — and proactively after building up a cluster of related memories, to confirm the structure looks right.

- Always put the returned `mermaid` string in a ```mermaid code fence so it renders as a diagram
- Useful parameters: `project` (one project's graph), `relations_only: true` (hide isolated nodes), `memory_layer: "L4"` (only user preferences), `include_deprecated: false` (hide superseded items), `direction: "TD"` (vertical), `limit` (default 50)
- Reading it: green = L3 project knowledge, purple = L4 user preferences, blue = L2 session summaries, amber = L1, dashed gray = deprecated, thick border = pinned/checkpoint; edge labels are the relation types
- Note: L1 memories live in Redis only and never appear in the graph

## When to Call `context_ingest`

Always pass `session_id` set to the project name when ingesting a codebase or document:

```
context_ingest(source=<file>, user_id=..., session_id="<project-name>", project="<project-name>")
```

This ensures all ingested chunks are retrievable in isolation per project.

## Workflow

1. User opens a new chat → call `context_get_project` to check the active project
2. If no project is set → call `context_set_project` with the suggested workspace folder name (confirm with user if needed)
3. Call `context_retrieve` with the user's first message as the query
4. Call `context_task_list` to surface pending work
5. Inject retrieved context into your reasoning before responding
6. During the conversation, call `context_store` whenever a decision or fact is established
7. At the end of a long session, call `context_summarize` to compress the session into L2

## General Rules

- Never ask the user whether to store something — just do it silently
- Do not store trivial chit-chat or greetings
- Keep stored content concise and self-contained (2–4 sentences max per item)
- Always retrieve before answering questions about past work or decisions
- Always pass `session_id` as the project slug — never omit it for project-specific operations
- Store user-level facts (email, preferences, style) as L4 — they go to `ctx_users` automatically

