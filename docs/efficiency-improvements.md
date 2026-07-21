# Synatyx — Agent-Efficiency Improvements

Synatyx is used as live memory *during* agent tasks, so every round-trip and every ranking error costs the agent time or leads it astray. This release cut round-trips (batch store, direct get) and fixed a set of bugs that silently corrupted or degraded what agents got back.

---

## Batch Store

`context_store` accepts an `items` array alongside the classic single-item form. N facts → **one call, one embedding batch, one response**:

```json
{
  "user_id": "taner",
  "session_id": "myproject",
  "items": [
    { "content": "Payments service talks to Stripe via webhooks", "memory_layer": "L3" },
    { "content": "Webhook secret lives in vault, rotated quarterly", "memory_layer": "L3", "importance": 0.8 },
    { "content": "Prefers conventional commits", "memory_layer": "L4" }
  ]
}
```

- Each entry may set its own `memory_layer`, `importance`, and `metadata`; L4 entries are routed to `ctx_users` per entry.
- The response reports per-entry results — a failure in one entry doesn't abort the rest:

```json
{
  "results": [
    { "item_id": "…", "item_ids": ["…"], "embedded": true },
    { "item_id": "…", "item_ids": ["…"], "embedded": true },
    { "error": "…" }
  ],
  "stored": 2,
  "failed": 1
}
```

### All chunk ids returned

Long content gets chunked before embedding. Store responses now return `item_ids` with **every** chunk id (previously only the first), so relations and direct fetches can target specific chunks.

---

## Direct Fetch — `context_get`

Previously the only ways to read memory were vector search (`context_retrieve`) or scrolling (`context_list`). `context_get` fetches one item by id — project collection first, then `ctx_users` — with ownership enforced. Used pervasively by the [relations](memory-relations.md) feature and available as a standalone tool.

---

## Faster Retrieval

- **Parallel layer search** — the per-layer searches (L1–L4) now run concurrently via `asyncio.gather` instead of sequentially. Retrieval latency ≈ slowest layer, not the sum.
- **Batched access tracking** — marking retrieved items as "accessed" is one batched Qdrant `set_payload` call instead of one round-trip per item.
- **Score fusion fixed** — dense/BM25 fusion weights (0.6/0.4) were applied twice, compressing effective weights to 0.36/0.24 and distorting ranking. Fusion is now applied once, matching the documented design.

---

## Reliability Fixes

These bugs directly affected what agents got back from memory:

| Bug | Impact | Fix |
|-----|--------|-----|
| GC mutated the shared Qdrant client's collection name | Concurrent requests could read/write **another project's collection** | GC works on scoped storage clones; the shared client is never mutated |
| `context_gc_stats` unpacked services in the wrong order | Crashed on every call | Correct unpacking; returns real numbers |
| `deprecate` scanned a 1000-item listing to find its target | Items beyond 1000 could never be deprecated | Direct fetch by id |
| GC hard-delete ignored pagination | More than 500 deprecated items were never cleaned up | Offset-paginated deletion loop |
| `list_items` dropped `created_at` when reconstructing items | Recency scoring saw everything as "created now" | `created_at` preserved from payload |

---

## Practical Impact for Agents

A typical end-of-session memory sync used to be ~6 sequential tool calls (N stores + deprecations). It's now typically **2**: one batch `context_store`, one `context_deprecate` with `superseded_by` (which also creates the supersedes edge — see [Memory Relations](memory-relations.md)). Combined with parallel retrieval, both the read and write paths an agent pays on every task got measurably shorter.
