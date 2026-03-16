from datetime import UTC, datetime

from unified_modernization.backfill.coordinator import BackfillCoordinator, SourceWatermark
from unified_modernization.contracts.events import CanonicalDomainEvent, SourceTechnology
from unified_modernization.contracts.projection import DependencyPolicy, DependencyRule
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.projection.store import InMemoryProjectionStateStore


def test_backfill_side_load_returns_stream_handoff_plan() -> None:
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
    coordinator = BackfillCoordinator(builder)
    now = datetime.now(UTC)
    events = [
        CanonicalDomainEvent(
            domain_name="customer_documents",
            entity_type="customerDocument",
            logical_entity_id="doc-1",
            tenant_id="tenant-a",
            source_technology=SourceTechnology.COSMOS,
            source_version=1,
            fragment_owner="document_core",
            payload={"title": "First"},
            event_time_utc=now,
        )
    ]

    result = coordinator.side_load(
        events,
        captured_watermarks=[SourceWatermark(source_name="cosmos", position="12345")],
        now_utc=now,
    )

    assert result.summary.ingested_events == 1
    assert result.summary.pending_documents == 1
    assert result.handoff_plan.captured_watermarks[0].position == "12345"
