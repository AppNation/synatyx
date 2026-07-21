from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Well-known relation types. Arbitrary custom strings are also accepted —
# these are the ones with defined semantics (e.g. supersedes chains).
KNOWN_RELATION_TYPES = {
    "related_to",
    "supersedes",
    "part_of",
    "depends_on",
    "caused_by",
    "alternative_to",
    "used_for",
}

DEFAULT_RELATION_TYPE = "related_to"

_MAX_TYPE_LENGTH = 64


class MemoryRelation(BaseModel):
    """A directed edge between two memory items (source → target).

    Item ids are globally unique UUIDs, so edges may span Qdrant collections
    (e.g. a project item linked to a user-global L4 item).
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    source_item_id: str
    target_item_id: str
    relation_type: str = DEFAULT_RELATION_TYPE
    project: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("relation_type")
    @classmethod
    def _normalize_type(cls, v: str) -> str:
        v = v.strip().lower().replace(" ", "_").replace("-", "_")
        if not v:
            return DEFAULT_RELATION_TYPE
        if len(v) > _MAX_TYPE_LENGTH:
            raise ValueError(f"relation_type longer than {_MAX_TYPE_LENGTH} chars")
        return v

    @field_validator("target_item_id")
    @classmethod
    def _no_self_link(cls, v: str, info: Any) -> str:
        if v == info.data.get("source_item_id"):
            raise ValueError("source_item_id and target_item_id must differ")
        return v

    model_config = {"frozen": False}
