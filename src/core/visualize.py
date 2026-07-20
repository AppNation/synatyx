from __future__ import annotations

from src.models.context import ContextItem
from src.models.relation import MemoryRelation

# Mermaid class styles per memory layer, plus modifiers for deprecated /
# pinned nodes. Colors chosen for readability on both light and dark themes.
_LAYER_CLASSES = {
    "L1": "classDef L1 fill:#fde68a,stroke:#b45309,color:#1c1917;",
    "L2": "classDef L2 fill:#bfdbfe,stroke:#1d4ed8,color:#1c1917;",
    "L3": "classDef L3 fill:#bbf7d0,stroke:#15803d,color:#1c1917;",
    "L4": "classDef L4 fill:#e9d5ff,stroke:#7e22ce,color:#1c1917;",
}
_DEPRECATED_CLASS = (
    "classDef deprecated fill:#e7e5e4,stroke:#78716c,color:#57534e,stroke-dasharray: 5 5;"
)
_PINNED_CLASS = "classDef pinned stroke-width:3px;"

_MAX_LABEL_CHARS = 60


def _node_id(item_id: str) -> str:
    """Stable short Mermaid node id derived from the item UUID."""
    return "n_" + item_id.replace("-", "")[:12]


def _escape_label(text: str) -> str:
    """Make free text safe inside a quoted Mermaid node label."""
    cleaned = " ".join(text.split())
    if len(cleaned) > _MAX_LABEL_CHARS:
        cleaned = cleaned[: _MAX_LABEL_CHARS - 1] + "…"
    return cleaned.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def render_mermaid(
    items: list[ContextItem],
    edges: list[MemoryRelation],
    direction: str = "LR",
    relations_only: bool = False,
) -> tuple[str, int, int]:
    """Render items + relation edges as a Mermaid flowchart.

    Only edges whose both endpoints are present in `items` are drawn.
    With relations_only=True, isolated nodes are omitted.

    Returns (mermaid_source, node_count, edge_count).
    """
    by_id = {item.id: item for item in items}
    drawable = [
        e for e in edges if e.source_item_id in by_id and e.target_item_id in by_id
    ]
    if relations_only:
        linked = {e.source_item_id for e in drawable} | {e.target_item_id for e in drawable}
        by_id = {item_id: item for item_id, item in by_id.items() if item_id in linked}
        drawable = [
            e for e in drawable if e.source_item_id in by_id and e.target_item_id in by_id
        ]

    lines = [f"graph {direction}"]
    used_layers: set[str] = set()

    for item in by_id.values():
        layer = item.memory_layer.value
        used_layers.add(layer)
        label = f"{layer}: {_escape_label(item.content)}"
        lines.append(f'    {_node_id(item.id)}["{label}"]')

    for edge in drawable:
        source = _node_id(edge.source_item_id)
        target = _node_id(edge.target_item_id)
        lines.append(f'    {source} -- "{_escape_label(edge.relation_type)}" --> {target}')

    for layer in sorted(used_layers):
        lines.append(f"    {_LAYER_CLASSES[layer]}")
        member_ids = [
            _node_id(i.id)
            for i in by_id.values()
            if i.memory_layer.value == layer and not i.is_deprecated
        ]
        if member_ids:
            lines.append(f"    class {','.join(member_ids)} {layer};")

    deprecated_ids = [_node_id(i.id) for i in by_id.values() if i.is_deprecated]
    if deprecated_ids:
        lines.append(f"    {_DEPRECATED_CLASS}")
        lines.append(f"    class {','.join(deprecated_ids)} deprecated;")

    pinned_ids = [_node_id(i.id) for i in by_id.values() if i.is_pinned]
    if pinned_ids:
        lines.append(f"    {_PINNED_CLASS}")
        lines.append(f"    class {','.join(pinned_ids)} pinned;")

    return "\n".join(lines), len(by_id), len(drawable)
