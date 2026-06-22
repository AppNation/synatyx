from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import settings
from src.storage.postgres import PostgresStorage
from src.storage.qdrant import QdrantStorage
from src.storage.redis import RedisStorage
from src.transports.mcp.server import SynatyxMCPServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Admin-key auth middleware (pure ASGI so SSE streaming is untouched).
# Validates a static key from the env against an incoming header. The key may
# be sent either in the configured header (default X-Auth-Key) or as
# `Authorization: Bearer <key>`. Public paths (e.g. /health) bypass the check.
# ---------------------------------------------------------------------------

class AdminKeyAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        admin_key: str,
        header_name: str,
        public_paths: frozenset[str],
    ) -> None:
        self.app = app
        self._admin_key = admin_key.encode()
        self._header_name = header_name.strip().lower().encode()
        self._public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._public_paths:
            await self.app(scope, receive, send)
            return

        if self._is_authorized(scope):
            await self.app(scope, receive, send)
            return

        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        await response(scope, receive, send)

    def _is_authorized(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])

        provided = headers.get(self._header_name)
        if provided is None:
            auth = headers.get(b"authorization", b"")
            if auth[:7].lower() == b"bearer ":
                provided = auth[7:].strip()

        return provided is not None and hmac.compare_digest(provided, self._admin_key)

# ---------------------------------------------------------------------------
# FastMCP instance — host/port resolved from env so Docker can override them.
# ---------------------------------------------------------------------------

_host = os.getenv("HOST", "0.0.0.0")
_port = int(os.getenv("PORT", "9000"))

mcp = FastMCP(
    "synatyx-context-engine",
    host=_host,
    port=_port,
    sse_path="/mcp/sse",
    message_path="/mcp/messages/",
)


# ---------------------------------------------------------------------------
# Lifespan — connect to all storage backends once, inject into FastMCP.
# SynatyxMCPServer registers every tool handler on the low-level mcp.Server.
# We swap FastMCP's internal server so the SSE transport carries the full
# tool set without re-registering anything.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[None]:
    qdrant = QdrantStorage(
        host=settings.qdrant.host,
        port=settings.qdrant.port,
        collection_name=settings.qdrant.collection_name,
    )
    await qdrant.init_collection()

    redis = RedisStorage(url=settings.redis.url)
    await redis.ping()

    postgres = PostgresStorage(dsn=settings.postgres.dsn)
    await postgres.connect()

    synatyx = SynatyxMCPServer(qdrant, redis, postgres)
    # Inject the fully-wired low-level Server into FastMCP so that handle_sse
    # picks it up on every incoming request.
    mcp._mcp_server = synatyx._server

    logger.info("Synatyx MCP HTTP server ready on %s:%d", _host, _port)

    yield

    await qdrant.close()
    await redis.close()
    await postgres.close()


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "synatyx-mcp"})


# ---------------------------------------------------------------------------
# ASGI app — FastMCP SSE routes + /health, wrapped with lifespan.
# ---------------------------------------------------------------------------

_sse_app = mcp.sse_app()

_PUBLIC_PATHS = frozenset({"/health"})

_middleware = []
if settings.auth.enabled:
    _middleware.append(
        Middleware(
            AdminKeyAuthMiddleware,
            admin_key=settings.auth.admin_key,
            header_name=settings.auth.header_name,
            public_paths=_PUBLIC_PATHS,
        )
    )
    logger.info("Admin-key auth enabled — expecting key in '%s' header", settings.auth.header_name)
else:
    logger.warning("AUTH_ADMIN_KEY not set — MCP HTTP server is UNAUTHENTICATED")

app = Starlette(
    routes=_sse_app.routes + [Route("/health", health)],
    middleware=_middleware,
    lifespan=lifespan,
)

