from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.models.context import ContextItem
from src.models.memory_layer import MemoryLayer
from src.models.relation import MemoryRelation
from src.models.task import Task, TaskPriority, TaskStatus
from src.transports.mcp.dashboard import (
    api_graph,
    api_indexes,
    api_items,
    api_overview,
    api_tasks,
    api_users,
    dashboard_page,
)


class FakeQdrant:
    """Duck-typed stand-in for QdrantStorage covering the dashboard's needs."""

    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self._collections = collections
        self._scope = next(iter(collections), "ctx_default")

    async def get_all_collections(self) -> list[str]:
        return list(self._collections) + ["not_a_ctx_collection"]

    def scoped(self, collection_name: str) -> FakeQdrant:
        clone = FakeQdrant(self._collections)
        clone._scope = collection_name
        return clone

    async def collection_stats(self) -> dict[str, Any]:
        items = self._collections[self._scope]
        active = [i for i in items if not i.get("is_deprecated")]
        return {
            "total": len(items),
            "active": len(active),
            "deprecated": len(items) - len(active),
            "pinned": sum(1 for i in active if i.get("is_pinned")),
            "by_layer": {
                layer: sum(1 for i in active if i.get("memory_layer") == layer)
                for layer in ("L1", "L2", "L3", "L4")
            },
        }

    async def scan_all_items(
        self,
        memory_layer: Any = None,
        include_deprecated: bool = False,
        limit: int = 1000,
        offset: str | None = None,
        with_vectors: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        items = [dict(i) for i in self._collections[self._scope]]
        if not include_deprecated:
            items = [i for i in items if not i.get("is_deprecated")]
        if memory_layer:
            items = [i for i in items if i.get("memory_layer") == memory_layer.value]
        return items[:limit], None

    async def get_by_id(self, item_id: str) -> ContextItem | None:
        for p in self._collections[self._scope]:
            if p.get("_id") == item_id:
                return ContextItem(
                    id=item_id,
                    user_id=p.get("user_id", ""),
                    session_id=p.get("session_id"),
                    content=p.get("content", ""),
                    memory_layer=MemoryLayer(p.get("memory_layer", "L3")),
                    importance=p.get("importance", 0.5),
                    is_pinned=p.get("is_pinned", False),
                    is_deprecated=p.get("is_deprecated", False),
                    metadata=p.get("metadata", {}),
                )
        return None


class FakePostgres:
    def __init__(
        self, tasks: list[Task], relations: list[MemoryRelation] | None = None
    ) -> None:
        self._tasks = tasks
        self._relations = relations or []

    async def task_list_all(
        self, status: TaskStatus | None = None, limit: int = 50
    ) -> list[Task]:
        tasks = self._tasks
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks[:limit]

    async def relation_list_all(
        self, item_ids: list[str], limit: int = 500
    ) -> list[MemoryRelation]:
        ids = set(item_ids)
        return [
            r
            for r in self._relations
            if r.source_item_id in ids or r.target_item_id in ids
        ][:limit]


def _item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_id": "00000000-0000-0000-0000-000000000001",
        "user_id": "u1",
        "session_id": "synatyx",
        "project": "synatyx",
        "content": "Qdrant runs on port 6333",
        "memory_layer": "L3",
        "importance": 0.7,
        "is_pinned": False,
        "is_deprecated": False,
        "metadata": {"origin": "agent-inferred"},
        "created_at": "2026-08-01T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _client(
    collections: dict[str, list[dict[str, Any]]],
    tasks: list[Task],
    relations: list[MemoryRelation] | None = None,
) -> TestClient:
    app = Starlette(
        routes=[
            Route("/dashboard", dashboard_page),
            Route("/dashboard/api/overview", api_overview),
            Route("/dashboard/api/items", api_items),
            Route("/dashboard/api/tasks", api_tasks),
            Route("/dashboard/api/users", api_users),
            Route("/dashboard/api/graph", api_graph),
            Route("/dashboard/api/indexes", api_indexes),
        ]
    )
    app.state.qdrant = FakeQdrant(collections)
    app.state.postgres = FakePostgres(tasks, relations)
    return TestClient(app)


