from datetime import UTC, datetime, timedelta

from unified_modernization.contracts.events import CanonicalDomainEvent, SourceTechnology
from unified_modernization.contracts.projection import DependencyPolicy, DependencyRule, ProjectionStatus
from unified_modernization.projection.builder import ProjectionBuilder


def test_projection_waits_for_required_fragment() -> None:
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[
                    DependencyRule(owner="document_core", required=True, freshness_ttl_seconds=300),
                    DependencyRule(owner="customer_profile", required=True, freshness_ttl_seconds=300),
                ],
            )
        ]
    )
    event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-1",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=1,
        fragment_owner="document_core",
        payload={"title": "First"},
    )

    decision = builder.upsert(event)

    assert decision.publish is False
    assert decision.state.status == ProjectionStatus.PENDING_REQUIRED_FRAGMENT
    assert decision.state.missing_required_fragments == ["customer_profile"]


def test_projection_publishes_when_required_fragments_arrive() -> None:
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[
                    DependencyRule(owner="document_core", required=True, freshness_ttl_seconds=300),
                    DependencyRule(owner="customer_profile", required=True, freshness_ttl_seconds=300),
                ],
            )
        ]
    )
    now = datetime.now(UTC)
    events = [
        CanonicalDomainEvent(
            domain_name="customer_documents",
            entity_type="customerDocument",
            logical_entity_id="doc-1",
            tenant_id="tenant-1",
            source_technology=SourceTechnology.COSMOS,
            source_version=1,
            fragment_owner="document_core",
            payload={"title": "First"},
            event_time_utc=now,
        ),
        CanonicalDomainEvent(
            domain_name="customer_documents",
            entity_type="customerDocument",
            logical_entity_id="doc-1",
            tenant_id="tenant-1",
            source_technology=SourceTechnology.ALLOYDB,
            source_version=7,
            fragment_owner="customer_profile",
            payload={"customerName": "Apurva"},
            event_time_utc=now,
        ),
    ]

    builder.upsert(events[0], now_utc=now)
    decision = builder.upsert(events[1], now_utc=now)

    assert decision.publish is True
    assert decision.document is not None
    assert decision.document.payload["title"] == "First"
    assert decision.document.payload["customerName"] == "Apurva"
    assert decision.state.status == ProjectionStatus.PUBLISHED


def test_projection_marks_stale_required_fragment() -> None:
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[
                    DependencyRule(owner="document_core", required=True, freshness_ttl_seconds=10),
                ],
            )
        ]
    )
    event_time = datetime.now(UTC) - timedelta(seconds=30)
    event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-1",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=1,
        fragment_owner="document_core",
        payload={"title": "Stale"},
        event_time_utc=event_time,
    )

    decision = builder.upsert(event, now_utc=datetime.now(UTC))

    assert decision.publish is False
    assert decision.state.status == ProjectionStatus.PENDING_REHYDRATION
