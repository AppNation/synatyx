# Synatyx Indexer — VS Code / Cursor extension

Keeps the open workspace pushed into a [Synatyx](https://github.com/tanerincode/synatyx)
code/doc index (`ctx_<project>__index`), so AI agents connected to the same
server get exact-symbol code search and `context_pack` code sections — without
anyone running indexing commands.

Works in **VS Code** and **Cursor** (Cursor installs VS Code extensions).

## What it does

- **On startup**: full hash diff against the server, uploads only what changed
- **On save**: debounced re-push (20 s) — the index is fresh moments after you edit
- **Status bar**: `Synatyx ✓` with last-sync details; click for status
- Uses the same `/index/diff` + `/index/files` protocol as
  `scripts/index_project.py` — idempotent, an unchanged repo costs one request
- Auth key lives in the OS keychain (VS Code SecretStorage), never in settings files

## Live call monitor

The **Synatyx Calls** panel (bottom panel area, next to Terminal/Output) shows
every MCP tool call your agents make against the server, live:

- Calls stream in over the server's `/events/calls` SSE feed, filtered to the
  workspace's project slug, grouped into session blocks with per-session
  call/token/error totals
- Each call row shows time, tool, duration, tokens, and ok/error; expand it
  for a parsed summary (retrieve item counts + top scores, stored item ids,
  `auto_linked`/`suggestions`, index-search hits, empty-retrieve diagnostics)
- Click a call to open the full event (arguments + response) as JSON
- On connect the panel backfills the last `synatyx.monitorReplay` calls
  (default 50) from the server's replay buffer; it reconnects with backoff
- Disable with `synatyx.monitorEnabled: false`. Server side, payload capture
  is controlled by `EVENTS_CAPTURE_PAYLOADS` / `EVENTS_MAX_PAYLOAD_BYTES`
  (metadata-only feed when off) and the whole feed by `EVENTS_ENABLED`

## Setup (per developer, once)

1. Install the extension (`.vsix` or marketplace, see below)
2. `Cmd/Ctrl+Shift+P` → **Synatyx: Set Auth Key** → paste the team key
3. Done. Settings (`synatyx.*`) let you override the server URL, user id,
   project slug, and the on-save/on-startup behavior.

## Commands

| Command | Action |
|---|---|
| `Synatyx: Index Project Now` | Force a diff + push |
| `Synatyx: Set Auth Key` | Store the key in the OS keychain |
| `Synatyx: Show Index Status` | Last sync summary |
| `Synatyx: Clear Call Monitor` | Empty the calls panel |
| `Synatyx: Reconnect Call Monitor` | Drop and re-open the SSE stream |

## Packaging & distribution

```bash
cd extensions/vscode
npx --yes @vscode/vsce package --no-dependencies   # → synatyx-indexer-0.3.0.vsix
```

- **Internal**: share the `.vsix`; devs install via "Extensions: Install from VSIX"
- **Cursor's marketplace**: publish to [Open VSX](https://open-vsx.org) —
  `npx ovsx publish synatyx-indexer-0.2.0.vsix -p <token>`
- **VS Code Marketplace**: `npx vsce publish -p <azure-devops-token>`
