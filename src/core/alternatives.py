from __future__ import annotations

import logging
from typing import Any

from src.config import settings
from src.core.embedder import get_embedder
from src.core.relation import RelationService
from src.models.relation import MemoryRelation
from src.storage.postgres import PostgresStorage
from src.storage.qdrant import QdrantStorage

logger = logging.getLogger(__name__)

# Edge types that mean "these serve the same purpose"
_ALTERNATIVE_TYPES = ("alternative_to", "used_for")


class AlternativesService:
    """Detects and groups memories that serve the same purpose.

    Two-band detection on store: when a new memory's embedding is highly
    similar to an existing one, an `alternative_to` edge is created
    automatically (similarity >= autolink threshold); mid-band matches are
    returned as suggestions for the agent to confirm with `context_relate`.
    """

    def __init__(
        self,
        project_storage: QdrantStorage,
        l4_storage: QdrantStorage,
        postgres: PostgresStorage,
        relations: RelationService,
    ) -> None:
        self._project_storage = project_storage
        self._l4_storage = l4_storage
        self._postgres = postgres
        self._relations = relations

    async def _storage_for(self, item_id: str) -> QdrantStorage | None:
        for storage in (self._project_storage, self._l4_storage):
            if await storage.get_by_id(item_id) is not None:
                return storage
        return None

    async def detect_for_item(self, user_id: str, item_id: str) -> dict[str, Any]:
        """Find same-purpose memories for a freshly stored item.

        Returns {"auto_linked": [...], "suggestions": [...]} — auto_linked
        entries got an `alternative_to` edge; suggestions are candidates in
        the [suggest_threshold, autolink_threshold) similarity band.
        """
        cfg = settings.relation
        result: dict[str, Any] = {"auto_linked": [], "suggestions": []}
        if not cfg.detect_enabled:
            return result

        storage = await self._storage_for(item_id)
        if storage is None:
            return result
        vector = await storage.get_vector(item_id)
        if vector is None:
            return result

        # Exclude the item itself and anything it is already linked to
        existing = await self._postgres.relation_list(user_id=user_id, item_id=item_id)
        linked_ids = {item_id}
        for edge in existing:
            linked_ids.add(edge.source_item_id)
            linked_ids.add(edge.target_item_id)

        hits = await storage.search(
            query_vector=vector,
            user_id=user_id,
            top_k=cfg.detect_limit + len(linked_ids),
            score_threshold=cfg.suggest_threshold,
        )

        for hit in hits:
            if hit.id in linked_ids:
                continue
            similarity = round(hit.semantic_score, 4)
            entry = {
                "item_id": hit.id,
                "content": hit.content[:160],
                "similarity": similarity,
            }
            if similarity >= cfg.autolink_threshold:
                try:
                    relation, created = await self._relations.relate(
                        user_id=user_id,
                        source_id=item_id,
                        target_id=hit.id,
                        relation_type="alternative_to",
                        metadata={"auto": True, "similarity": similarity},
                    )
                except (ValueError, PermissionError) as exc:
                    logger.warning("Auto-link skipped for %s -> %s: %s", item_id, hit.id, exc)
                    continue
                entry["relation_id"] = relation.id
                result["auto_linked"].append(entry)
            else:
                result["suggestions"].append(entry)
            if len(result["auto_linked"]) + len(result["suggestions"]) >= cfg.detect_limit:
                break
        return result

    async def alternatives(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Answer "what can I use for X?" — semantic search for the purpose,
        then group each match with its alternative_to/used_for neighbors.
        """
        vector = await get_embedder().embed(query)
        groups: list[dict[str, Any]] = []
        seen_anchor_ids: set[str] = set()

        for storage in (self._project_storage, self._l4_storage):
            hits = await storage.search(
                query_vector=vector,
                user_id=user_id,
                top_k=top_k,
                score_threshold=0.3,
            )
            for hit in hits:
                if hit.id in seen_anchor_ids:
                    continue
                seen_anchor_ids.add(hit.id)
                edges = await self._postgres.relation_list(user_id=user_id, item_id=hit.id)
                neighbors: list[dict[str, Any]] = []
                for edge in edges:
                    if edge.relation_type not in _ALTERNATIVE_TYPES:
                        continue
                    other_id = self._other_end(edge, hit.id)
                    try:
                        item = await self._relations.get_item(other_id, user_id)
                    except PermissionError:
                        continue
                    if item is None or item.is_deprecated:
                        continue
                    neighbors.append({
                        "item_id": item.id,
                        "content": item.content,
                        "memory_layer": item.memory_layer.value,
                        "relation_type": edge.relation_type,
                    })
                    seen_anchor_ids.add(item.id)
                groups.append({
                    "item": {
                        "item_id": hit.id,
                        "content": hit.content,
                        "memory_layer": hit.memory_layer.value,
                        "similarity": round(hit.semantic_score, 4),
                    },
                    "alternatives": neighbors,
                })
        # Groups with alternatives first, then by similarity
        groups.sort(
            key=lambda g: (len(g["alternatives"]) > 0, g["item"]["similarity"]),
            reverse=True,
        )
        return groups[:top_k]

    @staticmethod
    def _other_end(edge: MemoryRelation, anchor_id: str) -> str:
        return edge.target_item_id if edge.source_item_id == anchor_id else edge.source_item_id
