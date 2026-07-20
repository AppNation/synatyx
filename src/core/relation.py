from __future__ import annotations

import logging
from typing import Any

from src.models.context import ContextItem
from src.models.relation import MemoryRelation
from src.storage.postgres import PostgresStorage
from src.storage.qdrant import QdrantStorage

logger = logging.getLogger(__name__)


class RelationService:
    """Manages typed edges between memory items.

    Edges live in Postgres (source of truth); items live in Qdrant. Because
    item ids are globally unique UUIDs, an edge may span collections — e.g. a
    project item linked to a user-global L4 item — so item lookups check the
    project collection first, then the shared L4 collection.
    """

    def __init__(
        self,
        postgres: PostgresStorage,
        project_storage: QdrantStorage,
        l4_storage: QdrantStorage,
    ) -> None:
        self._postgres = postgres
        self._project_storage = project_storage
        self._l4_storage = l4_storage

    async def get_item(self, item_id: str, user_id: str) -> ContextItem | None:
        """Fetch an item by id from the project collection or ctx_users,
        enforcing user ownership."""
        for storage in (self._project_storage, self._l4_storage):
            item = await storage.get_by_id(item_id)
            if item is not None:
                if item.user_id != user_id:
                    raise PermissionError(f"User isolation violation for item {item_id!r}")
                return item
        return None

    async def relate(
        self,
        user_id: str,
        source_id: str,
        target_id: str,
        relation_type: str = "related_to",
        project: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[MemoryRelation, bool]:
        """Create an edge source → target. Both items must exist and belong
        to the user. Returns (relation, created)."""
        for item_id in (source_id, target_id):
            if await self.get_item(item_id, user_id) is None:
                raise ValueError(f"Item {item_id!r} not found for user {user_id!r}")

        relation = MemoryRelation(
            user_id=user_id,
            source_item_id=source_id,
            target_item_id=target_id,
            relation_type=relation_type,
            project=project,
            metadata=metadata or {},
        )
        saved, created = await self._postgres.relation_add(relation)
        await self._postgres.audit(user_id, "context_relate", {
            "relation_id": saved.id,
            "source_item_id": source_id,
            "target_item_id": target_id,
            "relation_type": saved.relation_type,
            "created": created,
        })
        return saved, created

    async def unrelate(
        self,
        user_id: str,
        relation_id: str | None = None,
        source_id: str | None = None,
        target_id: str | None = None,
        relation_type: str | None = None,
    ) -> int:
        """Delete edge(s) by relation id or endpoint pair. Returns count deleted."""
        deleted = await self._postgres.relation_delete(
            user_id=user_id,
            relation_id=relation_id,
            source_item_id=source_id,
            target_item_id=target_id,
            relation_type=relation_type,
        )
        if deleted:
            await self._postgres.audit(user_id, "context_unrelate", {
                "relation_id": relation_id,
                "source_item_id": source_id,
                "target_item_id": target_id,
                "deleted": deleted,
            })
        return deleted

    async def related(
        self,
        user_id: str,
        item_id: str,
        relation_type: str | None = None,
        direction: str = "both",
    ) -> tuple[list[MemoryRelation], dict[str, ContextItem]]:
        """Return edges touching item_id plus the hydrated neighbor items.

        Neighbors are fetched by direct point lookup, so deprecated items
        (e.g. targets of supersedes chains) are still returned.
        """
        edges = await self._postgres.relation_list(
            user_id=user_id,
            item_id=item_id,
            relation_type=relation_type,
            direction=direction,
        )
        neighbors: dict[str, ContextItem] = {}
        for edge in edges:
            other_id = (
                edge.target_item_id if edge.source_item_id == item_id else edge.source_item_id
            )
            if other_id in neighbors:
                continue
            try:
                item = await self.get_item(other_id, user_id)
            except PermissionError:
                continue
            if item is not None:
                neighbors[other_id] = item
        return edges, neighbors

    async def expand(
        self,
        user_id: str,
        item_ids: list[str],
        max_items: int = 10,
    ) -> list[dict[str, Any]]:
        """1-hop expansion for retrieval: given the retrieved item ids, return
        linked neighbor items (deduplicated, excluding the input set).

        Each result dict is the neighbor's model_dump plus a 'via_relation'
        marker describing the connecting edge.
        """
        if not item_ids:
            return []
        edges = await self._postgres.relation_list(user_id=user_id, item_ids=item_ids)
        seen = set(item_ids)
        expanded: list[dict[str, Any]] = []
        for edge in edges:
            if len(expanded) >= max_items:
                break
            anchor, other_id = (
                (edge.source_item_id, edge.target_item_id)
                if edge.source_item_id in seen
                else (edge.target_item_id, edge.source_item_id)
            )
            if other_id in seen:
                continue
            try:
                item = await self.get_item(other_id, user_id)
            except PermissionError:
                continue
            if item is None or item.is_deprecated:
                continue
            seen.add(other_id)
            dumped = item.model_dump()
            dumped["via_relation"] = {
                "relation_id": edge.id,
                "relation_type": edge.relation_type,
                "anchor_item_id": anchor,
            }
            expanded.append(dumped)
        return expanded
