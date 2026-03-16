from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class TelemetryEvent(BaseModel):
    event_type: str
    severity: str = "info"
    trace_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TelemetryCounter(BaseModel):
    name: str
    value: int = 0
    tags: dict[str, str] = Field(default_factory=dict)


class TelemetryTiming(BaseModel):
    name: str
    duration_ms: float
    tags: dict[str, str] = Field(default_factory=dict)
    trace_id: str | None = None


class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent | Mapping[str, Any]) -> None:
        raise NotImplementedError

    def increment(self, name: str, value: int = 1, *, tags: Mapping[str, str] | None = None) -> None:
        raise NotImplementedError

    def record_timing(
        self,
        name: str,
        duration_ms: float,
        *,
        tags: Mapping[str, str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> "TelemetrySpan":
        raise NotImplementedError


class TelemetrySpan:
    def __init__(
        self,
        sink: TelemetrySink,
        name: str,
        *,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self._sink = sink
        self._name = name
        self._trace_id = trace_id or str(uuid4())
        self._attributes = dict(attributes or {})
        self._started_at = 0.0

    def __enter__(self) -> "TelemetrySpan":
        self._started_at = monotonic()
        self._sink.emit(
            TelemetryEvent(
                event_type=f"{self._name}.start",
                trace_id=self._trace_id,
                attributes=self._attributes,
            )
        )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        duration_ms = (monotonic() - self._started_at) * 1000
        tags = {key: str(value) for key, value in self._attributes.items()}
        self._sink.record_timing(self._name, duration_ms, tags=tags, trace_id=self._trace_id)
        severity = "error" if exc is not None else "info"
        completion_attributes = dict(self._attributes)
        completion_attributes["outcome"] = "error" if exc is not None else "ok"
        if exc is not None:
            completion_attributes["error_type"] = type(exc).__name__
        self._sink.emit(
            TelemetryEvent(
                event_type=f"{self._name}.finish",
                severity=severity,
                trace_id=self._trace_id,
                attributes=completion_attributes,
            )
        )
        return False

    @property
    def trace_id(self) -> str:
        return self._trace_id


class NoopTelemetrySink:
    def emit(self, event: TelemetryEvent | Mapping[str, Any]) -> None:
        del event

    def increment(self, name: str, value: int = 1, *, tags: Mapping[str, str] | None = None) -> None:
        del name, value, tags

    def record_timing(
        self,
        name: str,
        duration_ms: float,
        *,
        tags: Mapping[str, str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        del name, duration_ms, tags, trace_id

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TelemetrySpan:
        return TelemetrySpan(self, name, trace_id=trace_id, attributes=attributes)


class InMemoryTelemetrySink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self.timings: list[TelemetryTiming] = []

    def emit(self, event: TelemetryEvent | Mapping[str, Any]) -> None:
        normalized = self._normalize_event(event)
        self.events.append(normalized.model_dump())

    def increment(self, name: str, value: int = 1, *, tags: Mapping[str, str] | None = None) -> None:
        normalized_tags = tuple(sorted((tags or {}).items()))
        self.counters[(name, normalized_tags)] += value

    def record_timing(
        self,
        name: str,
        duration_ms: float,
        *,
        tags: Mapping[str, str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.timings.append(
            TelemetryTiming(
                name=name,
                duration_ms=duration_ms,
                tags=dict(tags or {}),
                trace_id=trace_id,
            )
        )

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TelemetrySpan:
        return TelemetrySpan(self, name, trace_id=trace_id, attributes=attributes)

    @staticmethod
    def _normalize_event(event: TelemetryEvent | Mapping[str, Any]) -> TelemetryEvent:
        if isinstance(event, TelemetryEvent):
            return event
        payload = dict(event)
        event_type = str(payload.pop("event_type", payload.pop("name", "telemetry_event")))
        severity = str(payload.pop("severity", "info"))
        trace_id = payload.pop("trace_id", None)
        attributes = payload.pop("attributes", None)
        if attributes is None:
            attributes = payload
        return TelemetryEvent(
            event_type=event_type,
            severity=severity,
            trace_id=None if trace_id is None else str(trace_id),
            attributes=dict(attributes),
        )


class StructuredLoggerTelemetrySink:
    _SEVERITY_MAP = {
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
    }

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("unified_modernization.telemetry")

    def emit(self, event: TelemetryEvent | Mapping[str, Any]) -> None:
        normalized = InMemoryTelemetrySink._normalize_event(event)
        payload = normalized.model_dump()
        level = self._SEVERITY_MAP.get(normalized.severity.lower(), logging.INFO)
        self._logger.log(
            level,
            "telemetry %s",
            normalized.model_dump_json(),
            extra={"json_fields": payload},
        )

    def increment(self, name: str, value: int = 1, *, tags: Mapping[str, str] | None = None) -> None:
        self.emit(
            TelemetryEvent(
                event_type="metric.counter",
                attributes={"name": name, "value": value, "tags": dict(tags or {})},
            )
        )

    def record_timing(
        self,
        name: str,
        duration_ms: float,
        *,
        tags: Mapping[str, str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.emit(
            TelemetryEvent(
                event_type="metric.timing",
                trace_id=trace_id,
                attributes={"name": name, "duration_ms": duration_ms, "tags": dict(tags or {})},
            )
        )

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TelemetrySpan:
        return TelemetrySpan(self, name, trace_id=trace_id, attributes=attributes)
