from __future__ import annotations

from typing import Any

import pytest

from src.config import ObserverSettings
from src.core.observer import RelationObserver
from src.models.relation import MemoryRelation

# Orthogonal-ish unit vectors and near-duplicates for similarity control
_V_A = [1.0, 0.0, 0.0]
_V_A2 = [0.98, 0.2, 0.0]  # cosine(A, A2) ≈ 0.98
_V_A3 = [0.95, 0.31, 0.0]  # close to A and A2
_V_B = [0.0, 1.0, 0.0]  # orthogonal to the A family


class FakeQdrant:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self._collections = collections
        self._scope = next(iter(collections), "ctx_default")

    async def get_all_collections(self) -> list[str]:
        return list(self._collections)

    def scoped(self, name: str) -> FakeQdrant:
        clone = FakeQdrant(self._collections)
        clone._scope = name
        return clone

    async def scan_all_items(
        self,
        memory_layer: Any = None,
        include_deprecated: bool = False,
        limit: int = 500,
        offset: str | None = None,
        with_vectors: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        items = [dict(i) for i in self._collections[self._scope]]
        if not include_deprecated:
            items = [i for i in items if not i.get("is_deprecated")]
        return items, None


class FakePostgres:
    def __init__(self, existing: list[MemoryRelation] | None = None) -> None:
        self.relations: list[MemoryRelation] = list(existing or [])

    async def relation_list_all(
        self, item_ids: list[str], limit: int = 500
    ) -> list[MemoryRelation]:
        ids = set(item_ids)
        return [
            r for r in self.relations
            if r.source_item_id in ids or r.target_item_id in ids
        ][:limit]

    async def relation_add(self, relation: MemoryRelation) -> tuple[MemoryRelation, bool]:
        self.relations.append(relation)
        return relation, True


def _item(item_id: str, vector: list[float], **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_id": item_id,
        "user_id": "u1",
        "content": f"memory {item_id}",
        "memory_layer": "L3",
        "importance": 0.7,
        "is_deprecated": False,
        "metadata": {"project": "synatyx"},
        "created_at": f"2026-08-0{item_id[-1]}T10:00:00+00:00",
        "_vector": vector,
    }
    base.update(overrides)
    return base


def _observer(
    collections: dict[str, list[dict[str, Any]]],
    postgres: FakePostgres | None = None,
    **settings: Any,
) -> tuple[RelationObserver, FakePostgres]:
    pg = postgres or FakePostgres()
    cfg = ObserverSettings(**{"enabled": True, **settings})
    return RelationObserver(qdrant=FakeQdrant(collections), postgres=pg, settings=cfg), pg


@pytest.mark.asyncio
async def test_links_similar_pairs_with_provenance() -> None:
    obs, pg = _observer({"ctx_p": [_item("i1", _V_A), _item("i2", _V_A2), _item("i3", _V_B)]})
    stats = await obs.run_once()

    assert stats["edges_created"] == 1
    edge = pg.relations[0]
    assert edge.relation_type == "related_to"
    assert edge.metadata["origin"] == "observer"
    assert edge.metadata["auto"] is True
    assert 0.9 < edge.metadata["score"] <= 1.0
    # newer item (i2) points at the older one (i1)
    assert (edge.source_item_id, edge.target_item_id) == ("i2", "i1")
    assert edge.user_id == "u1"
    assert edge.project == "synatyx"


@pytest.mark.asyncio
async def test_never_links_across_users() -> None:
    obs, pg = _observer(
        {"ctx_p": [_item("i1", _V_A), _item("i2", _V_A2, user_id="u2")]}
    )
    stats = await obs.run_once()
    assert stats["edges_created"] == 0
    assert pg.relations == []


@pytest.mark.asyncio
async def test_skips_already_linked_pairs_any_direction() -> None:
    existing = [
        MemoryRelation(
            user_id="u1", source_item_id="i1", target_item_id="i2",
            relation_type="alternative_to",
        )
    ]
    obs, pg = _observer(
        {"ctx_p": [_item("i1", _V_A), _item("i2", _V_A2)]},
        postgres=FakePostgres(existing),
    )
    stats = await obs.run_once()
    assert stats["edges_created"] == 0
    assert stats["skipped_existing"] == 1
    assert len(pg.relations) == 1  # only the pre-existing edge


@pytest.mark.asyncio
async def test_per_item_cap_counts_only_observer_edges() -> None:
    items = [_item("i1", _V_A), _item("i2", _V_A2), _item("i3", _V_A3)]

    # cap 1: the strongest pair wins, the rest are capped out
    obs, pg = _observer({"ctx_p": items}, max_edges_per_item=1)
    stats = await obs.run_once()
    assert stats["edges_created"] == 1
    assert stats["skipped_capped"] == 2

    # a manual (non-observer) edge on i1 does NOT consume the cap
    manual = MemoryRelation(
        user_id="u1", source_item_id="i1", target_item_id="elsewhere",
        relation_type="depends_on",
    )
    obs, pg = _observer(
        {"ctx_p": [_item("i1", _V_A), _item("i2", _V_A2)]},
        postgres=FakePostgres([manual]),
        max_edges_per_item=1,
    )
    stats = await obs.run_once()
    assert stats["edges_created"] == 1


@pytest.mark.asyncio
async def test_dry_run_writes_nothing() -> None:
    obs, pg = _observer(
        {"ctx_p": [_item("i1", _V_A), _item("i2", _V_A2)]}, dry_run=True
    )
    stats = await obs.run_once()
    assert stats["edges_created"] == 1  # counted so the threshold can be tuned
    assert pg.relations == []


@pytest.mark.asyncio
async def test_max_edges_per_run_stops_the_pass() -> None:
    items = [
        _item("i1", _V_A), _item("i2", _V_A2), _item("i3", _V_A3),
        _item("i4", [0.97, 0.24, 0.0]),
    ]
    obs, pg = _observer({"ctx_p": items}, max_edges_per_run=2)
    stats = await obs.run_once()
    assert stats["edges_created"] == 2
    assert len(pg.relations) == 2


@pytest.mark.asyncio
async def test_disabled_and_skip_rules() -> None:
    obs, pg = _observer(
        {"ctx_p": [_item("i1", _V_A), _item("i2", _V_A2)]}, enabled=False
    )
    cfg = obs._settings
    assert cfg.enabled is False
    assert (await obs.run_once())["edges_created"] == 0

    # skill items and non-ctx collections are ignored
    obs, pg = _observer(
        {
            "ctx_p": [
                _item("i1", _V_A, metadata={"type": "skill"}),
                _item("i2", _V_A2),
            ],
            "not_ctx": [_item("i8", _V_A), _item("i9", _V_A2)],
            "ctx_system": [_item("i6", _V_A), _item("i7", _V_A2)],
        }
    )
    assert (await obs.run_once())["edges_created"] == 0
