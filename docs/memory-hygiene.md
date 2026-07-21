# Memory Hygiene — Staleness & Consolidation

A wrong memory is worse than no memory: the agent acts on retrieved context *confidently*. These two mechanisms keep the store truthful over time — one detects when the world changed underneath a memory, the other merges episodic noise into stable knowledge.

---

## Type-aware staleness

### File content hashes

When a stored fact refers to specific files, list them in `metadata.files`:

```
context_store(
  content="Auth middleware lives in src/auth.ts; JWT config in src/config.py",
  memory_layer="L3",
  metadata={"files": ["src/auth.ts", "src/config.py"], "fact_type": "file-location"},
  ...
)
```

At store time, Synatyx hashes each readable file (sha256, 16-hex prefix) into `metadata.file_hashes`. At retrieval time (`context_retrieve` and `context_brief`), the hashes are re-checked and any memory whose files changed or vanished comes back flagged:

```json
{
  "content": "Auth middleware lives in src/auth.ts...",
  "possibly_stale": true,
  "stale_files": ["src/auth.ts"]
}
```

**Agent rule:** treat a `possibly_stale` memory as a hypothesis, not a fact — verify against the file, then re-store the corrected fact and deprecate the old item.

Notes:
- Hashing happens where the server runs; in stdio mode (Claude Code, Cursor) that's the same machine as the repo, which is the intended setup. Unreadable paths are skipped at store time and never flagged.
- Flags are added only when something is stale — responses are unchanged for the common case.

### TTL decay per fact type

Different facts rot at different speeds. Tag items with `metadata.fact_type` and GC scales their effective TTL:

| `fact_type` | Multiplier | Rationale |
|---|---|---|
| `file-location` | ×0.3 | Paths and symbols move constantly |
| `config` | ×0.7 | Ports, env vars, flags change often |
| *(untagged)* | ×1.0 | Classic importance-only behaviour |
| `architecture` | ×1.5 | Design decisions hold for a long time |
| `preference` | ×3.0 | How the user works barely changes |

`effective_ttl = base_ttl × (1 + importance × GC_IMPORTANCE_MULTIPLIER) × type_multiplier`

Override the table with `GC_FACT_TYPE_MULTIPLIERS` (JSON) in `.env`. Unknown types are neutral (×1.0). Pinned items, checkpoints, L4, and skills remain immune to GC as before.

---

## Consolidation — episodic → semantic

Humans don't keep every episodic trace; sleep merges them into semantic knowledge. Ten session memories about Qdrant config should become one L3 fact. The `Consolidator` does exactly that:

1. **Scan** each project collection for non-deprecated L2 items (per user). Attempt records, pinned items, skills, and previous consolidations are never touched.
2. **Cluster** by embedding similarity (greedy, cosine ≥ `CONSOLIDATION_SIMILARITY_THRESHOLD`, default 0.83).
3. **Merge** every cluster of ≥ `CONSOLIDATION_MIN_CLUSTER_SIZE` (default 3) into one L3 item:
   - content: newest-first bullet list, prefixed `[Consolidated from N episodic memories]` (deterministic, no LLM dependency)
   - embedding: cluster centroid — exactly what the members collectively matched on
   - importance: max of the cluster (capped at 0.9 so consolidations never become GC-immune)
   - `metadata: {type: "consolidated", consolidated_from: [...ids]}`
4. **Deprecate** the originals (never deleted) and link each to the merged item with a `supersedes` edge — history stays navigable via `context_related` and visible in `context_visualize`.

### When it runs

- **Background:** after every GC pass in the GC daemon (`RUN_MODE=gc`), when `CONSOLIDATION_ENABLED=true`.
- **On demand:** the `context_consolidate` MCP tool runs one pass over the active project's collection — useful for stdio-only setups without the daemon.

`CONSOLIDATION_MAX_MERGES_PER_RUN` (default 20) caps each pass as a safety valve; a failed cluster merge is logged and skipped, never fatal.
