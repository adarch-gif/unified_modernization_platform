from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from unified_modernization.contracts.events import (
    CanonicalDomainEvent,
    ChangeType,
    MigrationStage,
    SourceTechnology,
)


class CosmosChangeFeedAdapterConfig(BaseModel):
    domain_name: str
    entity_type: str
    fragment_owner: str
    tenant_field: str = "tenantId"
    logical_entity_id_field: str = "id"
    source_version_field: str = "_lsn"
    event_time_field: str = "_ts"
    change_type_field: str = "operationType"
    migration_stage: MigrationStage = MigrationStage.AZURE_PRIMARY


class CosmosChangeFeedAdapter:
    """Normalizes Cosmos DB change feed records into canonical projection events."""

    def __init__(self, config: CosmosChangeFeedAdapterConfig) -> None:
        self._config = config

    def normalize(self, record: Mapping[str, object]) -> CanonicalDomainEvent:
        payload = dict(record)
        tenant_id = _require_str(payload.get(self._config.tenant_field), field_name=self._config.tenant_field)
        logical_entity_id = _require_str(
            payload.get(self._config.logical_entity_id_field),
            field_name=self._config.logical_entity_id_field,
        )
        source_version = _parse_int(payload.get(self._config.source_version_field), field_name=self._config.source_version_field)
        event_time = _parse_datetime(payload.get(self._config.event_time_field), field_name=self._config.event_time_field)
        change_type = _parse_cosmos_change_type(payload.get(self._config.change_type_field))
        document_payload = _strip_system_fields(
            payload,
            excluded_fields={
                self._config.source_version_field,
                self._config.event_time_field,
                self._config.change_type_field,
            },
        )
        return CanonicalDomainEvent(
            domain_name=self._config.domain_name,
            entity_type=self._config.entity_type,
            logical_entity_id=logical_entity_id,
            tenant_id=tenant_id,
            source_technology=SourceTechnology.COSMOS,
            source_version=source_version,
            event_time_utc=event_time,
            change_type=change_type,
            fragment_owner=self._config.fragment_owner,
            payload=document_payload,
            migration_stage=self._config.migration_stage,
        )


def _strip_system_fields(record: Mapping[str, Any], *, excluded_fields: set[str]) -> dict[str, Any]:
    system_prefixes = {"_", "_attachments"}
    return {
        key: value
        for key, value in record.items()
        if key not in excluded_fields and not any(key.startswith(prefix) for prefix in system_prefixes)
    }


def _require_str(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _parse_int(value: object, *, field_name: str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value)
    raise ValueError(f"{field_name} must be an integer")


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    raise ValueError(f"{field_name} must be a datetime-compatible value")


def _parse_cosmos_change_type(value: object) -> ChangeType:
    if not isinstance(value, str):
        return ChangeType.UPSERT
    normalized = value.strip().lower()
    if normalized in {"delete", "deleted", "remove"}:
        return ChangeType.DELETE
    if normalized in {"repair", "refresh"}:
        return ChangeType(normalized)
    return ChangeType.UPSERT
