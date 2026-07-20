from __future__ import annotations

from typing import Any

import pytest

from src.core.relation import RelationService
from src.models.context import ContextItem
from src.models.memory_layer import MemoryLayer
from src.models.relation import DEFAULT_RELATION_TYPE, MemoryRelation

# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------

def _relation(**overrides: Any) -> MemoryRelation:
    base: dict[str, Any] = {
        "user_id": "u1",
        "source_item_id": "aaaa",
        "target_item_id": "bbbb",
    }
    base.update(overrides)
    return MemoryRelation(**base)


def test_relation_defaults() -> None:
    rel = _relation()
    assert rel.relation_type == DEFAULT_RELATION_TYPE
    assert rel.id  # UUID generated
    assert rel.metadata == {}


def test_relation_type_is_normalized() -> None:
    assert _relation(relation_type="  Depends-On ").relation_type == "depends_on"
    assert _relation(relation_type="my custom link").relation_type == "my_custom_link"


def test_relation_type_empty_falls_back_to_default() -> None:
    assert _relation(relation_type="   ").relation_type == DEFAULT_RELATION_TYPE


def test_relation_type_too_long_rejected() -> None:
    with pytest.raises(ValueError):
        _relation(relation_type="x" * 65)


def test_self_link_rejected() -> None:
    with pytest.raises(ValueError):
        _relation(source_item_id="same", target_item_id="same")


# ---------------------------------------------------------------------------
# RelationService against in-memory fakes
# ---------------------------------------------------------------------------

class FakeQdrant:
    def __init__(self, items: dict[str, ContextItem]) -> None:
        self._items = items

    async def get_by_id(self, item_id: str) -> ContextItem | None:
        return self._items.get(item_id)


class FakePostgres:
    def __init__(self) -> None:
        self.relations: list[MemoryRelation] = []
        self.audits: list[tuple[str, str]] = []

    async def relation_add(self, relation: MemoryRelation) -> tuple[MemoryRelation, bool]:
        for existing in self.relations:
            if (
                existing.user_id == relation.user_id
                and existing.source_item_id == relation.source_item_id
                and existing.target_item_id == relation.target_item_id
                and existing.relation_type == relation.relation_type
            ):
                return existing, False
        self.relations.append(relation)
        return relation, True

    async def relation_list(
        self,
        user_id: str,
        item_id: str | None = None,
        item_ids: list[str] | None = None,
        relation_type: str | None = None,
        direction: str = "both",
        limit: int = 200,
    ) -> list[MemoryRelation]:
        ids = set(item_ids or ([item_id] if item_id else []))
        out = []
        for r in self.relations:
            if r.user_id != user_id:
                continue
            if relation_type and r.relation_type != relation_type:
                continue
            if ids:
                if direction == "out" and r.source_item_id not in ids:
                    continue
                if direction == "in" and r.target_item_id not in ids:
                    continue
                if direction == "both" and not ({r.source_item_id, r.target_item_id} & ids):
                    continue
            out.append(r)
        return out[:limit]

    async def relation_delete(self, user_id: str, **kwargs: Any) -> int:
        before = len(self.relations)
        rid = kwargs.get("relation_id")
        src, tgt = kwargs.get("source_item_id"), kwargs.get("target_item_id")
        if not rid and not (src and tgt):
            raise ValueError("Provide relation_id, or both source_item_id and target_item_id")
        self.relations = [
            r for r in self.relations
            if not (
                r.user_id == user_id
                and (r.id == rid if rid else (r.source_item_id == src and r.target_item_id == tgt))
            )
        ]
        return before - len(self.relations)

    async def audit(self, user_id: str, action: str, payload: Any = None) -> None:
        self.audits.append((user_id, action))


def _item(item_id: str, user_id: str = "u1", deprecated: bool = False) -> ContextItem:
    return ContextItem(
        id=item_id,
        user_id=user_id,
        content=f"content of {item_id}",
        memory_layer=MemoryLayer.L3,
        is_deprecated=deprecated,
    )


def _service(items: dict[str, ContextItem], l4_items: dict[str, ContextItem] | None = None):
    pg = FakePostgres()
    svc = RelationService(pg, FakeQdrant(items), FakeQdrant(l4_items or {}))  # type: ignore[arg-type]
    return svc, pg


@pytest.mark.asyncio
async def test_relate_creates_edge_and_dedups() -> None:
    items = {"a": _item("a"), "b": _item("b")}
    svc, pg = _service(items)

    edge, created = await svc.relate("u1", "a", "b", "depends_on")
    assert created is True
    assert edge.relation_type == "depends_on"

    _, created_again = await svc.relate("u1", "a", "b", "depends_on")
    assert created_again is False
    assert len(pg.relations) == 1


@pytest.mark.asyncio
async def test_relate_missing_item_rejected() -> None:
    svc, _ = _service({"a": _item("a")})
    with pytest.raises(ValueError):
        await svc.relate("u1", "a", "missing")


@pytest.mark.asyncio
async def test_relate_enforces_user_isolation() -> None:
    items = {"a": _item("a"), "b": _item("b", user_id="someone-else")}
    svc, _ = _service(items)
    with pytest.raises(PermissionError):
        await svc.relate("u1", "a", "b")


@pytest.mark.asyncio
async def test_relate_spans_l4_collection() -> None:
    svc, pg = _service({"a": _item("a")}, l4_items={"pref": _item("pref")})
    _, created = await svc.relate("u1", "a", "pref")
    assert created is True


@pytest.mark.asyncio
async def test_related_hydrates_deprecated_neighbors() -> None:
    # supersedes chains must remain visible even when the target is deprecated
    items = {"new": _item("new"), "old": _item("old", deprecated=True)}
    svc, _ = _service(items)
    await svc.relate("u1", "new", "old", "supersedes")

    edges, neighbors = await svc.related("u1", "new")
    assert len(edges) == 1
    assert neighbors["old"].is_deprecated is True


@pytest.mark.asyncio
async def test_unrelate_by_pair() -> None:
    items = {"a": _item("a"), "b": _item("b")}
    svc, pg = _service(items)
    await svc.relate("u1", "a", "b")
    deleted = await svc.unrelate("u1", source_id="a", target_id="b")
    assert deleted == 1
    assert pg.relations == []


@pytest.mark.asyncio
async def test_expand_returns_neighbors_with_via_relation() -> None:
    items = {"a": _item("a"), "b": _item("b"), "c": _item("c")}
    svc, _ = _service(items)
    await svc.relate("u1", "a", "b")
    await svc.relate("u1", "c", "a")  # inbound edge also expands

    expanded = await svc.expand("u1", ["a"])
    ids = {e["id"] for e in expanded}
    assert ids == {"b", "c"}
    for e in expanded:
        assert e["via_relation"]["anchor_item_id"] == "a"


@pytest.mark.asyncio
async def test_expand_skips_deprecated_and_already_selected() -> None:
    items = {"a": _item("a"), "b": _item("b"), "old": _item("old", deprecated=True)}
    svc, _ = _service(items)
    await svc.relate("u1", "a", "b")
    await svc.relate("u1", "a", "old")

    expanded = await svc.expand("u1", ["a", "b"])
    assert expanded == []  # b already selected, old is deprecated


@pytest.mark.asyncio
async def test_expand_respects_max_items() -> None:
    items = {str(i): _item(str(i)) for i in range(6)}
    svc, _ = _service(items)
    for i in range(1, 6):
        await svc.relate("u1", "0", str(i))

    expanded = await svc.expand("u1", ["0"], max_items=3)
    assert len(expanded) == 3
