// Synatyx Indexer — keeps the workspace pushed into a Synatyx code/doc index.
// Plain JS, zero dependencies: talks to the server's /index/diff + /index/files
// endpoints (same protocol as scripts/index_project.py).
"use strict";

const vscode = require("vscode");
const crypto = require("crypto");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// Mirror of the server-side filters (src/core/index.py) — keep in sync.
const EXTENSIONS = new Set([
  ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cpp", ".c",
  ".rb", ".php", ".swift", ".kt", ".yml", ".yaml", ".toml", ".json",
  ".md", ".mdx", ".markdown",
]);
const EXCLUDED_DIRS = new Set([
  ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
  "dist", "build", "target", ".next", ".mypy_cache", ".pytest_cache",
  ".ruff_cache", ".idea", ".vscode",
]);
const EXCLUDED_FILES = new Set(["uv.lock", "package-lock.json", "yarn.lock", "poetry.lock"]);
const MAX_FILE_BYTES = 200_000;
const UPLOAD_BATCH_FILES = 16;
const UPLOAD_BATCH_BYTES = 800_000;
const SAVE_DEBOUNCE_MS = 20_000;
const STARTUP_DELAY_MS = 8_000;

const KEY_SECRET = "synatyx.authKey";

let statusBar;
let output;
let secrets;
let saveTimer = null;
let running = false;
let lastSummary = "not indexed yet";

function cfg() {
  const c = vscode.workspace.getConfiguration("synatyx");
  const folder = vscode.workspace.workspaceFolders?.[0];
  return {
    url: (c.get("url") || "").replace(/\/+$/, ""),
    userId: c.get("userId") || os.userInfo().username,
    project: c.get("project") || (folder ? folder.name : ""),
    indexOnSave: c.get("indexOnSave"),
    indexOnStartup: c.get("indexOnStartup"),
    root: folder ? folder.uri.fsPath : null,
  };
}

function setStatus(text, tooltip) {
  statusBar.text = text;
  statusBar.tooltip = tooltip || "Synatyx Indexer — click for status";
  statusBar.show();
}

function listFiles(root) {
  try {
    const out = cp.execFileSync(
      "git", ["-C", root, "ls-files", "--cached", "--others", "--exclude-standard"],
      { encoding: "utf-8", timeout: 30_000, maxBuffer: 32 * 1024 * 1024 },
    );
    return out.split("\n").filter(Boolean).map((line) => path.join(root, line));
  } catch {
    const found = [];
    const walk = (dir) => {
      let entries;
      try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
      for (const e of entries) {
        if (e.isDirectory()) {
          if (!EXCLUDED_DIRS.has(e.name) && !e.name.startsWith(".")) walk(path.join(dir, e.name));
        } else if (e.isFile()) {
          found.push(path.join(dir, e.name));
        }
      }
    };
    walk(root);
    return found;
  }
}

function indexable(file) {
  if (!EXTENSIONS.has(path.extname(file).toLowerCase())) return false;
  if (EXCLUDED_FILES.has(path.basename(file))) return false;
  try {
    const st = fs.statSync(file);
    if (!st.isFile() || st.size > MAX_FILE_BYTES) return false;
    const fd = fs.openSync(file, "r");
    const buf = Buffer.alloc(1024);
    const n = fs.readSync(fd, buf, 0, 1024, 0);
    fs.closeSync(fd);
    return !buf.subarray(0, n).includes(0);
  } catch {
    return false;
  }
}

