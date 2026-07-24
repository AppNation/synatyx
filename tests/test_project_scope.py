"""Tests for project scoping: payload stamping at upsert and explicit-project
routing that bypasses the shared active-project pointer."""
from __future__ import annotations

from typing import Any

import pytest

from src.core.project import ProjectManager, slugify
from src.models.context import ContextItem
from src.models.memory_layer import MemoryLayer
from src.storage.qdrant import QdrantStorage

# ---------------------------------------------------------------------------
# project_slug property
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("collection", "expected"),
    [
        ("ctx_synatyx", "synatyx"),
        ("ctx_an_subscription_system", "an_subscription_system"),
        ("ctx_users", None),      # shared L4 collection — no project scope
        ("ctx_default", None),    # fallback when no project is set
        ("something_else", None),  # non-ctx collection
    ],
)
def test_project_slug_derivation(collection: str, expected: str | None) -> None:
    storage = QdrantStorage(host="localhost", port=6333, collection_name=collection)
    assert storage.project_slug == expected


# ---------------------------------------------------------------------------
# upsert stamps the project payload
# ---------------------------------------------------------------------------

class RecordingClient:
    """Captures upsert calls in place of AsyncQdrantClient."""

    def __init__(self) -> None:
        self.points: list[Any] = []

    async def upsert(self, collection_name: str, points: list[Any]) -> None:
        self.points.extend(points)


def _item(metadata: dict[str, Any] | None = None) -> ContextItem:
    return ContextItem(
        user_id="u1",
        content="fact",
        memory_layer=MemoryLayer.L3,
        embedding=[0.1] * 1536,
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_upsert_stamps_project_from_collection() -> None:
    storage = QdrantStorage(host="localhost", port=6333, collection_name="ctx_myproj")
    client = RecordingClient()
    storage._client = client  # type: ignore[assignment]

    await storage.upsert(_item())

    assert client.points[0].payload["project"] == "myproj"


@pytest.mark.asyncio
async def test_upsert_metadata_project_wins_over_collection() -> None:
    storage = QdrantStorage(host="localhost", port=6333, collection_name="ctx_myproj")
    client = RecordingClient()
    storage._client = client  # type: ignore[assignment]

    await storage.upsert(_item(metadata={"project": "explicit"}))

    assert client.points[0].payload["project"] == "explicit"


@pytest.mark.asyncio
async def test_upsert_shared_collection_gets_no_project() -> None:
    storage = QdrantStorage(host="localhost", port=6333, collection_name="ctx_users")
    client = RecordingClient()
    storage._client = client  # type: ignore[assignment]

    await storage.upsert(_item())

    assert client.points[0].payload["project"] is None


# ---------------------------------------------------------------------------
# explicit-project routing bypasses the active-project pointer
# ---------------------------------------------------------------------------

class FakeRedis:
    def __init__(self) -> None:
        self.pointer: str | None = "someone_elses_project"
        self.set_calls: list[tuple[str, str]] = []

    async def project_get(self, user_id: str) -> str | None:
        return self.pointer

    async def project_set(self, user_id: str, slug: str) -> None:
        self.set_calls.append((user_id, slug))
        self.pointer = slug


@pytest.mark.asyncio
async def test_get_storage_for_ignores_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_init(self: QdrantStorage) -> None:
        return None

    monkeypatch.setattr(QdrantStorage, "init_collection", no_init)

    redis = FakeRedis()
    default = QdrantStorage(host="localhost", port=6333, collection_name="ctx_default")
    pm = ProjectManager(redis, default)  # type: ignore[arg-type]

    storage = await pm.get_storage_for("My Project")

    assert storage.collection_name == "ctx_my_project"
    # The shared pointer is untouched — other sessions keep their routing.
    assert redis.set_calls == []
    assert redis.pointer == "someone_elses_project"


@pytest.mark.asyncio
async def test_get_storage_for_slugifies_like_set_project() -> None:
    assert slugify("My Project") == "my_project"
    assert slugify("taty-v2") == "taty_v2"
