from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from src.models.memory_layer import MemoryLayer
from src.models.task import TaskStatus

logger = logging.getLogger(__name__)

_HTML_PATH = Path(__file__).parent / "dashboard.html"

# Collections that hold memories. ctx_users is the shared L4 collection; every
# other ctx_* collection is one project.
_COLLECTION_PREFIX = "ctx_"


def _is_memory_collection(name: str) -> bool:
    """Memory collections only — code/doc index collections (ctx_<slug>__index)
    are not projects and would show up as phantom entries."""
    from src.core.project import is_index_collection
    return name.startswith(_COLLECTION_PREFIX) and not is_index_collection(name)


def _storages(request: Request) -> tuple[Any, Any] | None:
    qdrant = getattr(request.app.state, "qdrant", None)
    postgres = getattr(request.app.state, "postgres", None)
    if qdrant is None or postgres is None:
        return None
    return qdrant, postgres


async def dashboard_page(_request: Request) -> HTMLResponse:
    """Static dashboard shell. Public — all data comes from the authed API."""
    return HTMLResponse(_HTML_PATH.read_text(encoding="utf-8"))


async def api_overview(request: Request) -> JSONResponse:
    """Per-collection memory counts plus open-task totals."""
    storages = _storages(request)
    if storages is None:
        return JSONResponse({"error": "server not ready"}, status_code=503)
    qdrant, postgres = storages

    names = sorted(
        n for n in await qdrant.get_all_collections() if _is_memory_collection(n)
    )
    collections = []
    for name in names:
        try:
            stats = await qdrant.scoped(name).collection_stats()
        except Exception:
            logger.exception("collection_stats failed for %s", name)
            continue
        collections.append(
            {
                "collection": name,
                "slug": name.removeprefix(_COLLECTION_PREFIX),
                "is_user_global": name == "ctx_users",
                **stats,
            }
        )

    open_tasks = await postgres.task_list_all(status=TaskStatus.PENDING, limit=200)

    totals = {
        "total": sum(c["total"] for c in collections),
        "active": sum(c["active"] for c in collections),
        "deprecated": sum(c["deprecated"] for c in collections),
        "pinned": sum(c["pinned"] for c in collections),
        "by_layer": {
            layer.value: sum(c["by_layer"].get(layer.value, 0) for c in collections)
            for layer in MemoryLayer
        },
        "collections": len(collections),
        "open_tasks": len(open_tasks),
    }

    return JSONResponse({"totals": totals, "collections": collections})


async def api_items(request: Request) -> JSONResponse:
    """Recent items in one collection, newest first. No user filter — admin view."""
    storages = _storages(request)
    if storages is None:
        return JSONResponse({"error": "server not ready"}, status_code=503)
    qdrant, _ = storages

    collection = request.query_params.get("collection", "")
    valid = {n for n in await qdrant.get_all_collections() if _is_memory_collection(n)}
    if collection not in valid:
        return JSONResponse({"error": f"unknown collection {collection!r}"}, status_code=400)

    layer_raw = request.query_params.get("layer") or None
    layer = None
    if layer_raw:
        try:
            layer = MemoryLayer(layer_raw)
        except ValueError:
            return JSONResponse({"error": f"invalid layer {layer_raw!r}"}, status_code=400)

    include_deprecated = request.query_params.get("include_deprecated") == "true"
    user_filter = request.query_params.get("user") or None
    try:
        limit = min(int(request.query_params.get("limit", "50")), 200)
    except ValueError:
        return JSONResponse({"error": "invalid limit"}, status_code=400)

    payloads, _next = await qdrant.scoped(collection).scan_all_items(
        memory_layer=layer,
        include_deprecated=include_deprecated,
        limit=1000,
    )
    if user_filter:
        payloads = [p for p in payloads if p.get("user_id") == user_filter]

    def _created(p: dict[str, Any]) -> str:
        return p.get("created_at") or ""

    payloads.sort(key=_created, reverse=True)

    items = []
    for p in payloads[:limit]:
        meta = p.get("metadata") or {}
        content = p.get("content", "")
        items.append(
            {
                "id": p.get("_id"),
                "content": content[:400],
                "truncated": len(content) > 400,
                "memory_layer": p.get("memory_layer"),
                "importance": p.get("importance"),
                "is_pinned": p.get("is_pinned", False),
                "is_deprecated": p.get("is_deprecated", False),
                "origin": meta.get("origin"),
                "type": meta.get("type") or p.get("type"),
                "checkpoint_name": meta.get("checkpoint_name"),
                "user_id": p.get("user_id"),
                "session_id": p.get("session_id"),
                "project": p.get("project"),
                "created_at": p.get("created_at"),
            }
        )

    return JSONResponse(
        {
            "collection": collection,
            "count": len(items),
            "generated_at": datetime.now(UTC).isoformat(),
            "items": items,
        }
    )