async function post(base, key, route, payload) {
  const resp = await fetch(base + route, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Auth-Key": key },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error(`${route} → HTTP ${resp.status}`);
  return resp.json();
}

async function runIndex(trigger) {
  if (running) return;
  const { url, userId, project, root } = cfg();
  if (!root || !url || !project) return;
  const key = await secrets.get(KEY_SECRET);
  if (!key) {
    setStatus("$(database) Synatyx: no key", "Run “Synatyx: Set Auth Key” to enable indexing");
    return;
  }

  running = true;
  setStatus("$(sync~spin) Synatyx: indexing…");
  try {
    const manifest = {};
    for (const f of listFiles(root)) {
      if (!indexable(f)) continue;
      const rel = path.relative(root, f);
      if (rel.startsWith("..")) continue;
      manifest[rel.split(path.sep).join("/")] =
        crypto.createHash("sha256").update(fs.readFileSync(f)).digest("hex").slice(0, 16);
    }

    const diff = await post(url, key, "/index/diff", { user_id: userId, project, files: manifest });
    const changed = diff.changed || [];
    const removed = diff.removed || [];
    output.appendLine(
      `[${new Date().toISOString()}] ${trigger}: ${Object.keys(manifest).length} files — ` +
      `${changed.length} to upload, ${removed.length} to prune, ${diff.unchanged} unchanged`,
    );

    let indexed = 0, chunks = 0;
    let batch = [], batchBytes = 0, prune = removed;
    const flush = async () => {
      if (!batch.length && !prune.length) return;
      const res = await post(url, key, "/index/files", {
        user_id: userId, project, files: batch, prune,
      });
      indexed += res.files_indexed || 0;
      chunks += res.chunks_upserted || 0;
      batch = []; batchBytes = 0; prune = [];
    };
    for (const rel of changed) {
      let content;
      try { content = fs.readFileSync(path.join(root, rel), "utf-8"); } catch { continue; }
      batch.push({ path: rel, content });
      batchBytes += content.length;
      if (batch.length >= UPLOAD_BATCH_FILES || batchBytes >= UPLOAD_BATCH_BYTES) await flush();
    }
    await flush();

    lastSummary = `${Object.keys(manifest).length} files tracked · ` +
      `${indexed} pushed, ${chunks} chunks (${new Date().toLocaleTimeString()})`;
    setStatus("$(database) Synatyx ✓", lastSummary);
  } catch (err) {
    lastSummary = `last run failed: ${err.message}`;
    output.appendLine(`[synatyx] ${err.message}`);
    setStatus("$(database) Synatyx ⚠", lastSummary);
  } finally {
    running = false;
  }
}

// ---------------------------------------------------------------------------
// Live call monitor — subscribes to the server's /events/calls SSE feed and
// renders every MCP tool call (grouped by session) in a "Synatyx Calls"
// panel view. Zero dependencies: SSE is parsed off a streamed fetch body.
// ---------------------------------------------------------------------------

const MONITOR_MAX_EVENTS = 500;
const MONITOR_RECONNECT_MIN_MS = 2_000;
const MONITOR_RECONNECT_MAX_MS = 30_000;

let monitor = null;

function safeParse(text) {
  if (typeof text !== "string") return text;
  try { return JSON.parse(text); } catch { return text; }
}

function fmtTokens(t) {
  const total = (t?.input || 0) + (t?.output || 0) + (t?.embedding || 0);
  return total >= 1000 ? `~${(total / 1000).toFixed(1)}k tok` : `~${total} tok`;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

// Per-tool response summaries — the "parsed view" in the tree.
function describeCall(event) {
  const args = safeParse(event.arguments) || {};
  const resp = safeParse(event.response) || {};
  const lines = [];
  if (typeof args === "object") {
    if (args.query) lines.push(`query: ${String(args.query).slice(0, 120)}`);
    if (args.content) lines.push(`content: ${String(args.content).slice(0, 120)}`);
    if (args.memory_layer) lines.push(`layer: ${args.memory_layer}`);
  }
  if (typeof resp !== "object") {
    lines.push("response: (truncated — open JSON for raw)");
    return lines;
  }
  if (event.error) {
    lines.push(`error: ${String(resp.error || "unknown").slice(0, 160)}`);
    return lines;
  }
  if (Array.isArray(resp.context_items)) {
    const n = resp.context_items.length;
    if (n === 0) {
      lines.push("items: 0 — EMPTY RETRIEVE");
      if (resp.diagnostics) lines.push(`diagnostics: ${JSON.stringify(resp.diagnostics).slice(0, 160)}`);
    } else {
      const top = resp.context_items[0];
      lines.push(`items: ${n} (top ${top.memory_layer ?? "?"} score ${Number(top.score ?? 0).toFixed(2)})`);
      const stale = resp.context_items.filter((i) => i.possibly_stale).length;
      if (stale) lines.push(`possibly_stale: ${stale} item(s)`);
    }
  }
  if (resp.item_id) lines.push(`stored: ${resp.item_id}`);
  if (resp.auto_linked?.length) lines.push(`auto_linked: ${resp.auto_linked.length}`);
  if (resp.suggestions?.length) lines.push(`suggestions: ${resp.suggestions.length} same-purpose match(es)`);
  if (Array.isArray(resp.hits)) {
    lines.push(`hits: ${resp.hits.length}`);
    for (const h of resp.hits.slice(0, 3)) {
      lines.push(`  ${h.path ?? "?"}${h.start_line ? `:${h.start_line}` : ""} — ${h.symbol ?? ""}`);
    }
  }
  if (resp.rendered) lines.push(`rendered: ${resp.rendered.length} chars`);
  if (resp.token_estimate) lines.push(`token_estimate: ${resp.token_estimate}`);
  if (!lines.length) {
    const keys = Object.keys(resp).slice(0, 6).join(", ");
    lines.push(`response keys: ${keys || "(empty)"}`);
  }
  return lines;
}

class CallsProvider {
  constructor() {
    this._emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._emitter.event;
    this.events = [];       // newest first
    this.connected = false;
  }

  push(event) {
    this.events.unshift(event);
    if (this.events.length > MONITOR_MAX_EVENTS) this.events.pop();
    this._emitter.fire();
  }

  clear() {
    this.events = [];
    this._emitter.fire();
  }

  setConnected(state) {
    this.connected = state;
    this._emitter.fire();
  }

  _sessions() {
    const order = [];
    const byKey = new Map();
    for (const e of this.events) {
      const key = e.session_id || e.project || "(no session)";
      if (!byKey.has(key)) { byKey.set(key, []); order.push(key); }
      byKey.get(key).push(e);
    }
    return order.map((key) => ({ key, calls: byKey.get(key) }));
  }

  getTreeItem(el) { return el; }

  getChildren(el) {
    if (!el) {
      if (!this.events.length) {
        const item = new vscode.TreeItem(
          this.connected ? "Connected — waiting for calls…" : "Not connected",
        );
        item.iconPath = new vscode.ThemeIcon(this.connected ? "pulse" : "debug-disconnect");
        return [item];
      }
      return this._sessions().map((s) => {
        const tok = s.calls.reduce(
          (sum, e) => sum + (e.tokens?.input || 0) + (e.tokens?.output || 0) + (e.tokens?.embedding || 0), 0);
        const errors = s.calls.filter((e) => e.error).length;
        const item = new vscode.TreeItem(s.key, vscode.TreeItemCollapsibleState.Expanded);
        item.description =
          `${s.calls.length} calls · ${fmtTokens({ input: tok })}` + (errors ? ` · ${errors} error(s)` : "");
        item.iconPath = new vscode.ThemeIcon(errors ? "warning" : "comment-discussion");
        item.contextValue = "session";
        item.id = `session:${s.key}`;
        return item;
      });
    }
    if (el.contextValue === "session") {
      const key = el.id.slice("session:".length);
      const session = this._sessions().find((s) => s.key === key);
      return (session?.calls || []).map((e) => {
        const item = new vscode.TreeItem(
          `${fmtTime(e.ts)}  ${e.tool}`, vscode.TreeItemCollapsibleState.Collapsed);
        item.description = `${e.duration_ms}ms · ${fmtTokens(e.tokens)}`;
        item.iconPath = new vscode.ThemeIcon(
          e.error ? "error" : "pass",
          e.error ? new vscode.ThemeColor("errorForeground") : undefined);
        item.contextValue = "call";
        item.id = `call:${e.seq}:${e.ts}`;
        item.command = { command: "synatyx.openCall", title: "Open call JSON", arguments: [e] };
        return item;
      });
    }
    if (el.contextValue === "call") {
      const seq = Number(el.id.split(":")[1]);
      const event = this.events.find((e) => e.seq === seq);
      if (!event) return [];
      return describeCall(event).map((line) => {
        const item = new vscode.TreeItem(line);
        item.iconPath = new vscode.ThemeIcon("circle-small");
        return item;
      });
    }
    return [];
  }
}

class CallMonitor {
  constructor(context, provider) {
    this.context = context;
    this.provider = provider;
    this.abort = null;
    this.backoff = MONITOR_RECONNECT_MIN_MS;
    this.stopped = false;
    this.output = vscode.window.createOutputChannel("Synatyx Calls");
    context.subscriptions.push(this.output);
  }

  log(msg) { this.output.appendLine(`[${new Date().toISOString()}] ${msg}`); }

  async start() {
    this.stopped = false;
    this.loop();
  }

  stop() {
    this.stopped = true;
    this.abort?.abort();
    this.provider.setConnected(false);
  }

  async loop() {
    while (!this.stopped) {
      const { url, project } = cfg();
      const c = vscode.workspace.getConfiguration("synatyx");
      if (!c.get("monitorEnabled") || !url) { this.provider.setConnected(false); return; }
      const key = await secrets.get(KEY_SECRET);
      if (!key) {
        this.provider.setConnected(false);
        this.log("no auth key — run “Synatyx: Set Auth Key”");
        return;
      }
      try {
        await this.streamOnce(url, key, project, c.get("monitorReplay") ?? 50);
        this.backoff = MONITOR_RECONNECT_MIN_MS; // clean close → quick retry
      } catch (err) {
        if (this.stopped) return;
        this.log(`stream error: ${err.message} — retrying in ${this.backoff / 1000}s`);
      }
      this.provider.setConnected(false);
      if (this.stopped) return;
      await new Promise((r) => setTimeout(r, this.backoff));
      this.backoff = Math.min(this.backoff * 2, MONITOR_RECONNECT_MAX_MS);
    }
  }

  async streamOnce(base, key, project, replay) {
    this.abort = new AbortController();
    const params = new URLSearchParams({ replay: String(replay) });
    if (project) params.set("project", project);
    const resp = await fetch(`${base}/events/calls?${params}`, {
      headers: { "X-Auth-Key": key, Accept: "text/event-stream" },
      signal: this.abort.signal,
    });
    if (!resp.ok) throw new Error(`/events/calls → HTTP ${resp.status}`);
    this.provider.setConnected(true);
    this.backoff = MONITOR_RECONNECT_MIN_MS;
    this.log(`connected (project=${project || "all"})`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data: ")) continue; // ": ping" keep-alives
          try { this.provider.push(JSON.parse(line.slice(6))); } catch { /* skip bad frame */ }
        }
      }
    }
  }
}

