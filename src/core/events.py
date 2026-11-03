"""Live tool-call event bus — in-process pub/sub with a replay ring buffer.

Every MCP tool call publishes one event (tool, user, project, duration,
tokens, truncated args/response). Subscribers (the /events/calls SSE feed,
the VS Code call monitor) receive events as they happen; the ring buffer
lets a client that just connected backfill recent history without a
database round-trip. Payload capture is truncated and can be disabled
entirely — responses contain memory content.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from itertools import count
from typing import Any

from src.config import settings

# Subscriber queues are bounded; a slow consumer drops events rather than
# stalling call_tool.
_QUEUE_SIZE = 256


def _truncate(value: Any, limit: int) -> tuple[str, bool]:
    """Serialize a payload to JSON, cut at `limit` bytes on a char boundary."""
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class CallEventBus:
    def __init__(self, *, buffer_size: int | None = None) -> None:
        cfg = settings.events
        self._buffer: deque[dict[str, Any]] = deque(
            maxlen=buffer_size or cfg.buffer_size
        )
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._seq = count(1)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def build_event(
        self,
        *,
        tool: str,
        user_id: str,
        project: str | None,
        session_id: str | None,
        duration_ms: int,
        error: bool,
        input_tokens: int,
        output_tokens: int,
        embedding_tokens: int,
        arguments: dict[str, Any] | None,
        result: Any,
    ) -> dict[str, Any]:
        cfg = settings.events
        event: dict[str, Any] = {
            "seq": next(self._seq),
            "ts": time.time(),
            "tool": tool,
            "user_id": user_id,
            "project": project,
            "session_id": session_id,
            "duration_ms": duration_ms,
            "error": error,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "embedding": embedding_tokens,
            },
        }
        if cfg.capture_payloads:
            args, args_cut = _truncate(arguments or {}, cfg.max_payload_bytes)
            resp, resp_cut = _truncate(result, cfg.max_payload_bytes)
            event["arguments"] = args
            event["response"] = resp
            if args_cut or resp_cut:
                event["truncated"] = True
        return event

    def publish(self, event: dict[str, Any]) -> None:
        self._buffer.append(event)
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer: drop for them, never block the caller

    def recent(
        self, *, project: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Most recent events, newest last (chronological), optionally
        filtered by project."""
        events = [
            e for e in self._buffer
            if project is None or e.get("project") == project
        ]
        return events[-limit:]

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)


# One bus per process — call_tool publishes here, HTTP routes read from here.
event_bus = CallEventBus()
