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


class DebeziumChangeEventAdapterConfig(BaseModel):
    domain_name: str
    entity_type: str
    fragment_owner: str
    source_technology: SourceTechnology = SourceTechnology.AZURE_SQL
    tenant_field: str = "tenant_id"
    logical_entity_id_field: str = "id"
    source_version_field: str | None = "lsn"
    migration_stage: MigrationStage = MigrationStage.AZURE_PRIMARY


class DebeziumChangeEventAdapter:
    """Normalizes Debezium-style CDC envelopes into canonical projection events."""

    def __init__(self, config: DebeziumChangeEventAdapterConfig) -> None:
        self._config = config

    def normalize(self, record: Mapping[str, object]) -> CanonicalDomainEvent:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("Debezium record must contain a payload mapping")

        op = _require_str(payload.get("op"), field_name="payload.op")
        change_type = _parse_debezium_change_type(op)
        raw_document = payload.get("before") if change_type == ChangeType.DELETE else payload.get("after")
        if not isinstance(raw_document, Mapping):
            raise ValueError("Debezium record must contain a before/after mapping for the change")

        tenant_id = _require_str(raw_document.get(self._config.tenant_field), field_name=self._config.tenant_field)
        logical_entity_id = _require_str(
            raw_document.get(self._config.logical_entity_id_field),
            field_name=self._config.logical_entity_id_field,
        )
        source_section = payload.get("source", {})
        if not isinstance(source_section, Mapping):
            source_section = {}

        source_version = _parse_source_version(
            source=source_section,
            payload=payload,
            preferred_field=self._config.source_version_field,
        )
        event_time = _parse_debezium_event_time(payload)

        return CanonicalDomainEvent(
            domain_name=self._config.domain_name,
            entity_type=self._config.entity_type,
            logical_entity_id=logical_entity_id,
            tenant_id=tenant_id,
            source_technology=self._config.source_technology,
            source_version=source_version,
            event_time_utc=event_time,
            change_type=change_type,
            fragment_owner=self._config.fragment_owner,
            payload=dict(raw_document),
            migration_stage=self._config.migration_stage,
        )


def _require_str(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _parse_debezium_change_type(value: str) -> ChangeType:
    normalized = value.lower()
    if normalized == "d":
        return ChangeType.DELETE
    if normalized == "r":
        return ChangeType.REFRESH
    return ChangeType.UPSERT


def _parse_source_version(
    *,
    source: Mapping[str, object],
    payload: Mapping[str, object],
    preferred_field: str | None,
) -> int:
    if preferred_field is not None:
        candidate = source.get(preferred_field)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            digits = "".join(char for char in candidate if char.isdigit())
            if digits:
                return int(digits)

    for fallback_field in ("ts_ms", "ts_us", "ts_ns"):
        candidate = payload.get(fallback_field) or source.get(fallback_field)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            return int(candidate)

    raise ValueError("Debezium record must contain a numeric source version")


def _parse_debezium_event_time(payload: Mapping[str, object]) -> datetime:
    raw_time = payload.get("ts_ms")
    if isinstance(raw_time, int):
        return datetime.fromtimestamp(raw_time / 1000, tz=UTC)
    if isinstance(raw_time, str) and raw_time.strip():
        return datetime.fromtimestamp(int(raw_time) / 1000, tz=UTC)
    return datetime.now(UTC)