async def api_users(request: Request) -> JSONResponse:
    """Distinct users in one collection with per-user memory counts."""
    storages = _storages(request)
    if storages is None:
        return JSONResponse({"error": "server not ready"}, status_code=503)
    qdrant, _ = storages

    collection = request.query_params.get("collection", "")
    valid = {n for n in await qdrant.get_all_collections() if _is_memory_collection(n)}
    if collection not in valid:
        return JSONResponse({"error": f"unknown collection {collection!r}"}, status_code=400)

    payloads, _next = await qdrant.scoped(collection).scan_all_items(
        include_deprecated=True, limit=1000
    )

    users: dict[str, dict[str, Any]] = {}
    for p in payloads:
        uid = p.get("user_id") or "(unknown)"
        u = users.setdefault(
            uid,
            {
                "user_id": uid,
                "total": 0,
                "active": 0,
                "deprecated": 0,
                "pinned": 0,
                "by_layer": {layer.value: 0 for layer in MemoryLayer},
                "last_created_at": None,
            },
        )
        u["total"] += 1
        if p.get("is_deprecated"):
            u["deprecated"] += 1
        else:
            u["active"] += 1
            layer = p.get("memory_layer")
            if layer in u["by_layer"]:
                u["by_layer"][layer] += 1
            if p.get("is_pinned"):
                u["pinned"] += 1
        created = p.get("created_at")
        if created and (u["last_created_at"] is None or created > u["last_created_at"]):
            u["last_created_at"] = created

    ranked = sorted(users.values(), key=lambda u: u["total"], reverse=True)
    return JSONResponse({"collection": collection, "count": len(ranked), "users": ranked})


async def api_graph(request: Request) -> JSONResponse:
    """Nodes + relation edges for one collection — the memory graph."""
    storages = _storages(request)
    if storages is None:
        return JSONResponse({"error": "server not ready"}, status_code=503)
    qdrant, postgres = storages

    collection = request.query_params.get("collection", "")
    valid = {n for n in await qdrant.get_all_collections() if _is_memory_collection(n)}
    if collection not in valid:
        return JSONResponse({"error": f"unknown collection {collection!r}"}, status_code=400)

    user_filter = request.query_params.get("user") or None
    try:
        limit = min(int(request.query_params.get("limit", "150")), 300)
    except ValueError:
        return JSONResponse({"error": "invalid limit"}, status_code=400)

    payloads, _next = await qdrant.scoped(collection).scan_all_items(
        include_deprecated=True, limit=1000
    )
    if user_filter:
        payloads = [p for p in payloads if p.get("user_id") == user_filter]
    payloads.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    payloads = payloads[:limit]

    node_ids = [p["_id"] for p in payloads if p.get("_id")]
    edges = await postgres.relation_list_all(item_ids=node_ids, limit=500)

    # Hydrate edge endpoints that fall outside the scanned set (e.g. the other
    # arm of a supersedes chain, or an L4 item in ctx_users) so their edges
    # render instead of being dropped.
    known = {p["_id"] for p in payloads}
    scoped = qdrant.scoped(collection)
    for edge in edges:
        for endpoint in (edge.source_item_id, edge.target_item_id):
            if endpoint in known:
                continue
            known.add(endpoint)
            item = await scoped.get_by_id(endpoint)
            if item is not None:
                payloads.append(
                    {
                        "_id": item.id,
                        "user_id": item.user_id,
                        "content": item.content,
                        "memory_layer": item.memory_layer.value,
                        "importance": item.importance,
                        "is_pinned": item.is_pinned,
                        "is_deprecated": item.is_deprecated,
                        "metadata": item.metadata,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    }
                )

    present = {p["_id"] for p in payloads}
    nodes = [
        {
            "id": p["_id"],
            "label": (p.get("content") or "")[:120],
            "memory_layer": p.get("memory_layer"),
            "importance": p.get("importance", 0.5),
            "is_pinned": p.get("is_pinned", False),
            "is_deprecated": p.get("is_deprecated", False),
            "user_id": p.get("user_id"),
            "checkpoint_name": (p.get("metadata") or {}).get("checkpoint_name"),
        }
        for p in payloads
    ]
    edge_list = [
        {
            "source": e.source_item_id,
            "target": e.target_item_id,
            "type": e.relation_type,
        }
        for e in edges
        if e.source_item_id in present and e.target_item_id in present
    ]

    return JSONResponse(
        {
            "collection": collection,
            "nodes": nodes,
            "edges": edge_list,
            "node_count": len(nodes),
            "edge_count": len(edge_list),
        }
    )


async def api_tasks(request: Request) -> JSONResponse:
    """Tasks across all users, optionally filtered by status."""
    storages = _storages(request)
    if storages is None:
        return JSONResponse({"error": "server not ready"}, status_code=503)
    _, postgres = storages

    status_raw = request.query_params.get("status") or None
    status = None
    if status_raw and status_raw != "all":
        try:
            status = TaskStatus(status_raw)
        except ValueError:
            return JSONResponse({"error": f"invalid status {status_raw!r}"}, status_code=400)

    tasks = await postgres.task_list_all(status=status, limit=100)
    return JSONResponse(
        {
            "count": len(tasks),
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "project": t.project,
                    "user_id": t.user_id,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in tasks
            ],
        }
    )
