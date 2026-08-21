import asyncio
import json

import pytest

from src.core.events import CallEventBus, _truncate


def _event(bus: CallEventBus, **overrides):
    kwargs = dict(
        tool="context_retrieve",
        user_id="u1",
        project="proj",
        session_id="proj",
        duration_ms=42,
        error=False,
        input_tokens=10,
        output_tokens=20,
        embedding_tokens=5,
        arguments={"query": "hello"},
        result={"context_items": []},
    )
    kwargs.update(overrides)
    return bus.build_event(**kwargs)


def test_build_event_carries_metadata_and_payloads():
    bus = CallEventBus()
    event = _event(bus)
    assert event["tool"] == "context_retrieve"
    assert event["project"] == "proj"
    assert event["duration_ms"] == 42
    assert event["tokens"] == {"input": 10, "output": 20, "embedding": 5}
    assert json.loads(event["arguments"]) == {"query": "hello"}
    assert json.loads(event["response"]) == {"context_items": []}
    assert "truncated" not in event


def test_seq_is_monotonic():
    bus = CallEventBus()
    first = _event(bus)
    second = _event(bus)
    assert second["seq"] == first["seq"] + 1


def test_truncate_cuts_at_limit():
    text, cut = _truncate({"blob": "x" * 100}, 20)
    assert cut is True
    assert len(text) == 20
    text, cut = _truncate({"a": 1}, 4096)
    assert cut is False
    assert json.loads(text) == {"a": 1}


def test_oversized_payload_flags_event_truncated(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings.events, "max_payload_bytes", 32)
    bus = CallEventBus()
    event = _event(bus, result={"blob": "y" * 500})
    assert event["truncated"] is True
    assert len(event["response"]) == 32


def test_capture_payloads_off_strips_payloads(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings.events, "capture_payloads", False)
    bus = CallEventBus()
    event = _event(bus)
    assert "arguments" not in event
    assert "response" not in event
    assert event["tokens"]["output"] == 20


def test_recent_filters_by_project_and_limits():
    bus = CallEventBus()
    for i in range(5):
        bus.publish(_event(bus, project="a" if i % 2 == 0 else "b"))
    assert len(bus.recent()) == 5
    a_events = bus.recent(project="a")
    assert len(a_events) == 3
    assert all(e["project"] == "a" for e in a_events)
    assert len(bus.recent(limit=2)) == 2
    # chronological: newest last
    seqs = [e["seq"] for e in bus.recent()]
    assert seqs == sorted(seqs)


def test_ring_buffer_evicts_oldest():
    bus = CallEventBus(buffer_size=3)
    for _ in range(5):
        bus.publish(_event(bus))
    events = bus.recent()
    assert len(events) == 3
    assert events[-1]["seq"] == 5


async def test_subscribers_receive_published_events():
    bus = CallEventBus()
    queue = bus.subscribe()
    event = _event(bus)
    bus.publish(event)
    received = await asyncio.wait_for(queue.get(), timeout=1)
    assert received["seq"] == event["seq"]
    bus.unsubscribe(queue)
    assert bus.subscriber_count == 0


async def test_full_subscriber_queue_drops_instead_of_blocking():
    bus = CallEventBus()
    queue = bus.subscribe()
    from src.core import events as events_mod
    for _ in range(events_mod._QUEUE_SIZE + 10):
        bus.publish(_event(bus))  # must never raise or block
    assert queue.qsize() == events_mod._QUEUE_SIZE
    bus.unsubscribe(queue)


def test_call_tool_publishes_to_bus(monkeypatch):
    """The global bus in server.py receives one event per tool call."""
    from src.core.events import event_bus
    before = len(event_bus.recent(limit=500))
    event_bus.publish(_event(event_bus, tool="context_store"))
    after = event_bus.recent(limit=500)
    assert len(after) == before + 1
    assert after[-1]["tool"] == "context_store"


def _routes_app():
    from starlette.applications import Starlette
    from starlette.routing import Route

    from src.transports.mcp.http_server import api_calls, events_calls

    return Starlette(routes=[
        Route("/dashboard/api/calls", api_calls),
        Route("/events/calls", events_calls),
    ])


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    return TestClient(_routes_app())


def test_api_calls_returns_recent(client):
    from src.core.events import event_bus
    event_bus.publish(_event(event_bus, project="apitest"))
    resp = client.get("/dashboard/api/calls", params={"project": "apitest"})
    assert resp.status_code == 200
    calls = resp.json()["calls"]
    assert calls and all(c["project"] == "apitest" for c in calls)


def test_api_calls_rejects_bad_limit(client):
    resp = client.get("/dashboard/api/calls", params={"limit": "nope"})
    assert resp.status_code == 400


async def test_sse_stream_replays_then_streams_live():
    # Drive the generator directly — HTTP-level streaming can't be shut down
    # deterministically under a test transport, the frame protocol can.
    from src.core.events import event_bus
    from src.transports.mcp.http_server import _sse_call_stream

    replayed = _event(event_bus, project="ssetest")
    event_bus.publish(replayed)
    gen = _sse_call_stream("ssetest", 5)
    try:
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1)
        assert frame.startswith("data: ")
        assert json.loads(frame[6:].strip())["project"] == "ssetest"

        # Live phase: next frame arrives only once a matching event is
        # published; other-project events are filtered out.
        event_bus.publish(_event(event_bus, project="other"))
        live = _event(event_bus, project="ssetest")
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0)  # let the generator park on queue.get()
        event_bus.publish(live)
        frame = await asyncio.wait_for(task, timeout=1)
        assert json.loads(frame[6:].strip())["seq"] == live["seq"]
    finally:
        await gen.aclose()
    assert event_bus.subscriber_count == 0


async def test_sse_stream_pings_when_idle():
    from src.transports.mcp.http_server import _sse_call_stream

    gen = _sse_call_stream("nothing-here", 0, ping_seconds=0.05)
    try:
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1)
        assert frame.startswith(": ping")
    finally:
        await gen.aclose()


def test_events_calls_404_when_disabled(client, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings.events, "enabled", False)
    resp = client.get("/events/calls")
    assert resp.status_code == 404
