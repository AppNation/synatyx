from __future__ import annotations

import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.config import settings
from src.core.alternatives import AlternativesService
from src.core.brief import BriefService
from src.core.budget import BudgetManager
from src.core.ingest import IngestService
from src.core.project import ProjectManager
from src.core.relation import RelationService
from src.core.retrieve import RetrieveService, empty_retrieve_diagnostics
from src.core.score import score_items
from src.core.skill import SkillService
from src.core.store import StoreService
from src.core.summarize import SummarizeService
from src.models.memory_layer import MemoryLayer
from src.storage.postgres import PostgresStorage
from src.storage.qdrant import QdrantStorage
from src.storage.redis import RedisStorage
from src.transports.mcp.tools import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

class SynatyxMCPServer:
    def __init__(
        self,
        qdrant: QdrantStorage,
        redis: RedisStorage,
        postgres: PostgresStorage,
    ) -> None:
        self._server = Server("synatyx-context-engine")
        self._default_qdrant = qdrant
        self._redis = redis
        self._postgres = postgres
        self._project_manager = ProjectManager(redis, qdrant)
        # Service cache keyed by collection_name — avoids re-creating services on every call
        self._svc_cache: dict[str, tuple[RetrieveService, StoreService, IngestService]] = {}
        self._budget = BudgetManager()
        self._skill_svc_cache: dict[str, SkillService] = {}
        self._register_handlers()

    async def _get_skill_service(self, user_id: str) -> SkillService:
        """Return a SkillService backed by the active project's Qdrant collection."""
        storage, _, _, _, _ = await self._get_services(user_id)
        key = storage.collection_name
        if key not in self._skill_svc_cache:
            self._skill_svc_cache[key] = SkillService(storage, self._postgres)
        return self._skill_svc_cache[key]

    async def _get_relation_service(self, user_id: str) -> RelationService:
        """Return a RelationService spanning the active project collection and ctx_users."""
        storage, _, _, _, _ = await self._get_services(user_id)
        l4_storage = await self._project_manager.get_l4_storage()
        return RelationService(self._postgres, storage, l4_storage)

    async def _get_alternatives_service(self, user_id: str) -> AlternativesService:
        storage, _, _, _, _ = await self._get_services(user_id)
        l4_storage = await self._project_manager.get_l4_storage()
        relations = RelationService(self._postgres, storage, l4_storage)
        return AlternativesService(storage, l4_storage, self._postgres, relations)

    async def _detect_alternatives_safe(self, user_id: str, item_id: str) -> dict[str, Any]:
        """Run same-purpose detection after a store; never let it break the store."""
        try:
            alternatives = await self._get_alternatives_service(user_id)
            return await alternatives.detect_for_item(user_id, item_id)
        except Exception:
            logger.exception("Alternative detection failed for item %s", item_id)
            return {"auto_linked": [], "suggestions": []}

    async def _get_l4_services(self) -> tuple[QdrantStorage, StoreService, RetrieveService]:
        """Return services backed by the shared ctx_users collection (L4 only)."""
        storage = await self._project_manager.get_l4_storage()
        key = storage.collection_name
        if key not in self._svc_cache:
            store_svc = StoreService(storage, self._redis, self._postgres)
            retrieve_svc = RetrieveService(storage, self._redis, self._postgres)
            ingest_svc = IngestService(store_svc)
            self._svc_cache[key] = (retrieve_svc, store_svc, ingest_svc)
        retrieve, store, _ = self._svc_cache[key]
        return storage, store, retrieve

    async def _get_services(
        self, user_id: str
    ) -> tuple[QdrantStorage, RetrieveService, StoreService, IngestService, str | None]:
        """Return project-scoped services for the given user.

        Returns:
            (storage, retrieve, store, ingest, cwd_suggestion)
            cwd_suggestion is non-None only when no project has been set yet.
        """
        storage, suggestion = await self._project_manager.get_storage(user_id)
        key = storage.collection_name
        if key not in self._svc_cache:
            store_svc = StoreService(storage, self._redis, self._postgres)
            retrieve_svc = RetrieveService(storage, self._redis, self._postgres)
            ingest_svc = IngestService(store_svc)
            self._svc_cache[key] = (retrieve_svc, store_svc, ingest_svc)
        retrieve, store, ingest = self._svc_cache[key]
        return storage, retrieve, store, ingest, suggestion

    def _register_handlers(self) -> None:
        @self._server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name=t["name"],
                    description=t["description"],
                    inputSchema=t["parameters"],
                )
                for t in TOOL_DEFINITIONS
            ]

        @self._server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            import json
            try:
                result = await self._dispatch(name, arguments)
            except Exception as exc:
                logger.exception("Tool %r raised an error", name)
                result = {"error": str(exc), "tool": name}
            return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        user_id = args.get("user_id", "")

        # ── Project management (no storage needed) ──────────────────────────
        if name == "context_set_project":
            slug, storage = await self._project_manager.set_project(user_id, args["project"])
            return {
                "project": slug,
                "collection": storage.collection_name,
                "message": f"Active project set to '{slug}' (collection: '{storage.collection_name}').",
            }

        elif name == "context_get_project":
            slug = await self._project_manager.get_project(user_id)
            if slug:
                return {"project": slug, "collection": f"ctx_{slug}", "suggestion": None}
            from src.core.project import _detect_cwd_name
            suggestion = _detect_cwd_name()
            return {
                "project": None,
                "collection": None,
                "suggestion": suggestion,
                "message": (
                    f"No project set. Detected workspace folder '{suggestion}'. "
                    f"Call context_set_project with project='{suggestion}' to confirm."
                ),
            }

        # ── All other tools — route to the active project's storage ─────────
        storage, retrieve, store, ingest, suggestion = await self._get_services(user_id)
        _warn: dict[str, Any] = (
            {"_project_warning": f"No project set. Detected workspace '{suggestion}'. Call context_set_project to confirm."}
            if suggestion else {}
        )

        if name == "context_brief":
            l4_storage = await self._project_manager.get_l4_storage()
            brief_svc = BriefService(storage, l4_storage, self._postgres)
            slug = await self._project_manager.get_project(user_id)
            brief_result = await brief_svc.brief(
                user_id=user_id,
                project=args.get("project"),
                session_id=args.get("session_id"),
                max_tokens=args.get("max_tokens", 2000),
                recent_days=args.get("recent_days", 7),
            )
            return {
                "project": slug,
                "collection": storage.collection_name,
                **brief_result,
                **_warn,
            }

        elif name == "context_retrieve":
            requested = [MemoryLayer(l) for l in args.get("memory_layers", [])] or list(MemoryLayer)
            top_k = args.get("top_k", 10)

            # Split: L4 always comes from ctx_users; everything else from the project collection
            project_layers = [l for l in requested if l != MemoryLayer.L4]
            include_l4 = MemoryLayer.L4 in requested

            combined_items = []
            suggested_budget: dict = {}

            if project_layers:
                proj_result = await retrieve.retrieve(
                    query=args["query"],
                    user_id=user_id,
                    session_id=args.get("session_id"),
                    project=args.get("project"),
                    top_k=top_k,
                    memory_layers=project_layers,
                )
                combined_items.extend(proj_result.context_items)
                suggested_budget = proj_result.suggested_budget

            if include_l4:
                _, _, l4_retrieve = await self._get_l4_services()
                l4_result = await l4_retrieve.retrieve(
                    query=args["query"],
                    user_id=user_id,
                    session_id=args.get("session_id"),
                    top_k=top_k,
                    memory_layers=[MemoryLayer.L4],
                )
                combined_items.extend(l4_result.context_items)
                suggested_budget = suggested_budget or l4_result.suggested_budget

            combined_items.sort(key=lambda x: x.score, reverse=True)
            final_items = combined_items[:top_k]
            total_tokens = sum(i.token_estimate for i in final_items)

            dumped_items = [i.model_dump() for i in final_items]

            # Optional 1-hop relation expansion — pull in linked memories
            if args.get("expand_relations") and final_items:
                relations = await self._get_relation_service(user_id)
                expanded = await relations.expand(
                    user_id=user_id,
                    item_ids=[i.id for i in final_items],
                    max_items=top_k,
                )
                dumped_items.extend(expanded)
                total_tokens += sum(len(e.get("content", "")) // 4 for e in expanded)

            retrieve_result: dict[str, Any] = {
                "context_items": dumped_items,
                "total_tokens": total_tokens,
                "suggested_budget": suggested_budget,
                **_warn,
            }

            # Empty results are ambiguous — attach diagnostics so the agent can
            # tell "nothing stored" from "filters/layers missed". Never let the
            # extra counting break the retrieve itself.
            if not final_items:
                try:
                    by_layer: dict[str, int] = {}
                    for _layer in (MemoryLayer.L2, MemoryLayer.L3):
                        by_layer[_layer.value] = await storage.count_items(
                            user_id=user_id, memory_layer=_layer
                        )
                    l4_storage = await self._project_manager.get_l4_storage()
                    by_layer[MemoryLayer.L4.value] = await l4_storage.count_items(
                        user_id=user_id, memory_layer=MemoryLayer.L4
                    )
                    retrieve_result["diagnostics"] = empty_retrieve_diagnostics(
                        total_for_user=sum(by_layer.values()),
                        items_by_layer=by_layer,
                        requested_layers=requested,
                        session_id=args.get("session_id"),
                        project=args.get("project"),
                    )
                except Exception:
                    logger.exception("Retrieve diagnostics failed (non-critical)")

            return retrieve_result

        elif name == "context_store":
            # Batch mode: store several items in one call
            if "items" in args and args["items"]:
                results: list[dict[str, Any]] = []
                for entry in args["items"]:
                    entry_layer = MemoryLayer(entry["memory_layer"])
                    # L4 is user-global — always goes to ctx_users
                    _store = (
                        store if entry_layer != MemoryLayer.L4
                        else (await self._get_l4_services())[1]
                    )
                    batch = await _store.store_batch(
                        [entry], user_id=user_id, session_id=args.get("session_id")
                    )
                    # Same-purpose detection (L1 lives in Redis — no embedding to compare)
                    if settings.relation.detect_enabled and entry_layer != MemoryLayer.L1:
                        for r in batch:
                            if "item_id" in r:
                                detection = await self._detect_alternatives_safe(
                                    user_id, r["item_id"]
                                )
                                if detection["auto_linked"] or detection["suggestions"]:
                                    r.update(detection)
                    results.extend(batch)
                stored = sum(1 for r in results if "error" not in r)
                return {
                    "results": results,
                    "stored": stored,
                    "failed": len(results) - stored,
                    **_warn,
                }

            if "content" not in args or "memory_layer" not in args:
                return {
                    "error": "Provide either 'items' (batch) or 'content' + 'memory_layer' (single)."
                }

            layer = MemoryLayer(args["memory_layer"])
            # L4 is user-global — always goes to ctx_users, not the active project collection
            _store = store if layer != MemoryLayer.L4 else (await self._get_l4_services())[1]
            item_ids, embedded = await _store.store(
                content=args["content"],
                user_id=user_id,
                memory_layer=layer,
                importance=args.get("importance", 0.5),
                session_id=args.get("session_id"),
                metadata=args.get("metadata"),
                confidence=args.get("confidence", 1.0),
                origin=args.get("origin"),
            )
            single_result: dict[str, Any] = {
                "item_id": item_ids[0], "item_ids": item_ids, "embedded": embedded, **_warn
            }
            # Same-purpose detection (L1 lives in Redis — no embedding to compare)
            if settings.relation.detect_enabled and layer != MemoryLayer.L1 and embedded:
                detection = await self._detect_alternatives_safe(user_id, item_ids[0])
                if detection["auto_linked"] or detection["suggestions"]:
                    single_result.update(detection)
            return single_result

        elif name == "context_summarize":
            summarize = SummarizeService(self._redis, self._postgres, store=store)
            await summarize.summarize_async(
                session_id=args["session_id"],
                user_id=user_id,
                max_tokens=args.get("max_tokens", 500),
                focus=args.get("focus"),
            )
            return {"status": "summarization_scheduled", **_warn}

        elif name == "context_score":
            from src.models.context import ContextItem
            items = [ContextItem(**i) for i in args["items"]]
            scored, dropped = score_items(items, args["query"])
            return {
                "scored_items": [i.model_dump() for i in scored],
                "dropped_items": [i.model_dump() for i in dropped],
            }

        elif name == "context_ingest":
            from src.models.memory_layer import MemoryLayer as ML
            layer_str = args.get("memory_layer", "L3")
            result = await ingest.ingest(
                source=args["source"],
                user_id=user_id,
                memory_layer=ML(layer_str),
                importance=float(args.get("importance", 0.8)),
                project=args.get("project"),
                session_id=args.get("session_id"),
            )
            return {
                "source": result.source,
                "chunks_stored": result.chunks_stored,
                "chunks_failed": result.chunks_failed,
                "total_chunks": result.total_chunks,
                **_warn,
            }

        elif name == "context_checkpoint":
            item_ids, embedded = await store.checkpoint(
                name=args["name"],
                content=args["content"],
                user_id=user_id,
                project=args.get("project"),
                session_id=args.get("session_id"),
            )
            return {"item_id": item_ids[0], "item_ids": item_ids, "embedded": embedded, "checkpoint_name": args["name"], **_warn}

        elif name == "context_deprecate":
            item_id = args["item_id"]
            superseded_by = args.get("superseded_by")
            # L4 items live in ctx_users — fall back if not in the project collection
            _dep_store = store
            if await storage.get_by_id(item_id) is None:
                l4_storage, l4_store, _ = await self._get_l4_services()
                if await l4_storage.get_by_id(item_id) is not None:
                    _dep_store = l4_store
            await _dep_store.deprecate(
                item_id=item_id,
                user_id=user_id,
                reason=args.get("reason"),
            )
            result: dict[str, Any] = {"deprecated": True, "item_id": item_id}
            if superseded_by:
                relations = await self._get_relation_service(user_id)
                edge, _created = await relations.relate(
                    user_id=user_id,
                    source_id=superseded_by,
                    target_id=item_id,
                    relation_type="supersedes",
                )
                result["superseded_by"] = superseded_by
                result["relation_id"] = edge.id
            return result

        elif name == "context_relate":
            relations = await self._get_relation_service(user_id)
            edge, created = await relations.relate(
                user_id=user_id,
                source_id=args["source_id"],
                target_id=args["target_id"],
                relation_type=args.get("relation_type", "related_to"),
                project=args.get("project"),
                metadata=args.get("metadata"),
            )
            return {
                "relation_id": edge.id,
                "source_id": edge.source_item_id,
                "target_id": edge.target_item_id,
                "relation_type": edge.relation_type,
                "created": created,
            }

        elif name == "context_unrelate":
            relations = await self._get_relation_service(user_id)
            deleted = await relations.unrelate(
                user_id=user_id,
                relation_id=args.get("relation_id"),
                source_id=args.get("source_id"),
                target_id=args.get("target_id"),
                relation_type=args.get("relation_type"),
            )
            return {"deleted": deleted}

        elif name == "context_related":
            relations = await self._get_relation_service(user_id)
            edges, neighbors = await relations.related(
                user_id=user_id,
                item_id=args["item_id"],
                relation_type=args.get("relation_type"),
                direction=args.get("direction", "both"),
            )
            return {
                "item_id": args["item_id"],
                "relations": [
                    {
                        "relation_id": e.id,
                        "source_id": e.source_item_id,
                        "target_id": e.target_item_id,
                        "relation_type": e.relation_type,
                        "created_at": e.created_at.isoformat(),
                    }
                    for e in edges
                ],
                "items": {item_id: item.model_dump() for item_id, item in neighbors.items()},
                "count": len(edges),
            }

        elif name == "context_get":
            relations = await self._get_relation_service(user_id)
            fetched = await relations.get_item(args["item_id"], user_id)
            if fetched is None:
                return {"error": f"Item {args['item_id']!r} not found"}
            return {"item": fetched.model_dump()}

        elif name == "context_alternatives":
            alternatives_svc = await self._get_alternatives_service(user_id)
            groups = await alternatives_svc.alternatives(
                user_id=user_id,
                query=args["query"],
                top_k=args.get("top_k", 5),
            )
            return {"query": args["query"], "groups": groups, "count": len(groups), **_warn}

        elif name == "context_visualize":
            from src.core.visualize import render_mermaid
            from src.models.memory_layer import MemoryLayer as ML
            layer_str = args.get("memory_layer")
            graph_layer = ML(layer_str) if layer_str else None
            # L4 lives in ctx_users — route there when the filter is explicitly L4
            _viz_storage = (
                (await self._get_l4_services())[0] if graph_layer == ML.L4 else storage
            )
            graph_items = await _viz_storage.list_items(
                user_id=user_id,
                memory_layer=graph_layer,
                include_deprecated=args.get("include_deprecated", True),
                project=args.get("project"),
                limit=args.get("limit", 50),
            )
            relations = await self._get_relation_service(user_id)
            edges = await self._postgres.relation_list(
                user_id=user_id,
                item_ids=[i.id for i in graph_items],
                limit=500,
            )
            # Edges may reach items outside the listed set (other layers, the
            # shared L4 collection, deprecated supersedes targets) — hydrate
            # those endpoints so their edges render instead of being dropped.
            known_ids = {i.id for i in graph_items}
            for edge in edges:
                for endpoint in (edge.source_item_id, edge.target_item_id):
                    if endpoint in known_ids:
                        continue
                    known_ids.add(endpoint)
                    try:
                        neighbor = await relations.get_item(endpoint, user_id)
                    except PermissionError:
                        continue
                    if neighbor is not None:
                        graph_items.append(neighbor)
            mermaid, node_count, edge_count = render_mermaid(
                graph_items,
                edges,
                direction=args.get("direction", "LR"),
                relations_only=args.get("relations_only", False),
            )
            return {
                "mermaid": mermaid,
                "node_count": node_count,
                "edge_count": edge_count,
                **_warn,
            }

        elif name == "context_list":
            from src.models.memory_layer import MemoryLayer as ML
            layer_str = args.get("memory_layer")
            layer = ML(layer_str) if layer_str else None
            # L4 lives in ctx_users — route list calls there when the filter is explicitly L4
            _list_storage = (await self._get_l4_services())[0] if layer == ML.L4 else storage
            items = await _list_storage.list_items(
                user_id=user_id,
                memory_layer=layer,
                checkpoints_only=args.get("checkpoints_only", False),
                include_deprecated=args.get("include_deprecated", False),
                project=args.get("project"),
                limit=args.get("limit", 50),
            )
            return {
                "items": [
                    {
                        "id": i.id,
                        "memory_layer": i.memory_layer.value,
                        "content": i.content[:200],
                        "importance": i.importance,
                        "is_pinned": i.is_pinned,
                        "is_deprecated": i.is_deprecated,
                        "metadata": i.metadata,
                    }
                    for i in items
                ],
                "count": len(items),
                **_warn,
            }

        elif name == "context_task_add":
            from src.models.task import Task, TaskPriority, TaskStatus
            task = Task(
                user_id=user_id,
                title=args["title"],
                description=args.get("description", ""),
                priority=TaskPriority(args.get("priority", "medium")),
                project=args.get("project"),
            )
            saved = await self._postgres.task_add(task)
            return {"task_id": saved.id, "title": saved.title, "status": saved.status, "priority": saved.priority}

        elif name == "context_task_list":
            from src.models.task import TaskPriority, TaskStatus
            status_str = args.get("status", "pending")
            priority_str = args.get("priority")
            tasks = await self._postgres.task_list(
                user_id=user_id,
                status=TaskStatus(status_str) if status_str else None,
                priority=TaskPriority(priority_str) if priority_str else None,
                project=args.get("project"),
                limit=args.get("limit", 50),
            )
            return {
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description,
                        "status": t.status,
                        "priority": t.priority,
                        "project": t.project,
                        "created_at": t.created_at.isoformat(),
                    }
                    for t in tasks
                ],
                "count": len(tasks),
            }

        elif name == "context_task_update":
            from src.models.task import TaskPriority, TaskStatus
            status_str = args.get("status")
            priority_str = args.get("priority")
            updated = await self._postgres.task_update(
                task_id=args["task_id"],
                user_id=user_id,
                status=TaskStatus(status_str) if status_str else None,
                priority=TaskPriority(priority_str) if priority_str else None,
                title=args.get("title"),
                description=args.get("description"),
            )
            if not updated:
                return {"error": f"Task {args['task_id']!r} not found"}
            return {"task_id": updated.id, "title": updated.title, "status": updated.status, "updated_at": updated.updated_at.isoformat()}

        elif name == "context_skill_store":
            svc = await self._get_skill_service(user_id)
            skill = await svc.store(
                name=args["name"],
                description=args["description"],
                content=args["content"],
                user_id=user_id,
                project=args.get("project"),
                frontmatter=args.get("frontmatter"),
            )
            return {
                "skill_id": skill.id,
                "name": skill.name,
                "slug": skill.slug,
                "project": skill.project,
                "created_at": skill.created_at.isoformat(),
            }

        elif name == "context_skill_find":
            svc = await self._get_skill_service(user_id)
            results = await svc.find(
                query=args["query"],
                user_id=user_id,
                project=args.get("project"),
                top_k=args.get("top_k", 3),
            )
            return {"skills": results, "count": len(results)}

        elif name == "context_skill_get":
            svc = await self._get_skill_service(user_id)
            skill = await svc.get(
                name=args["name"],
                user_id=user_id,
                project=args.get("project"),
            )
            if not skill:
                return {"error": f"Skill {args['name']!r} not found"}
            return {
                "name": skill.name,
                "slug": skill.slug,
                "description": skill.description,
                "content": skill.content,
                "frontmatter": skill.frontmatter,
                "project": skill.project,
            }

        elif name == "context_skill_list":
            svc = await self._get_skill_service(user_id)
            skills = await svc.list_skills(
                user_id=user_id,
                project=args.get("project"),
                limit=args.get("limit", 50),
            )
            return {
                "skills": [
                    {"name": s.name, "slug": s.slug, "description": s.description, "project": s.project}
                    for s in skills
                ],
                "count": len(skills),
            }

        elif name == "context_skill_delete":
            svc = await self._get_skill_service(user_id)
            deleted = await svc.delete(name=args["name"], user_id=user_id)
            if not deleted:
                return {"error": f"Skill {args['name']!r} not found"}
            return {"deleted": True, "name": args["name"]}

        elif name == "context_gc_stats":
            # _get_services returns (storage, retrieve, store, ingest, suggestion)
            qdrant = storage
            from src.config import settings as _settings
            from src.core.gc import GarbageCollector, _IMMUNE_LAYERS, _IMMUNE_TYPE
            from datetime import datetime, timedelta, timezone

            gc = GarbageCollector(qdrant=qdrant, postgres=self._postgres, settings=_settings.gc)
            now = datetime.now(timezone.utc)
            warn_threshold = timedelta(days=14)

            all_items, _ = await qdrant.scan_all_items(include_deprecated=True, limit=1000)
            total = len(all_items)
            protected = expiring_soon = deprecated = pending_hard_delete = 0

            for item in all_items:
                if item.get("is_deprecated"):
                    deprecated += 1
                    dep_raw = item.get("deprecated_at")
                    if dep_raw:
                        dep_at = datetime.fromisoformat(dep_raw)
                        if dep_at.tzinfo is None:
                            dep_at = dep_at.replace(tzinfo=timezone.utc)
                        if (now - dep_at).days >= _settings.gc.grace_period_days:
                            pending_hard_delete += 1
                    continue

                if gc._is_immune(item):
                    protected += 1
                    continue

                base_ttl = gc._get_base_ttl(item.get("memory_layer", ""))
                if base_ttl is None:
                    protected += 1
                    continue

                importance = float(item.get("importance", 0.5))
                effective_ttl = base_ttl * (1 + importance * _settings.gc.importance_multiplier)
                last_raw = item.get("last_accessed_at") or item.get("created_at")
                if last_raw:
                    last = datetime.fromisoformat(last_raw)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    remaining = timedelta(days=effective_ttl) - (now - last)
                    if timedelta(0) < remaining <= warn_threshold:
                        expiring_soon += 1

            return {
                "total_items": total,
                "protected": protected,
                "expiring_soon_14d": expiring_soon,
                "already_deprecated": deprecated,
                "pending_hard_delete": pending_hard_delete,
                "gc_enabled": _settings.gc.enabled,
                "l2_base_ttl_days": _settings.gc.l2_base_ttl_days,
                "l3_base_ttl_days": _settings.gc.l3_base_ttl_days,
                "grace_period_days": _settings.gc.grace_period_days,
            }

        raise ValueError(f"Unknown tool: {name}")

    async def run_stdio(self) -> None:
        """Run the MCP server over stdio (for OpenClaw / Claude Desktop)."""
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(read_stream, write_stream, self._server.create_initialization_options())

