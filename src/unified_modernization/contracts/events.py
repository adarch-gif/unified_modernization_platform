from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class SourceTechnology(StrEnum):
    AZURE_SQL = "azure_sql"
    COSMOS = "cosmos"
    SPANNER = "spanner"
    FIRESTORE = "firestore"
    ALLOYDB = "alloydb"
    OUTBOX = "outbox"


class ChangeType(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"
    REFRESH = "refresh"
    REPAIR = "repair"


class MigrationStage(StrEnum):
    AZURE_PRIMARY = "azure_primary"
    AZURE_PRIMARY_GCP_WARMING = "azure_primary_gcp_warming"
    GCP_SHADOW_VALIDATED = "gcp_shadow_validated"
    GCP_PRIMARY_FALLBACK_WINDOW = "gcp_primary_fallback_window"
    GCP_PRIMARY_STABLE = "gcp_primary_stable"


class CanonicalDomainEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    domain_name: str
    entity_type: str
    logical_entity_id: str
    tenant_id: str
    source_technology: SourceTechnology
    source_version: int = Field(ge=0)
    event_time_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    change_type: ChangeType = ChangeType.UPSERT
    fragment_owner: str
    payload: dict[str, Any]
    migration_stage: MigrationStage = MigrationStage.AZURE_PRIMARY
    trace_id: str = Field(default_factory=lambda: str(uuid4()))

    @field_validator("event_time_utc")
    @classmethod
    def ensure_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @property
    def ordering_key(self) -> str:
        return f"{self.tenant_id}|{self.domain_name}|{self.logical_entity_id}"
