from __future__ import annotations

import uuid
from typing import Any

from src.config import ConsolidationSettings
from src.core.consolidate import Consolidator, cosine, greedy_cluster, merge_content
from src.models.context import ContextItem
from src.models.memory_layer import MemoryLayer
from tests.test_relations import FakePostgres


def _payload(
    content: str,
    vector: list[float],
    user_id: str = "u1",
    layer: str = "L2",
    **overrides: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_id": str(uuid.uuid4()),
        "_vector": vector,
        "user_id": user_id,
        "session_id": "proj",
        "content": content,
        "memory_layer": layer,
        "importance": 0.5,
        "is_pinned": False,
        "is_deprecated": False,
        "metadata": {},
        "created_at": "2026-07-20T10:00:00+00:00",
    }
    base.update(overrides)
    return base


class FakeQdrantCons:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.upserted: list[ContextItem] = []
        self.deprecated: list[str] = []
        self.collection_name = "ctx_test"

    async def get_all_collections(self) -> list[str]:
        return ["ctx_test", "ctx_users", "other_collection"]

    def scoped(self, name: str):  # noqa: ANN201 — mirrors QdrantStorage API
        return self

    async def scan_all_items(
        self,
        memory_layer: MemoryLayer | None = None,
        include_deprecated: bool = False,
        limit: int = 1000,
        offset: str | None = None,
        with_vectors: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        out = [
            p for p in self.payloads
            if (memory_layer is None or p["memory_layer"] == memory_layer.value)
            and not p["is_deprecated"]
        ]
        return out, None

    async def upsert(self, item: ContextItem) -> str:
        self.upserted.append(item)
        return item.id

    async def deprecate(self, item_id: str, reason: str | None = None) -> None:
        self.deprecated.append(item_id)


def _run(payloads: list[dict[str, Any]], **settings: Any):
    qdrant = FakeQdrantCons(payloads)
    postgres = FakePostgres()
    consolidator = Consolidator(
        qdrant,  # type: ignore[arg-type]
        postgres,  # type: ignore[arg-type]
        ConsolidationSettings(**settings),
    )
    return consolidator, qdrant, postgres


# ── primitives ───────────────────────────────────────────────────────────────

def test_cosine() -> None:
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 0]) == 0.0


def test_greedy_cluster_groups_similar() -> None:
    a = _payload("a", [1.0, 0.0])
    b = _payload("b", [0.99, 0.05])
    c = _payload("c", [0.0, 1.0])
    clusters = greedy_cluster([a, b, c], threshold=0.9)
    sizes = sorted(len(cl) for cl in clusters)
    assert sizes == [1, 2]


def test_merge_content_newest_first() -> None:
    old = _payload("old note", [1, 0], created_at="2026-07-01T00:00:00+00:00")
    new = _payload("new note", [1, 0], created_at="2026-07-20T00:00:00+00:00")
    merged = merge_content([old, new])
    assert merged.startswith("[Consolidated from 2 episodic memories]")
    assert merged.index("new note") < merged.index("old note")


# ── full pass ────────────────────────────────────────────────────────────────

async def test_cluster_merged_into_l3_with_supersedes() -> None:
    vec = [1.0, 0.0, 0.0]
    payloads = [
        _payload("qdrant runs on 6333", vec),
        _payload("qdrant port is 6333", [0.99, 0.05, 0.0], importance=0.8),
        _payload("we use qdrant at 6333", [0.98, 0.08, 0.0]),
        _payload("unrelated deploy note", [0.0, 1.0, 0.0]),
    ]
    consolidator, qdrant, postgres = _run(payloads, min_cluster_size=3)
    stats = await consolidator.run_once()

    assert stats["merged_clusters"] == 1
    assert stats["items_consolidated"] == 3
    assert len(qdrant.upserted) == 1
    merged = qdrant.upserted[0]
    assert merged.memory_layer == MemoryLayer.L3
    assert merged.metadata["type"] == "consolidated"
    assert merged.importance == 0.8  # max of the cluster
    assert len(merged.metadata["consolidated_from"]) == 3
    # originals deprecated + supersedes edges from the merged item
    assert len(qdrant.deprecated) == 3
    assert len(postgres.relations) == 3
    assert all(e.relation_type == "supersedes" for e in postgres.relations)
    assert all(e.source_item_id == merged.id for e in postgres.relations)


async def test_small_clusters_left_alone() -> None:
    payloads = [
        _payload("a", [1.0, 0.0]),
        _payload("b", [0.99, 0.05]),
    ]
    consolidator, qdrant, _ = _run(payloads, min_cluster_size=3)
    stats = await consolidator.run_once()
    assert stats["merged_clusters"] == 0
    assert qdrant.upserted == [] and qdrant.deprecated == []


async def test_attempts_and_pinned_never_merged() -> None:
    vec = [1.0, 0.0]
    payloads = [
        _payload("attempt record", vec, metadata={"type": "attempt"}),
        _payload("pinned note", vec, is_pinned=True),
        _payload("normal 1", vec),
        _payload("normal 2", vec),
    ]
    consolidator, qdrant, _ = _run(payloads, min_cluster_size=3)
    stats = await consolidator.run_once()
    # only the two normals cluster — below min size, so nothing merges
    assert stats["merged_clusters"] == 0
    assert qdrant.deprecated == []


async def test_l3_items_not_touched() -> None:
    vec = [1.0, 0.0]
    payloads = [_payload(f"l3 fact {i}", vec, layer="L3") for i in range(4)]
    consolidator, qdrant, _ = _run(payloads, min_cluster_size=3)
    stats = await consolidator.run_once()
    assert stats["merged_clusters"] == 0


async def test_users_of_different_ids_not_mixed() -> None:
    vec = [1.0, 0.0]
    payloads = [
        _payload("u1 a", vec), _payload("u1 b", vec),
        _payload("u2 a", vec, user_id="u2"),
    ]
    consolidator, qdrant, _ = _run(payloads, min_cluster_size=3)
    stats = await consolidator.run_once()
    assert stats["merged_clusters"] == 0  # 2 + 1, never 3 across users


async def test_max_merges_per_run_caps_the_pass() -> None:
    def cluster_at(direction: list[float], label: str) -> list[dict[str, Any]]:
        return [_payload(f"{label} {i}", direction) for i in range(3)]

    payloads = cluster_at([1.0, 0.0, 0.0], "x") + cluster_at([0.0, 1.0, 0.0], "y")
    consolidator, qdrant, _ = _run(payloads, min_cluster_size=3, max_merges_per_run=1)
    stats = await consolidator.run_once()
    assert stats["merged_clusters"] == 1
    assert len(qdrant.deprecated) == 3


async def test_disabled_is_noop() -> None:
    payloads = [_payload(f"n{i}", [1.0, 0.0]) for i in range(5)]
    consolidator, qdrant, _ = _run(payloads, enabled=False)
    stats = await consolidator.run_once()
    assert stats == {"merged_clusters": 0, "items_consolidated": 0, "skipped_small": 0}
    assert qdrant.upserted == []