def _sample_client() -> TestClient:
    collections = {
        "ctx_synatyx": [
            _item(),
            _item(
                _id="00000000-0000-0000-0000-000000000002",
                content="old fact",
                is_deprecated=True,
                user_id="u2",
                created_at="2026-07-01T10:00:00+00:00",
            ),
            _item(
                _id="00000000-0000-0000-0000-000000000003",
                content="newer fact",
                memory_layer="L2",
                is_pinned=True,
                created_at="2026-08-02T10:00:00+00:00",
            ),
        ],
        "ctx_users": [_item(memory_layer="L4", project=None, session_id=None)],
    }
    tasks = [
        Task(user_id="u1", title="pending thing", status=TaskStatus.PENDING),
        Task(
            user_id="u1",
            title="done thing",
            status=TaskStatus.DONE,
            priority=TaskPriority.HIGH,
        ),
    ]
    return _client(collections, tasks)


def test_dashboard_page_serves_html() -> None:
    res = _sample_client().get("/dashboard")
    assert res.status_code == 200
    assert "Synatyx" in res.text
    assert res.headers["content-type"].startswith("text/html")


def test_overview_aggregates_collections_and_skips_non_ctx() -> None:
    res = _sample_client().get("/dashboard/api/overview")
    assert res.status_code == 200
    data = res.json()

    names = [c["collection"] for c in data["collections"]]
    assert names == ["ctx_synatyx", "ctx_users"]
    assert data["totals"]["total"] == 4
    assert data["totals"]["active"] == 3
    assert data["totals"]["deprecated"] == 1
    assert data["totals"]["pinned"] == 1
    assert data["totals"]["by_layer"] == {"L1": 0, "L2": 1, "L3": 1, "L4": 1}
    assert data["totals"]["open_tasks"] == 1

    users = next(c for c in data["collections"] if c["collection"] == "ctx_users")
    assert users["is_user_global"] is True
    assert users["slug"] == "users"


def test_items_sorted_newest_first_and_filtered() -> None:
    client = _sample_client()

    res = client.get("/dashboard/api/items", params={"collection": "ctx_synatyx"})
    assert res.status_code == 200
    data = res.json()
    assert [i["content"] for i in data["items"]] == ["newer fact", "Qdrant runs on port 6333"]
    assert data["items"][0]["origin"] == "agent-inferred"

    res = client.get(
        "/dashboard/api/items",
        params={"collection": "ctx_synatyx", "include_deprecated": "true"},
    )
    assert res.json()["count"] == 3

    res = client.get(
        "/dashboard/api/items", params={"collection": "ctx_synatyx", "layer": "L2"}
    )
    assert [i["memory_layer"] for i in res.json()["items"]] == ["L2"]


def test_items_rejects_unknown_collection_and_bad_layer() -> None:
    client = _sample_client()
    assert client.get("/dashboard/api/items", params={"collection": "ctx_nope"}).status_code == 400
    assert (
        client.get(
            "/dashboard/api/items", params={"collection": "ctx_synatyx", "layer": "L9"}
        ).status_code
        == 400
    )


def test_tasks_filtered_by_status() -> None:
    client = _sample_client()

    res = client.get("/dashboard/api/tasks", params={"status": "pending"})
    assert [t["title"] for t in res.json()["tasks"]] == ["pending thing"]

    res = client.get("/dashboard/api/tasks", params={"status": "all"})
    assert res.json()["count"] == 2

    assert client.get("/dashboard/api/tasks", params={"status": "bogus"}).status_code == 400


def _graph_client() -> TestClient:
    collections = {
        "ctx_synatyx": [
            _item(),
            _item(
                _id="00000000-0000-0000-0000-000000000002",
                content="old fact",
                is_deprecated=True,
                user_id="u2",
            ),
            _item(
                _id="00000000-0000-0000-0000-000000000003",
                content="newer fact",
                memory_layer="L2",
                is_pinned=True,
            ),
        ],
    }
    relations = [
        MemoryRelation(
            user_id="u1",
            source_item_id="00000000-0000-0000-0000-000000000003",
            target_item_id="00000000-0000-0000-0000-000000000002",
            relation_type="supersedes",
        ),
        MemoryRelation(
            user_id="u1",
            source_item_id="00000000-0000-0000-0000-000000000001",
            target_item_id="99999999-9999-9999-9999-999999999999",
            relation_type="depends_on",
        ),
    ]
    return _client(collections, [], relations)


