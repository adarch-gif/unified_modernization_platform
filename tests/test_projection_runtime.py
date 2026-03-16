import asyncio
from datetime import UTC, datetime

from unified_modernization.contracts.events import CanonicalDomainEvent, ChangeType, SourceTechnology
from unified_modernization.contracts.projection import DependencyPolicy, DependencyRule, ProjectionStatus
from unified_modernization.observability.telemetry import InMemoryTelemetrySink
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.projection.runtime import (
    BackpressureController,
    InMemoryDeadLetterQueue,
    ProjectionRuntime,
)
from unified_modernization.projection.store import InMemoryProjectionStateStore


def test_projection_runtime_throttles_when_pending_threshold_is_exceeded() -> None:
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[
                    DependencyRule(owner="document_core", required=True),
                    DependencyRule(owner="customer_profile", required=True),
                ],
            )
        ],
        state_store=InMemoryProjectionStateStore(),
    )
    runtime = ProjectionRuntime(
        builder,
        backpressure_controller=BackpressureController(max_pending_documents=0),
        telemetry_sink=InMemoryTelemetrySink(),
    )
    event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-1",
        tenant_id="tenant-a",
        source_technology=SourceTechnology.COSMOS,
        source_version=1,
        fragment_owner="document_core",
        payload={"title": "First"},
        event_time_utc=datetime.now(UTC),
    )

    result = runtime.process(event)

    assert result.accepted is False
    assert result.throttled is True
    assert result.reason_code == "pending_threshold_exceeded"


def test_projection_runtime_dead_letters_failed_mutation() -> None:
    class _FailingBuilder:
        def pending_count(self) -> int:
            return 0

        def upsert(self, event: CanonicalDomainEvent, now_utc: datetime | None = None) -> None:
            del event, now_utc
            raise RuntimeError("boom")

    dead_letter_queue = InMemoryDeadLetterQueue()
    runtime = ProjectionRuntime(
        _FailingBuilder(),  # type: ignore[arg-type]
        dead_letter_queue=dead_letter_queue,
        telemetry_sink=InMemoryTelemetrySink(),
    )
    event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-2",
        tenant_id="tenant-a",
        source_technology=SourceTechnology.COSMOS,
        source_version=1,
        fragment_owner="document_core",
        payload={"title": "First"},
    )

    result = runtime.process(event)

    assert result.accepted is False
    assert result.dead_lettered is True
    assert len(dead_letter_queue.records) == 1
    assert dead_letter_queue.records[0].logical_entity_id == "doc-2"


def test_projection_runtime_quarantines_entity_when_builder_mutation_fails() -> None:
    telemetry = InMemoryTelemetrySink()
    builder = ProjectionBuilder(
        [],
        state_store=InMemoryProjectionStateStore(),
        telemetry_sink=telemetry,
    )
    dead_letter_queue = InMemoryDeadLetterQueue()
    runtime = ProjectionRuntime(
        builder,
        dead_letter_queue=dead_letter_queue,
        telemetry_sink=telemetry,
    )
    event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="unknownDocument",
        logical_entity_id="doc-quarantine",
        tenant_id="tenant-a",
        source_technology=SourceTechnology.COSMOS,
        source_version=1,
        fragment_owner="document_core",
        payload={"title": "bad"},
    )

    result = runtime.process(event)
    state = builder.get_state("tenant-a", "customer_documents", "unknownDocument", "doc-quarantine")

    assert result.accepted is False
    assert result.dead_lettered is True
    assert state is not None
    assert state.status == ProjectionStatus.QUARANTINED
    assert state.quarantine_reason is not None
    assert "no dependency policy configured" in state.quarantine_reason
    assert telemetry.counters[("projection.quarantined", (("domain_name", "customer_documents"), ("entity_type", "unknownDocument")))] == 1


def test_projection_runtime_allows_pending_entity_completion_under_backpressure() -> None:
    telemetry = InMemoryTelemetrySink()
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[
                    DependencyRule(owner="document_core", required=True),
                    DependencyRule(owner="customer_profile", required=True),
                ],
            )
        ],
        state_store=InMemoryProjectionStateStore(),
    )
    now = datetime.now(UTC)
    builder.upsert(
        CanonicalDomainEvent(
            domain_name="customer_documents",
            entity_type="customerDocument",
            logical_entity_id="doc-3",
            tenant_id="tenant-a",
            source_technology=SourceTechnology.COSMOS,
            source_version=1,
            fragment_owner="document_core",
            payload={"title": "First"},
            event_time_utc=now,
        ),
        now_utc=now,
    )

    runtime = ProjectionRuntime(
        builder,
        backpressure_controller=BackpressureController(max_pending_documents=1),
        telemetry_sink=telemetry,
    )
    result = runtime.process(
        CanonicalDomainEvent(
            domain_name="customer_documents",
            entity_type="customerDocument",
            logical_entity_id="doc-3",
            tenant_id="tenant-a",
            source_technology=SourceTechnology.ALLOYDB,
            source_version=2,
            fragment_owner="customer_profile",
            payload={"customerName": "Apurva"},
            event_time_utc=now,
        ),
        now_utc=now,
    )

    assert result.accepted is True
    assert result.decision is not None
    assert result.decision.publish is True
    assert telemetry.counters[
        (
            "projection.backpressure.bypassed",
            (("domain_name", "customer_documents"), ("entity_type", "customerDocument"), ("reason", "pending_entity_completion")),
        )
    ] == 1


def test_projection_runtime_allows_repair_events_under_backpressure() -> None:
    telemetry = InMemoryTelemetrySink()
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ],
        state_store=InMemoryProjectionStateStore(),
    )
    runtime = ProjectionRuntime(
        builder,
        backpressure_controller=BackpressureController(max_pending_documents=0),
        telemetry_sink=telemetry,
    )

    result = runtime.process(
        CanonicalDomainEvent(
            domain_name="customer_documents",
            entity_type="customerDocument",
            logical_entity_id="doc-4",
            tenant_id="tenant-a",
            source_technology=SourceTechnology.COSMOS,
            source_version=1,
            change_type=ChangeType.REPAIR,
            fragment_owner="document_core",
            payload={"title": "Recovered"},
            event_time_utc=datetime.now(UTC),
        )
    )

    assert result.accepted is True
    assert telemetry.counters[
        (
            "projection.backpressure.bypassed",
            (("domain_name", "customer_documents"), ("entity_type", "customerDocument"), ("reason", "priority_change_type")),
        )
    ] == 1


def test_projection_runtime_process_async_uses_async_publisher() -> None:
    class _AsyncPublisher:
        def __init__(self) -> None:
            self.published: list[str] = []

        async def publish_async(self, document: object, *, trace_id: str | None = None) -> dict[str, object]:
            del trace_id
            self.published.append(document.document_id)  # type: ignore[attr-defined]
            return {"result": "created"}

    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ],
        state_store=InMemoryProjectionStateStore(),
    )
    runtime = ProjectionRuntime(
        builder,
        document_publisher=_AsyncPublisher(),  # type: ignore[arg-type]
        telemetry_sink=InMemoryTelemetrySink(),
    )

    result = asyncio.run(
        runtime.process_async(
            CanonicalDomainEvent(
                domain_name="customer_documents",
                entity_type="customerDocument",
                logical_entity_id="doc-async-runtime",
                tenant_id="tenant-a",
                source_technology=SourceTechnology.COSMOS,
                source_version=1,
                fragment_owner="document_core",
                payload={"title": "Async runtime"},
                event_time_utc=datetime.now(UTC),
            )
        )
    )

    assert result.accepted is True
    assert result.publish_result == {"result": "created"}
