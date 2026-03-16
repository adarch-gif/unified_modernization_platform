from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from unified_modernization.contracts.events import (
    CanonicalDomainEvent,
    ChangeType,
    MigrationStage,
    SourceTechnology,
)


class FirestoreOutboxRecord(BaseModel):
    tenant_id: str
    domain_name: str
    entity_type: str
    logical_entity_id: str
    fragment_owner: str
    source_version: int = Field(ge=0)
    payload: dict[str, Any]
    change_type: ChangeType = ChangeType.UPSERT
    event_time_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    migration_stage: MigrationStage = MigrationStage.GCP_PRIMARY_STABLE


def normalize_outbox_record(record: FirestoreOutboxRecord) -> CanonicalDomainEvent:
    return CanonicalDomainEvent(
        domain_name=record.domain_name,
        entity_type=record.entity_type,
        logical_entity_id=record.logical_entity_id,
        tenant_id=record.tenant_id,
        source_technology=SourceTechnology.FIRESTORE,
        source_version=record.source_version,
        event_time_utc=record.event_time_utc,
        change_type=record.change_type,
        fragment_owner=record.fragment_owner,
        payload=record.payload,
        migration_stage=record.migration_stage,
    )
