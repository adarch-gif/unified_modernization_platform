from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import BaseModel

from unified_modernization.contracts.events import (
    CanonicalDomainEvent,
    ChangeType,
    MigrationStage,
    SourceTechnology,
)


class SpannerChangeStreamAdapterConfig(BaseModel):
    domain_name: str
    entity_type: str
    fragment_owner: str
    tenant_field: str = "tenant_id"
    logical_entity_id_field: str = "id"
    migration_stage: MigrationStage = MigrationStage.GCP_PRIMARY_STABLE


class SpannerChangeStreamAdapter:
    """Normalizes Spanner change stream records into canonical projection events."""

    def __init__(self, config: SpannerChangeStreamAdapterConfig) -> None:
        self._config = config

    def normalize(self, record: Mapping[str, object]) -> CanonicalDomainEvent:
        mod_type = _require_str(record.get("mod_type"), field_name="mod_type").upper()
        keys = _require_mapping(record.get("keys"), field_name="keys")
        new_values = record.get("new_values")
        old_values = record.get("old_values")
        if mod_type == "DELETE":
            data = _mapping_or_empty(old_values) or keys
            change_type = ChangeType.DELETE
        else:
            data = _mapping_or_empty(new_values)
            if not data:
                raise ValueError("Spanner change record must contain new_values for non-delete operations")
            change_type = ChangeType.UPSERT

        tenant_id = _require_str(
            data.get(self._config.tenant_field, keys.get(self._config.tenant_field)),
            field_name=self._config.tenant_field,
        )
        logical_entity_id = _require_str(
            data.get(self._config.logical_entity_id_field, keys.get(self._config.logical_entity_id_field)),
            field_name=self._config.logical_entity_id_field,
        )
        source_version = _parse_record_sequence(record.get("record_sequence"))
        event_time = _parse_commit_timestamp(record.get("commit_timestamp"))

        return CanonicalDomainEvent(
            domain_name=self._config.domain_name,
            entity_type=self._config.entity_type,
            logical_entity_id=logical_entity_id,
            tenant_id=tenant_id,
            source_technology=SourceTechnology.SPANNER,
            source_version=source_version,
            event_time_utc=event_time,
            change_type=change_type,
            fragment_owner=self._config.fragment_owner,
            payload=dict(data),
            migration_stage=self._config.migration_stage,
        )


def _require_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _mapping_or_empty(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _require_str(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _parse_record_sequence(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        digits = "".join(char for char in value if char.isdigit())
        if digits:
            return int(digits)
    raise ValueError("record_sequence must be numeric or contain digits")


def _parse_commit_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    raise ValueError("commit_timestamp must be a datetime-compatible value")
