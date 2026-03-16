from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from unified_modernization.contracts.events import SourceTechnology


class ProjectionStatus(StrEnum):
    PENDING_REQUIRED_FRAGMENT = "pending_required_fragment"
    PENDING_REHYDRATION = "pending_rehydration"
    READY_TO_BUILD = "ready_to_build"
    PUBLISHED = "published"
    STALE = "stale"
    DELETED = "deleted"
    QUARANTINED = "quarantined"


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    DELETED = "deleted"


class DependencyRule(BaseModel):
    owner: str
    required: bool = True
    freshness_ttl_seconds: int | None = None
    field_owners: list[str] = Field(default_factory=list)

    def is_stale(self, event_time_utc: datetime, now_utc: datetime) -> bool:
        if self.freshness_ttl_seconds is None:
            return False
        return now_utc - event_time_utc > timedelta(seconds=self.freshness_ttl_seconds)


class DependencyPolicy(BaseModel):
    entity_type: str
    rules: list[DependencyRule]
    allow_partial_optional_publish: bool = False


class ProjectionKey(BaseModel):
    tenant_id: str
    domain_name: str
    entity_type: str
    logical_entity_id: str


class FragmentRecord(BaseModel):
    tenant_id: str
    domain_name: str
    entity_type: str
    logical_entity_id: str
    fragment_owner: str
    source_technology: SourceTechnology
    source_version: int = Field(ge=0)
    event_time_utc: datetime
    payload: dict[str, Any]
    delete_flag: bool = False

    @field_validator("event_time_utc")
    @classmethod
    def ensure_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ProjectionStateRecord(BaseModel):
    tenant_id: str
    domain_name: str
    entity_type: str
    logical_entity_id: str
    status: ProjectionStatus
    reason_code: str
    projection_version: int = 0
    last_built_utc: datetime | None = None
    last_published_utc: datetime | None = None
    last_source_change_utc: datetime | None = None
    missing_required_fragments: list[str] = Field(default_factory=list)
    stale_required_fragments: list[str] = Field(default_factory=list)
    completeness_status: CompletenessStatus = CompletenessStatus.PARTIAL
    last_payload_hash: str | None = None


class SearchDocument(BaseModel):
    document_id: str
    tenant_id: str
    domain_name: str
    entity_type: str
    projection_version: int
    completeness_status: CompletenessStatus
    source_versions: dict[str, int]
    payload: dict[str, Any]


class PublicationDecision(BaseModel):
    publish: bool
    state: ProjectionStateRecord
    document: SearchDocument | None = None
