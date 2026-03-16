from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field

from unified_modernization.contracts.events import CanonicalDomainEvent, ChangeType
from unified_modernization.contracts.projection import ProjectionStatus, PublicationDecision
from unified_modernization.observability.telemetry import NoopTelemetrySink, TelemetryEvent, TelemetrySink
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.projection.publisher import SearchDocumentPublisher


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
    publish_result: dict[str, object] | None = None
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
        document_publisher: SearchDocumentPublisher | None = None,
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        self._projection_builder = projection_builder
        self._dead_letter_queue = dead_letter_queue or InMemoryDeadLetterQueue()
        self._backpressure_controller = backpressure_controller or BackpressureController()
        self._document_publisher = document_publisher
        self._telemetry_sink = telemetry_sink or NoopTelemetrySink()

    def process(
        self,
        event: CanonicalDomainEvent,
        *,
        now_utc: datetime | None = None,
    ) -> ProjectionRuntimeResult:
        pending_documents = self._projection_builder.pending_count()
        pressure = self._backpressure_controller.evaluate(pending_documents)
        backpressure_result = self._handle_backpressure(
            event,
            pending_documents=pending_documents,
            pressure=pressure,
            bypass_reason=None if pressure.accepted else self._backpressure_bypass_reason(event),
        )
        if backpressure_result is not None:
            return backpressure_result

        try:
            decision = self._projection_builder.upsert(event, now_utc=now_utc)
        except Exception as exc:  # pragma: no cover - exercised through tests
            self._quarantine_failed_entity(event, exc, now_utc=now_utc)
            return self._projection_failure_result(event, exc)

        publish_result: dict[str, object] | None = None
        if decision.publish and decision.document is not None and self._document_publisher is not None:
            try:
                publish_result = self._document_publisher.publish(
                    decision.document,
                    trace_id=event.trace_id,
                )
            except Exception as exc:  # pragma: no cover - exercised through tests
                return self._publish_failure_result(event, decision, exc)

        return self._processed_result(event, decision, publish_result)

    async def process_async(
        self,
        event: CanonicalDomainEvent,
        *,
        now_utc: datetime | None = None,
    ) -> ProjectionRuntimeResult:
        pending_documents = await asyncio.to_thread(self._projection_builder.pending_count)
        pressure = self._backpressure_controller.evaluate(pending_documents)
        bypass_reason = None
        if not pressure.accepted:
            bypass_reason = await self._backpressure_bypass_reason_async(event)
        backpressure_result = self._handle_backpressure(
            event,
            pending_documents=pending_documents,
            pressure=pressure,
            bypass_reason=bypass_reason,
        )
        if backpressure_result is not None:
            return backpressure_result

        try:
            decision = await asyncio.to_thread(self._projection_builder.upsert, event, now_utc=now_utc)
        except Exception as exc:  # pragma: no cover - exercised through tests
            await self._quarantine_failed_entity_async(event, exc, now_utc=now_utc)
            return self._projection_failure_result(event, exc)

        publish_result: dict[str, object] | None = None
        if decision.publish and decision.document is not None and self._document_publisher is not None:
            try:
                publish_result = await self._publish_document_async(
                    decision.document,
                    trace_id=event.trace_id,
                )
            except Exception as exc:  # pragma: no cover - exercised through tests
                return self._publish_failure_result(event, decision, exc)

        return self._processed_result(event, decision, publish_result)

    def _handle_backpressure(
        self,
        event: CanonicalDomainEvent,
        *,
        pending_documents: int,
        pressure: BackpressureDecision,
        bypass_reason: str | None,
    ) -> ProjectionRuntimeResult | None:
        if pressure.accepted:
            return None
        if bypass_reason is not None:
            self._telemetry_sink.increment(
                "projection.backpressure.bypassed",
                tags={"domain_name": event.domain_name, "entity_type": event.entity_type, "reason": bypass_reason},
            )
            return None
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

    def _projection_failure_result(
        self,
        event: CanonicalDomainEvent,
        exc: Exception,
    ) -> ProjectionRuntimeResult:
        self._dead_letter_queue.publish(
            DeadLetterRecord(
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
        )
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

    def _publish_failure_result(
        self,
        event: CanonicalDomainEvent,
        decision: PublicationDecision,
        exc: Exception,
    ) -> ProjectionRuntimeResult:
        document = decision.document
        if document is None:
            raise ValueError("publish failure requires a document")
        self._dead_letter_queue.publish(
            DeadLetterRecord(
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                domain_name=event.domain_name,
                entity_type=event.entity_type,
                logical_entity_id=event.logical_entity_id,
                trace_id=event.trace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                payload={
                    "event": event.model_dump(mode="json"),
                    "document": document.model_dump(mode="json"),
                },
            )
        )
        self._telemetry_sink.increment(
            "projection.publish_failed",
            tags={"domain_name": event.domain_name, "entity_type": event.entity_type},
        )
        self._telemetry_sink.emit(
            TelemetryEvent(
                event_type="projection_publish_failed",
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
            decision=decision,
            dead_lettered=True,
            reason_code="publish_failed",
        )

    def _processed_result(
        self,
        event: CanonicalDomainEvent,
        decision: PublicationDecision,
        publish_result: dict[str, object] | None,
    ) -> ProjectionRuntimeResult:
        metric_name = "projection.published" if decision.publish else "projection.pending"
        self._telemetry_sink.increment(
            metric_name,
            tags={"domain_name": event.domain_name, "entity_type": event.entity_type},
        )
        return ProjectionRuntimeResult(
            accepted=True,
            decision=decision,
            publish_result=publish_result,
            reason_code="processed",
        )

    def _backpressure_bypass_reason(self, event: CanonicalDomainEvent) -> str | None:
        return self._backpressure_bypass_reason_from_state(
            event,
            self._projection_builder.get_state(
                event.tenant_id,
                event.domain_name,
                event.entity_type,
                event.logical_entity_id,
            ),
        )

    async def _backpressure_bypass_reason_async(self, event: CanonicalDomainEvent) -> str | None:
        if event.change_type in {ChangeType.REPAIR, ChangeType.REFRESH}:
            return "priority_change_type"
        state = await asyncio.to_thread(
            self._projection_builder.get_state,
            event.tenant_id,
            event.domain_name,
            event.entity_type,
            event.logical_entity_id,
        )
        return self._backpressure_bypass_reason_from_state(event, state)

    @staticmethod
    def _backpressure_bypass_reason_from_state(
        event: CanonicalDomainEvent,
        state: ProjectionStatus | Any | None,
    ) -> str | None:
        if event.change_type in {ChangeType.REPAIR, ChangeType.REFRESH}:
            return "priority_change_type"
        if state is None:
            return None
        state_status = getattr(state, "status", None)
        if state_status in {
            ProjectionStatus.PENDING_REQUIRED_FRAGMENT,
            ProjectionStatus.PENDING_REHYDRATION,
            ProjectionStatus.STALE,
            ProjectionStatus.QUARANTINED,
            ProjectionStatus.READY_TO_BUILD,
        }:
            return "pending_entity_completion"
        return None

    async def _publish_document_async(
        self,
        document: Any,
        *,
        trace_id: str | None,
    ) -> dict[str, object]:
        publisher = self._document_publisher
        if publisher is None:
            raise RuntimeError("document publisher is not configured")
        publish_async = getattr(publisher, "publish_async", None)
        if callable(publish_async):
            result = await cast(Any, publish_async)(document, trace_id=trace_id)
            return cast(dict[str, object], result)
        result = await asyncio.to_thread(publisher.publish, document, trace_id=trace_id)
        return cast(dict[str, object], result)

    def _quarantine_failed_entity(
        self,
        event: CanonicalDomainEvent,
        exc: Exception,
        *,
        now_utc: datetime | None,
    ) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        try:
            self._projection_builder.quarantine(
                event.tenant_id,
                event.domain_name,
                event.entity_type,
                event.logical_entity_id,
                reason=reason,
                now_utc=now_utc,
                trace_id=event.trace_id,
            )
        except Exception:
            self._telemetry_sink.increment(
                "projection.quarantine_failed",
                tags={"domain_name": event.domain_name, "entity_type": event.entity_type},
            )

    async def _quarantine_failed_entity_async(
        self,
        event: CanonicalDomainEvent,
        exc: Exception,
        *,
        now_utc: datetime | None,
    ) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        try:
            await asyncio.to_thread(
                self._projection_builder.quarantine,
                event.tenant_id,
                event.domain_name,
                event.entity_type,
                event.logical_entity_id,
                reason=reason,
                now_utc=now_utc,
                trace_id=event.trace_id,
            )
        except Exception:
            self._telemetry_sink.increment(
                "projection.quarantine_failed",
                tags={"domain_name": event.domain_name, "entity_type": event.entity_type},
            )