def test_users_aggregates_per_user() -> None:
    res = _sample_client().get("/dashboard/api/users", params={"collection": "ctx_synatyx"})
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 2
    u1 = next(u for u in data["users"] if u["user_id"] == "u1")
    assert u1["total"] == 2 and u1["active"] == 2 and u1["pinned"] == 1
    assert u1["by_layer"]["L2"] == 1 and u1["by_layer"]["L3"] == 1
    u2 = next(u for u in data["users"] if u["user_id"] == "u2")
    assert u2["total"] == 1 and u2["deprecated"] == 1

    assert (
        _sample_client()
        .get("/dashboard/api/users", params={"collection": "ctx_nope"})
        .status_code
        == 400
    )


def test_items_user_filter() -> None:
    res = _sample_client().get(
        "/dashboard/api/items",
        params={"collection": "ctx_synatyx", "user": "u2", "include_deprecated": "true"},
    )
    assert [i["content"] for i in res.json()["items"]] == ["old fact"]


def test_graph_returns_nodes_and_edges() -> None:
    res = _graph_client().get("/dashboard/api/graph", params={"collection": "ctx_synatyx"})
    assert res.status_code == 200
    data = res.json()

    assert data["node_count"] == 3
    # The supersedes edge survives (both endpoints present); the edge to the
    # unknown item is dropped after hydration fails to find it.
    assert data["edge_count"] == 1
    edge = data["edges"][0]
    assert edge["type"] == "supersedes"
    assert edge["source"] == "00000000-0000-0000-0000-000000000003"

    deprecated = next(n for n in data["nodes"] if n["is_deprecated"])
    assert deprecated["id"] == "00000000-0000-0000-0000-000000000002"
    pinned = next(n for n in data["nodes"] if n["is_pinned"])
    assert pinned["memory_layer"] == "L2"


def test_graph_user_filter_keeps_hydrated_endpoints() -> None:
    res = _graph_client().get(
        "/dashboard/api/graph", params={"collection": "ctx_synatyx", "user": "u1"}
    )
    data = res.json()
    # u2's deprecated item is filtered out of the scan but hydrated back in
    # because u1's supersedes edge points at it.
    assert data["edge_count"] == 1
    ids = {n["id"] for n in data["nodes"]}
    assert "00000000-0000-0000-0000-000000000002" in ids


def test_endpoints_503_before_lifespan() -> None:
    app = Starlette(routes=[Route("/dashboard/api/overview", api_overview)])
    client = TestClient(app)
    assert client.get("/dashboard/api/overview").status_code == 503


# ── api_indexes ──────────────────────────────────────────────────────────────

def test_indexes_lists_index_collections_only() -> None:
    collections = {
        "ctx_synatyx": [_item()],
        "ctx_myapp__index": [
            {"user_id": "u1", "chunk_index": 0, "chunk_total": 3, "language": "python",
             "path": "src/a.py", "indexed_at": "2026-08-03T10:00:00+00:00"},
            {"user_id": "u1", "chunk_index": 1, "chunk_total": 3, "language": "python",
             "path": "src/a.py", "indexed_at": "2026-08-03T10:00:00+00:00"},
            {"user_id": "u1", "chunk_index": 0, "chunk_total": 2, "language": "md",
             "path": "README.md", "indexed_at": "2026-08-03T11:00:00+00:00"},
        ],
    }
    client = _client(collections, tasks=[])
    res = client.get("/dashboard/api/indexes")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    ix = data["indexes"][0]
    assert ix["project"] == "myapp"
    assert ix["files"] == 2          # chunk-0 records only
    assert ix["chunks"] == 5         # 3 + 2 via chunk_total
    assert ix["by_language"] == {"python": 1, "md": 1}
    assert ix["last_indexed_at"] == "2026-08-03T11:00:00+00:00"
    assert ix["users"] == ["u1"]


def test_overview_still_excludes_index_collections() -> None:
    collections = {
        "ctx_synatyx": [_item()],
        "ctx_myapp__index": [
            {"user_id": "u1", "chunk_index": 0, "chunk_total": 1, "language": "python",
             "path": "a.py", "indexed_at": "2026-08-03T10:00:00+00:00"},
        ],
    }
    client = _client(collections, tasks=[])
    names = [c["collection"] for c in client.get("/dashboard/api/overview").json()["collections"]]
    assert "ctx_myapp__index" not in names
