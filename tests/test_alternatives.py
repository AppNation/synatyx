from __future__ import annotations

from typing import Any

import pytest

from src.config import settings
from src.core.alternatives import AlternativesService
from src.core.relation import RelationService
from src.models.context import ContextItem, ScoredContextItem
from src.models.memory_layer import MemoryLayer
from src.models.relation import MemoryRelation
from tests.test_relations import FakePostgres  # reuse the relation fake


class FakeQdrantAlt:
    """Fake storage with controllable similarity scores per item pair."""

    def __init__(
        self,
        items: dict[str, ContextItem],
        similarities: dict[str, float] | None = None,
    ) -> None:
        self._items = items
        # similarities: item_id -> score returned when searching from any vector
        self._similarities = similarities or {}

    async def get_by_id(self, item_id: str) -> ContextItem | None:
        return self._items.get(item_id)

    async def get_vector(self, item_id: str) -> list[float] | None:
        if item_id in self._items:
            return [1.0] * 4
        return None

    async def search(
        self,
        query_vector: list[float],
        user_id: str,
        top_k: int = 10,
        score_threshold: float = 0.0,
        **kwargs: Any,
    ) -> list[ScoredContextItem]:
        hits = []
        for item_id, score in sorted(
            self._similarities.items(), key=lambda kv: kv[1], reverse=True
        ):
            item = self._items.get(item_id)
            if item is None or item.user_id != user_id or item.is_deprecated:
                continue
            if score < score_threshold:
                continue
            hits.append(
                ScoredContextItem(**item.model_dump(), semantic_score=score)
            )
        return hits[:top_k]


class EmptyQdrant(FakeQdrantAlt):
    def __init__(self) -> None:
        super().__init__({}, {})


def _item(item_id: str, content: str, user_id: str = "u1", **overrides: Any) -> ContextItem:
    base: dict[str, Any] = {
        "id": item_id,
        "user_id": user_id,
        "content": content,
        "memory_layer": MemoryLayer.L3,
    }
    base.update(overrides)
    return ContextItem(**base)


X = "aaaa1111-0000-0000-0000-000000000000"
Y = "bbbb2222-0000-0000-0000-000000000000"
Z = "cccc3333-0000-0000-0000-000000000000"


def _service(
    items: dict[str, ContextItem], similarities: dict[str, float]
) -> tuple[AlternativesService, FakePostgres]:
    qdrant = FakeQdrantAlt(items, similarities)
    l4 = EmptyQdrant()
    postgres = FakePostgres()
    relations = RelationService(postgres, qdrant, l4)  # type: ignore[arg-type]
    svc = AlternativesService(qdrant, l4, postgres, relations)  # type: ignore[arg-type]
    return svc, postgres


async def test_high_similarity_auto_links() -> None:
    items = {
        X: _item(X, "Used ApproveButton component for the approve action"),
        Y: _item(Y, "ConfirmButton component also renders an approve action"),
    }
    svc, postgres = _service(items, {Y: 0.95})
    result = await svc.detect_for_item("u1", X)
    assert len(result["auto_linked"]) == 1
    assert result["auto_linked"][0]["item_id"] == Y
    assert result["auto_linked"][0]["similarity"] == 0.95
    assert "relation_id" in result["auto_linked"][0]
    assert result["suggestions"] == []
    # edge really created, typed alternative_to, with auto metadata
    assert len(postgres.relations) == 1
    edge = postgres.relations[0]
    assert edge.relation_type == "alternative_to"
    assert edge.metadata["auto"] is True


async def test_mid_band_becomes_suggestion() -> None:
    items = {X: _item(X, "x"), Y: _item(Y, "y")}
    svc, postgres = _service(items, {Y: 0.85})
    result = await svc.detect_for_item("u1", X)
    assert result["auto_linked"] == []
    assert len(result["suggestions"]) == 1
    assert result["suggestions"][0]["item_id"] == Y
    assert postgres.relations == []


async def test_below_suggest_threshold_ignored() -> None:
    items = {X: _item(X, "x"), Y: _item(Y, "y")}
    svc, postgres = _service(items, {Y: 0.5})
    result = await svc.detect_for_item("u1", X)
    assert result["auto_linked"] == []
    assert result["suggestions"] == []


async def test_already_related_items_excluded() -> None:
    items = {X: _item(X, "x"), Y: _item(Y, "y")}
    svc, postgres = _service(items, {Y: 0.99})
    postgres.relations.append(MemoryRelation(
        user_id="u1", source_item_id=X, target_item_id=Y, relation_type="related_to",
    ))
    result = await svc.detect_for_item("u1", X)
    assert result["auto_linked"] == []
    assert result["suggestions"] == []
    assert len(postgres.relations) == 1  # no new edge


async def test_self_hit_excluded() -> None:
    items = {X: _item(X, "x")}
    svc, postgres = _service(items, {X: 1.0})
    result = await svc.detect_for_item("u1", X)
    assert result["auto_linked"] == []
    assert result["suggestions"] == []


async def test_detection_disabled_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    items = {X: _item(X, "x"), Y: _item(Y, "y")}
    svc, postgres = _service(items, {Y: 0.99})
    monkeypatch.setattr(settings.relation, "detect_enabled", False)
    result = await svc.detect_for_item("u1", X)
    assert result == {"auto_linked": [], "suggestions": []}
    assert postgres.relations == []


async def test_alternatives_groups_neighbors(monkeypatch: pytest.MonkeyPatch) -> None:
    items = {
        X: _item(X, "ApproveButton used for approvals"),
        Y: _item(Y, "ConfirmButton used for approvals"),
        Z: _item(Z, "unrelated logging fact"),
    }
    svc, postgres = _service(items, {X: 0.9, Z: 0.4})
    postgres.relations.append(MemoryRelation(
        user_id="u1", source_item_id=X, target_item_id=Y, relation_type="alternative_to",
    ))

    class StubEmbedder:
        async def embed(self, text: str) -> list[float]:
            return [1.0] * 4

    import src.core.alternatives as alt_mod
    monkeypatch.setattr(alt_mod, "get_embedder", lambda: StubEmbedder())

    groups = await svc.alternatives("u1", "approve button", top_k=5)
    assert groups, "expected at least one group"
    top = groups[0]
    assert top["item"]["item_id"] == X
    assert [n["item_id"] for n in top["alternatives"]] == [Y]
    assert top["alternatives"][0]["relation_type"] == "alternative_to"
    # Y appears as X's alternative, not duplicated as its own anchor group
    anchor_ids = [g["item"]["item_id"] for g in groups]
    assert Y not in anchor_ids


async def test_alternatives_skips_deprecated_neighbors(monkeypatch: pytest.MonkeyPatch) -> None:
    items = {
        X: _item(X, "current option"),
        Y: _item(Y, "old option", is_deprecated=True),
    }
    svc, postgres = _service(items, {X: 0.9})
    postgres.relations.append(MemoryRelation(
        user_id="u1", source_item_id=X, target_item_id=Y, relation_type="alternative_to",
    ))

    class StubEmbedder:
        async def embed(self, text: str) -> list[float]:
            return [1.0] * 4

    import src.core.alternatives as alt_mod
    monkeypatch.setattr(alt_mod, "get_embedder", lambda: StubEmbedder())

    groups = await svc.alternatives("u1", "option", top_k=5)
    assert groups[0]["alternatives"] == []
