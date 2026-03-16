from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field

from unified_modernization.contracts.events import CanonicalDomainEvent
from unified_modernization.contracts.projection import PublicationDecision
from unified_modernization.observability.telemetry import NoopTelemetrySink, TelemetryEvent, TelemetrySink
from unified_modernization.projection.builder import ProjectionBuilder


class DeadLetterRecord(BaseModel):
    event_id: str
    tenant_id: str
    domain_name: str
    entity_type: str
    logical_entity_id: str
    trace_id: str
    error_type: str
    error_message: str
    failed_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, object] = Field(default_factory=dict)


class DeadLetterQueue(Protocol):
    def publish(self, record: DeadLetterRecord) -> None:
        raise NotImplementedError


class InMemoryDeadLetterQueue:
    def __init__(self) -> None:
        self.records: list[DeadLetterRecord] = []

    def publish(self, record: DeadLetterRecord) -> None:
        self.records.append(record)


class BackpressureDecision(BaseModel):
    accepted: bool
    reason_code: str
    pending_documents: int


class BackpressureController:
    def __init__(self, *, max_pending_documents: int = 100_000) -> None:
        self._max_pending_documents = max_pending_documents

    def evaluate(self, pending_documents: int) -> BackpressureDecision:
        if pending_documents >= self._max_pending_documents:
            return BackpressureDecision(
                accepted=False,
                reason_code="pending_threshold_exceeded",
                pending_documents=pending_documents,
            )
        return BackpressureDecision(
            accepted=True,
            reason_code="accepted",
            pending_documents=pending_documents,
        )


class ProjectionRuntimeResult(BaseModel):
    accepted: bool
    decision: PublicationDecision | None = None
    throttled: bool = False
    dead_lettered: bool = False
    reason_code: str | None = None


class ProjectionRuntime:
    """Operational wrapper for backpressure and DLQ handling around projection mutation."""

    def __init__(
        self,
        projection_builder: ProjectionBuilder,
        *,
        dead_letter_queue: DeadLetterQueue | None = None,
        backpressure_controller: BackpressureController | None = None,
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        self._projection_builder = projection_builder
        self._dead_letter_queue = dead_letter_queue or InMemoryDeadLetterQueue()
        self._backpressure_controller = backpressure_controller or BackpressureController()
        self._telemetry_sink = telemetry_sink or NoopTelemetrySink()

    def process(
        self,
        event: CanonicalDomainEvent,
        *,
        now_utc: datetime | None = None,
    ) -> ProjectionRuntimeResult:
        pending_documents = self._projection_builder.pending_count()
        pressure = self._backpressure_controller.evaluate(pending_documents)
        if not pressure.accepted:
            self._telemetry_sink.increment(
                "projection.backpressure.rejected",
                tags={"domain_name": event.domain_name, "entity_type": event.entity_type},
            )
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type="projection_backpressure",
                    severity="warning",
                    trace_id=event.trace_id,
                    attributes={
                        "tenant_id": event.tenant_id,
                        "logical_entity_id": event.logical_entity_id,
                        "pending_documents": pending_documents,
                        "reason_code": pressure.reason_code,
                    },
                )
            )
            return ProjectionRuntimeResult(
                accepted=False,
                throttled=True,
                reason_code=pressure.reason_code,
            )

        try:
            decision = self._projection_builder.upsert(event, now_utc=now_utc)
        except Exception as exc:  # pragma: no cover - exercised through tests
            record = DeadLetterRecord(
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                domain_name=event.domain_name,
                entity_type=event.entity_type,
                logical_entity_id=event.logical_entity_id,
                trace_id=event.trace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                payload=event.model_dump(mode="json"),
            )
            self._dead_letter_queue.publish(record)
            self._telemetry_sink.increment(
                "projection.failed",
                tags={"domain_name": event.domain_name, "entity_type": event.entity_type},
            )
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type="projection_failed",
                    severity="error",
                    trace_id=event.trace_id,
                    attributes={
                        "tenant_id": event.tenant_id,
                        "logical_entity_id": event.logical_entity_id,
                        "error_type": type(exc).__name__,
                    },
                )
            )
            return ProjectionRuntimeResult(
                accepted=False,
                dead_lettered=True,
                reason_code="projection_failed",
            )

        metric_name = "projection.published" if decision.publish else "projection.pending"
        self._telemetry_sink.increment(
            metric_name,
            tags={"domain_name": event.domain_name, "entity_type": event.entity_type},
        )
        return ProjectionRuntimeResult(
            accepted=True,
            decision=decision,
            reason_code="processed",
        )
