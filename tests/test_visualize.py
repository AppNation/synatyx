from __future__ import annotations

from typing import Any

from src.core.visualize import _escape_label, _node_id, render_mermaid
from src.models.context import ContextItem
from src.models.memory_layer import MemoryLayer
from src.models.relation import MemoryRelation


def _item(item_id: str, layer: MemoryLayer = MemoryLayer.L3, **overrides: Any) -> ContextItem:
    base: dict[str, Any] = {
        "id": item_id,
        "user_id": "u1",
        "content": f"content of {item_id}",
        "memory_layer": layer,
    }
    base.update(overrides)
    return ContextItem(**base)


def _edge(source: str, target: str, relation_type: str = "related_to") -> MemoryRelation:
    return MemoryRelation(
        user_id="u1",
        source_item_id=source,
        target_item_id=target,
        relation_type=relation_type,
    )


def test_node_id_is_stable_and_mermaid_safe() -> None:
    assert _node_id("f47ac10b-58cc-4372-a567-0e02b2c3d479") == "n_f47ac10b58cc"
    assert _node_id("f47ac10b-58cc-4372-a567-0e02b2c3d479") == _node_id(
        "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    )


def test_escape_label_truncates_and_escapes() -> None:
    assert '"' not in _escape_label('say "hi" <b>now</b>')
    assert len(_escape_label("x" * 500)) <= 60
    assert "\n" not in _escape_label("line one\nline two")


def test_render_basic_graph() -> None:
    a, b = _item("aaaa1111-0000-0000-0000-000000000000"), _item(
        "bbbb2222-0000-0000-0000-000000000000", layer=MemoryLayer.L4
    )
    mermaid, nodes, edges = render_mermaid([a, b], [_edge(a.id, b.id, "supersedes")])
    assert mermaid.startswith("graph LR")
    assert nodes == 2
    assert edges == 1
    assert '-- "supersedes" -->' in mermaid
    assert "classDef L3" in mermaid
    assert "classDef L4" in mermaid


def test_edges_with_missing_endpoints_are_dropped() -> None:
    a = _item("aaaa1111-0000-0000-0000-000000000000")
    mermaid, nodes, edges = render_mermaid(
        [a], [_edge(a.id, "cccc3333-0000-0000-0000-000000000000")]
    )
    assert nodes == 1
    assert edges == 0
    assert "-->" not in mermaid


def test_relations_only_hides_isolated_nodes() -> None:
    a = _item("aaaa1111-0000-0000-0000-000000000000")
    b = _item("bbbb2222-0000-0000-0000-000000000000")
    lonely = _item("cccc3333-0000-0000-0000-000000000000")
    mermaid, nodes, edges = render_mermaid(
        [a, b, lonely], [_edge(a.id, b.id)], relations_only=True
    )
    assert nodes == 2
    assert edges == 1
    assert _node_id(lonely.id) not in mermaid


def test_deprecated_and_pinned_styling() -> None:
    old = _item("aaaa1111-0000-0000-0000-000000000000", is_deprecated=True)
    pinned = _item("bbbb2222-0000-0000-0000-000000000000", is_pinned=True)
    mermaid, _, _ = render_mermaid([old, pinned], [_edge(pinned.id, old.id, "supersedes")])
    assert "classDef deprecated" in mermaid
    assert f"class {_node_id(old.id)} deprecated;" in mermaid
    assert "classDef pinned" in mermaid
    assert f"class {_node_id(pinned.id)} pinned;" in mermaid
    # deprecated nodes keep the dashed style, not their layer color
    assert _node_id(old.id) not in [
        part
        for line in mermaid.splitlines()
        if line.strip().startswith("class ") and line.rstrip().endswith(" L3;")
        for part in line.split()[1].split(",")
    ]


def test_direction_td() -> None:
    a = _item("aaaa1111-0000-0000-0000-000000000000")
    mermaid, _, _ = render_mermaid([a], [], direction="TD")
    assert mermaid.startswith("graph TD")