function activateMonitor(context) {
  const provider = new CallsProvider();
  monitor = new CallMonitor(context, provider);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("synatyx.calls", provider),
    vscode.commands.registerCommand("synatyx.callsClear", () => provider.clear()),
    vscode.commands.registerCommand("synatyx.callsReconnect", () => {
      monitor.stop();
      monitor.start();
    }),
    vscode.commands.registerCommand("synatyx.openCall", async (event) => {
      const pretty = {
        ...event,
        arguments: safeParse(event.arguments),
        response: safeParse(event.response),
        ts_iso: new Date(event.ts * 1000).toISOString(),
      };
      const doc = await vscode.workspace.openTextDocument({
        content: JSON.stringify(pretty, null, 2), language: "json",
      });
      await vscode.window.showTextDocument(doc, { preview: true });
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("synatyx.monitorEnabled") || e.affectsConfiguration("synatyx.url")) {
        monitor.stop();
        monitor.start();
      }
    }),
  );
  monitor.start();
}

function activate(context) {
  secrets = context.secrets;
  output = vscode.window.createOutputChannel("Synatyx Indexer");
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 90);
  statusBar.command = "synatyx.status";
  setStatus("$(database) Synatyx");

  context.subscriptions.push(
    statusBar,
    output,
    vscode.commands.registerCommand("synatyx.indexNow", () => runIndex("manual")),
    vscode.commands.registerCommand("synatyx.setKey", async () => {
      const key = await vscode.window.showInputBox({
        prompt: "Synatyx auth key (stored in the OS keychain via VS Code SecretStorage)",
        password: true, ignoreFocusOut: true,
      });
      if (key) {
        await secrets.store(KEY_SECRET, key.trim());
        vscode.window.showInformationMessage("Synatyx key saved.");
        runIndex("key-set");
        monitor?.stop();
        monitor?.start();
      }
    }),
    vscode.commands.registerCommand("synatyx.status", () => {
      const { url, project, userId } = cfg();
      vscode.window.showInformationMessage(
        `Synatyx — ${project} @ ${url} (user ${userId}): ${lastSummary}`,
      );
    }),
    vscode.workspace.onDidSaveTextDocument(() => {
      if (!cfg().indexOnSave) return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => runIndex("on-save"), SAVE_DEBOUNCE_MS);
    }),
  );

  activateMonitor(context);

  if (cfg().indexOnStartup) {
    setTimeout(() => runIndex("startup"), STARTUP_DELAY_MS);
  }
}

function deactivate() {
  clearTimeout(saveTimer);
  monitor?.stop();
}

module.exports = { activate, deactivate };
