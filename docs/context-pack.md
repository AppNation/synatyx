# `context_pack` — Task-Driven Context Assembly

`context_brief` orients a session; `context_pack` arms a task. Give it what you
are about to do and a token budget, and it returns one assembled, prompt-ready
context block selected by relevance — not by recency or scroll order.

```
context_pack(query="add retry logic to the payment webhook",
             user_id="taner", project="my-app", max_tokens=3000)
```

## What goes in the pack

| Section | Source | Selection |
|---------|--------|-----------|
| `identity` | L4 (`ctx_users`) | vector search against the query, topped up by importance |
| `memories` | L2 + L3 | full hybrid retrieval pipeline (dense + BM25 + signals + MMR), then 1-hop relation expansion |
| `checkpoints` | L3 pinned | vector search, `is_pinned` filter |
| `attempts` | L2 `type: attempt` | vector search — dead ends relevant to *this* task |
| `open_tasks` | PostgreSQL | BM25 token overlap with the query, in-progress first |
| `skills` | skill registry | `context_skill_find`; full body included only when budget allows |
| `code` | `ctx_<slug>__index` | hybrid index search (see [code-index.md](code-index.md)) — only when the project has an index |

## Budgeting

Each section gets a weighted share of `max_tokens` (memories dominate at 38%).
Two properties distinguish it from the brief's budgeter:

- **Renormalization** — absent sections (no index, no skills) give their weight
  back to the rest instead of losing it.
- **Spillover** — unspent budget from underfull sections is redistributed to
  sections that overflowed, so the pack arrives full.

## Output

```jsonc
{
  "sections": { "identity": [...], "memories": [...], ... },  // structured
  "rendered": "<!-- synatyx context_pack ... -->\n# Context for: ...",
  "token_estimate": 2870,
  "budget": { "memories": {"allocated": 1140, "used": 1090, "overflow": 2}, ... },
  "diagnostics": { ... }   // only when nothing matched — explains why
}
```

`rendered` is designed to be injected verbatim into a prompt. Every item
carries provenance (`[user-stated]`, `[agent-inferred]`, `[ingested-from-web]`)
and staleness markers (`[STALE: src/x.py]`) so the consuming agent can weigh
trust. Relation-expanded memories are prefixed `↳ <relation_type>`.

It is also exposed as the MCP prompt `pack-context`, so clients that speak MCP
prompts can inject a pack without a tool call.

## When to use which

| | `context_brief` | `context_pack` |
|---|---|---|
| When | session start | before any significant task |
| Selection | scroll + sort (recency, importance) | query relevance |
| Skills / code | never | yes |
| Replaces | get_project → retrieve → task_list dance | retrieve + task_list + skill_find + index_search |
