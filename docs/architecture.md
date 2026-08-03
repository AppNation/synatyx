# Synatyx — Architecture

## How It Works

```mermaid
flowchart LR
    IDE(["🖥️ IDE\nClaude / Cursor / Augment"])
    MCP["⚙️ Synatyx\nMCP Server"]
    LLM(["🤖 LLM"])

    IDE -->|"MCP (streamable-HTTP / stdio)"| MCP
    MCP -->|assembled context injected| LLM

    subgraph Engine ["Context Engine"]
        PACK["📦 context_pack\nquery-driven assembly"]
        IDX["🗂️ ctx_<slug>__index\npersistent code/doc index"]
        subgraph Memory ["4-Layer Memory"]
            L1["🔴 L1 · Redis\nWorking Memory"]
            L2["🟠 L2 · Qdrant\nEpisodic Summaries"]
            L3["🟡 L3 · Qdrant\nSemantic Knowledge"]
            L4["🟢 L4 · Qdrant\nPermanent Rules"]
        end
    end

    MCP <--> PACK
    PACK <--> Memory
    PACK <--> IDX
```

---

## Context Assembly

Two assembly tools sit above the memory layers:

- **`context_brief`** (session start) — scroll-and-sort digest: identity, last
  session, project knowledge, recent changes, attempts, open tasks.
- **`context_pack`** (any task) — query-driven: every section is selected by
  relevance to the task, packed by a weighted section budgeter with spillover
  reallocation, and rendered to prompt-ready markdown with provenance and
  staleness markers. Consumes the code index when one exists.
  → [context-pack.md](context-pack.md)

Both are also exposed proactively: MCP **resources** (`context://brief`,
`context://projects`) and **prompts** (`session-start`, `pack-context`), using
`DEFAULT_USER_ID` for identity.

---

## Code & Doc Index

Each project can own a persistent index in a sibling collection
`ctx_<slug>__index` — deterministic chunk ids, incremental re-embedding,
hybrid dense + exact-symbol + full-text search. GC, consolidation, and the
relation observer skip `__index` collections by construction.
→ [code-index.md](code-index.md)

---

## 4-Layer Memory Model

| Layer | Storage | Purpose | Token Budget |
|-------|---------|---------|--------------|
| **L1** | Redis | Working memory — ephemeral facts for the current session | ~4k |
| **L2** | Qdrant | Episodic — compressed summaries of past sessions | ~1k |
| **L3** | Qdrant | Semantic knowledge — stable facts, decisions, checkpoints, skills | ~2k |
| **L4** | Qdrant (`ctx_users`) | Procedural — user-global rules, preferences, coding style | ~500 |

> L4 is always stored in the shared `ctx_users` collection — it follows the user across all projects.

---

## Retrieval Pipeline

```mermaid
flowchart LR
    Q([Query]) --> D[Dense Vector\nSearch · Qdrant]
    D --> B[BM25\nRe-rank]
    B --> M[MMR\nDiversity]
    M --> F[Score\nFusion]
    F --> R([Ranked Results])
```

1. **Dense vector search** — embed query, cosine similarity against Qdrant collection
2. **BM25 re-rank** — sparse keyword boost for exact term matches
3. **MMR diversity** — reduce redundancy across results
4. **Score fusion** — combine semantic + recency + importance + user signal scores

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Core | Python 3.12 + asyncio |
| MCP Transport | Anthropic MCP SDK — streamable-HTTP (stateless) / stdio; legacy SSE kept at `/mcp/sse` |
| Vector DB | Qdrant |
| Working Memory | Redis |
| Metadata + Tasks | PostgreSQL + Alembic |
| Embeddings | OpenAI `text-embedding-3-small` or `sentence-transformers` |
| LLM (summarize) | `gpt-4o-mini` |

---

## Project Structure

```
synatyx/
├── src/
│   ├── core/          # retrieve, store, summarize, score, ingest, skill, budget, project
│   ├── parsers/       # docx, pdf, markdown, code, web + registry
│   ├── transports/
│   │   └── mcp/       # MCP stdio server, tools.json, adapters
│   ├── storage/       # Qdrant, Redis, PostgreSQL clients
│   └── models/        # context, session, task, skill, memory layer
├── .claude/
│   ├── CLAUDE.md      # Claude Code rules
│   └── skills/        # Claude Agent Skills
├── .cursor/rules/     # Cursor rules
├── .augment/rules/    # Augment rules
├── docs/              # Documentation
├── alembic/           # Database migrations
├── Makefile
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Multi-Project Isolation

Each project gets its own dedicated Qdrant collection (`ctx_<slug>`). The active project is persisted per user in Redis and survives server restarts.

```
context_set_project(project="my-app")
→ all memory ops route to ctx_my_app
→ context_retrieve returns only my-app memories
```

L4 (user preferences) is always global — stored in `ctx_users` regardless of active project.

