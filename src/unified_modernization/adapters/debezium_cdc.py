from __future__ import annotations

import logging
import threading
from copy import deepcopy
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pydantic import BaseModel

from unified_modernization.contracts.events import (
    CanonicalDomainEvent,
    ChangeType,
    MigrationStage,
    SourceTechnology,
)

_LOGGER = logging.getLogger(__name__)


class DebeziumLSNFormat(StrEnum):
    AUTO = "auto"
    DECIMAL = "decimal"
    POSTGRES_LSN = "postgres_lsn"
    SQLSERVER_LSN = "sqlserver_lsn"


class DebeziumChangeEventAdapterConfig(BaseModel):
    domain_name: str
    entity_type: str
    fragment_owner: str
    source_technology: SourceTechnology = SourceTechnology.AZURE_SQL
    tenant_field: str = "tenant_id"
    logical_entity_id_field: str = "id"
    source_version_field: str | None = "lsn"
    lsn_format: DebeziumLSNFormat = DebeziumLSNFormat.AUTO
    allow_timestamp_source_version_fallback: bool = False
    migration_stage: MigrationStage = MigrationStage.AZURE_PRIMARY


class DebeziumChangeEventAdapter:
    """Normalizes Debezium-style CDC envelopes into canonical projection events."""

    def __init__(self, config: DebeziumChangeEventAdapterConfig) -> None:
        self._config = config
        self._timestamp_fallback_warned = threading.Event()

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

        source_version, used_timestamp_fallback = _parse_source_version(
            source=source_section,
            payload=payload,
            preferred_field=self._config.source_version_field,
            lsn_format=self._config.lsn_format,
            allow_timestamp_fallback=self._config.allow_timestamp_source_version_fallback,
        )
        if used_timestamp_fallback and not self._timestamp_fallback_warned.is_set():
            _LOGGER.warning(
                "Debezium adapter is using timestamp fallback for source version; configure a native source version field",
                extra={
                    "domain_name": self._config.domain_name,
                    "entity_type": self._config.entity_type,
                    "fragment_owner": self._config.fragment_owner,
                },
            )
            self._timestamp_fallback_warned.set()
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
            payload=deepcopy(dict(raw_document)),
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
    if normalized == "t":
        raise ValueError("Debezium TRUNCATE events must be handled at the pipeline level")
    return ChangeType.UPSERT


def _parse_source_version(
    *,
    source: Mapping[str, object],
    payload: Mapping[str, object],
    preferred_field: str | None,
    lsn_format: DebeziumLSNFormat,
    allow_timestamp_fallback: bool,
) -> tuple[int, bool]:
    if preferred_field is not None:
        candidate = source.get(preferred_field)
        if isinstance(candidate, int):
            return candidate, False
        if isinstance(candidate, str) and candidate.strip():
            return _parse_string_source_version(candidate, lsn_format=lsn_format), False

    if not allow_timestamp_fallback:
        raise ValueError("Debezium record must contain a native numeric source version")

    fallback = _parse_timestamp_source_version(payload=payload, source=source)
    if fallback is None:
        raise ValueError("Debezium record must contain a numeric source version")
    return fallback, True


def _parse_string_source_version(value: str, *, lsn_format: DebeziumLSNFormat) -> int:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Debezium source version string must be non-empty")

    if lsn_format in {DebeziumLSNFormat.AUTO, DebeziumLSNFormat.POSTGRES_LSN} and "/" in normalized:
        return _parse_postgres_lsn(normalized)
    if lsn_format in {DebeziumLSNFormat.AUTO, DebeziumLSNFormat.SQLSERVER_LSN} and ":" in normalized:
        return _parse_sqlserver_lsn(normalized)
    if lsn_format in {DebeziumLSNFormat.AUTO, DebeziumLSNFormat.DECIMAL}:
        if normalized.isdigit():
            return int(normalized)
        if lsn_format == DebeziumLSNFormat.AUTO:
            raise ValueError(f"unsupported Debezium source version format {value!r}")
    raise ValueError(f"unsupported Debezium source version format {value!r}")


def _parse_postgres_lsn(value: str) -> int:
    parts = value.split("/")
    if len(parts) != 2 or not all(part for part in parts):
        raise ValueError(f"invalid PostgreSQL LSN {value!r}")
    return (int(parts[0], 16) << 32) | int(parts[1], 16)


def _parse_sqlserver_lsn(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 3 or not all(part for part in parts):
        raise ValueError(f"invalid SQL Server LSN {value!r}")
    first, second, third = (int(part, 16) for part in parts)
    return (first << 48) | (second << 16) | third


def _parse_timestamp_source_version(
    *,
    payload: Mapping[str, object],
    source: Mapping[str, object],
) -> int | None:
    for fallback_field, multiplier in (("ts_ns", 1), ("ts_us", 1000), ("ts_ms", 1_000_000)):
        candidate = payload.get(fallback_field)
        if candidate is None:
            candidate = source.get(fallback_field)
        if isinstance(candidate, int):
            return candidate * multiplier
        if isinstance(candidate, str) and candidate.strip():
            return int(candidate) * multiplier
    return None


def _parse_debezium_event_time(payload: Mapping[str, object]) -> datetime:
    raw_time = payload.get("ts_ms")
    if isinstance(raw_time, int):
        return datetime.fromtimestamp(raw_time / 1000, tz=UTC)
    if isinstance(raw_time, str) and raw_time.strip():
        return datetime.fromtimestamp(int(raw_time) / 1000, tz=UTC)
    raise ValueError("Debezium record missing ts_ms; cannot determine event time")
