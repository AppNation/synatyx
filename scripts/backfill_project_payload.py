#!/usr/bin/env python3
"""Backfill the `project` payload field on existing Qdrant points.

Historically, points stored through context_store carried `project: null`
(only /capture set it), so any retrieve/list with a project filter returned
nothing. New points are stamped with the project slug at upsert time; this
script brings existing points in line.

For every `ctx_<slug>` collection except the shared ones (ctx_users,
ctx_default), sets `project = <slug>` on all points where it is missing or
null. Idempotent — safe to re-run.

Run inside the mcp container (or any env with QDRANT_HOST/PORT set):
    python scripts/backfill_project_payload.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio

from qdrant_client import AsyncQdrantClient

from src.config import settings

SHARED_COLLECTIONS = {"ctx_users", "ctx_default"}
PREFIX = "ctx_"
BATCH = 256


async def backfill_collection(client: AsyncQdrantClient, name: str, dry_run: bool) -> int:
    slug = name[len(PREFIX):]
    pending: list[str] = []
    offset = None
    while True:
        points, offset = await client.scroll(
            collection_name=name,
            limit=BATCH,
            offset=offset,
            with_payload=["project"],
            with_vectors=False,
        )
        pending.extend(str(p.id) for p in points if (p.payload or {}).get("project") is None)
        if offset is None:
            break

    if pending and not dry_run:
        for i in range(0, len(pending), BATCH):
            await client.set_payload(
                collection_name=name,
                payload={"project": slug},
                points=pending[i : i + BATCH],
                wait=True,
            )
    return len(pending)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args()

    client = AsyncQdrantClient(host=settings.qdrant.host, port=settings.qdrant.port)
    try:
        collections = await client.get_collections()
        total = 0
        for c in sorted(col.name for col in collections.collections):
            if not c.startswith(PREFIX) or c in SHARED_COLLECTIONS:
                continue
            n = await backfill_collection(client, c, args.dry_run)
            total += n
            verb = "would stamp" if args.dry_run else "stamped"
            print(f"{c}: {verb} project='{c[len(PREFIX):]}' on {n} points")
        print(f"{'DRY RUN — ' if args.dry_run else ''}total: {total} points")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
