from pathlib import Path

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from unified_modernization.contracts.events import CanonicalDomainEvent, ChangeType, SourceTechnology
from unified_modernization.contracts.projection import DependencyPolicy, DependencyRule, ProjectionKey, ProjectionStatus
from unified_modernization.projection.bootstrap import build_projection_builder
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.projection.store import (
    SpannerProjectionStateStore,
    SqliteProjectionStateStore,
)


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


def test_projection_domain_scoped_policies_do_not_collide() -> None:
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                domain_name="customer_documents",
                entity_type="sharedDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            ),
            DependencyPolicy(
                domain_name="account_documents",
                entity_type="sharedDocument",
                rules=[DependencyRule(owner="account_profile", required=True)],
            ),
        ]
    )

    customer_decision = builder.upsert(
        CanonicalDomainEvent(
            domain_name="customer_documents",
            entity_type="sharedDocument",
            logical_entity_id="doc-1",
            tenant_id="tenant-1",
            source_technology=SourceTechnology.COSMOS,
            source_version=1,
            fragment_owner="document_core",
            payload={"title": "Customer"},
            event_time_utc=datetime.now(UTC),
        )
    )
    account_decision = builder.upsert(
        CanonicalDomainEvent(
            domain_name="account_documents",
            entity_type="sharedDocument",
            logical_entity_id="doc-2",
            tenant_id="tenant-1",
            source_technology=SourceTechnology.COSMOS,
            source_version=1,
            fragment_owner="document_core",
            payload={"title": "Account"},
            event_time_utc=datetime.now(UTC),
        )
    )

    assert customer_decision.publish is True
    assert account_decision.publish is False
    assert account_decision.state.missing_required_fragments == ["account_profile"]


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


def test_projection_state_persists_in_sqlite_store(tmp_path: Path) -> None:
    store = SqliteProjectionStateStore(tmp_path / "projection.db")
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ],
        state_store=store,
    )
    event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-2",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=1,
        fragment_owner="document_core",
        payload={"title": "Persisted"},
    )

    builder.upsert(event)
    persisted_state = builder.get_state("tenant-1", "customer_documents", "customerDocument", "doc-2")

    assert persisted_state is not None
    assert persisted_state.status == ProjectionStatus.PUBLISHED
    assert persisted_state.entity_revision >= 1
    assert store.pending_count() == 0


def test_duplicate_delete_event_is_idempotent() -> None:
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ]
    )
    delete_event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-delete",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=7,
        change_type=ChangeType.DELETE,
        fragment_owner="document_core",
        payload={},
        event_time_utc=datetime.now(UTC),
    )

    first = builder.upsert(delete_event)
    second = builder.upsert(delete_event)

    assert first.publish is True
    assert first.state.status == ProjectionStatus.DELETED
    assert second.publish is False
    assert second.state.status == ProjectionStatus.DELETED
    assert second.state.projection_version == first.state.projection_version


class _FakeSpannerSnapshot:
    def __init__(self, fragments: dict[tuple[str, ...], dict[str, Any]], states: dict[tuple[str, ...], dict[str, Any]]) -> None:
        self._fragments = fragments
        self._states = states

    def __enter__(self) -> "_FakeSpannerSnapshot":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def execute_sql(
        self,
        sql: str,
        params: dict[str, object] | None = None,
        param_types: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        del param_types
        params = params or {}
        key = (
            str(params.get("tenant_id", "")),
            str(params.get("domain_name", "")),
            str(params.get("entity_type", "")),
            str(params.get("logical_entity_id", "")),
        )
        if "FROM projection_fragments" in sql and "source_version" not in sql:
            rows = []
            for fragment_owner, record in self._fragments.get(key, {}).items():
                rows.append({"fragment_owner": fragment_owner, "payload_json": record["payload_json"]})
            return rows
        if "FROM projection_fragments" in sql and "source_version" in sql:
            fragment = self._fragments.get(key, {}).get(str(params["fragment_owner"]))
            return [] if fragment is None else [{"source_version": fragment["source_version"]}]
        if "FROM projection_states" in sql and "payload_json" in sql:
            state = self._states.get(key)
            return [] if state is None else [{"payload_json": state["payload_json"]}]
        if "SELECT status" in sql:
            return [{"status": row["status"]} for row in self._states.values()]
        raise AssertionError(f"unexpected SQL: {sql}")


class _FakeSpannerTransaction(_FakeSpannerSnapshot):
    def execute_update(
        self,
        sql: str,
        params: dict[str, object] | None = None,
        param_types: dict[str, object] | None = None,
    ) -> int:
        del param_types
        params = params or {}
        key = (
            str(params.get("tenant_id", "")),
            str(params.get("domain_name", "")),
            str(params.get("entity_type", "")),
            str(params.get("logical_entity_id", "")),
        )
        if "projection_fragments" in sql:
            fragment_owner = str(params["fragment_owner"])
            self._fragments.setdefault(key, {})[fragment_owner] = {
                "source_version": int(params["source_version"]),
                "payload_json": str(params["payload_json"]),
            }
            return 1
        if "projection_states" in sql:
            self._states[key] = {
                "status": str(params["status"]),
                "payload_json": str(params["payload_json"]),
            }
            return 1
        raise AssertionError(f"unexpected DML: {sql}")


class _FakeSpannerDatabase:
    def __init__(self) -> None:
        self.fragments: dict[tuple[str, ...], dict[str, Any]] = {}
        self.states: dict[tuple[str, ...], dict[str, Any]] = {}

    def snapshot(self) -> _FakeSpannerSnapshot:
        return _FakeSpannerSnapshot(self.fragments, self.states)

    def run_in_transaction(self, func: object) -> object:
        transaction = _FakeSpannerTransaction(self.fragments, self.states)
        assert callable(func)
        return func(transaction)


def test_projection_state_persists_in_spanner_store() -> None:
    database = _FakeSpannerDatabase()
    store = SpannerProjectionStateStore(database)
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ],
        state_store=store,
    )
    event = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-3",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=5,
        fragment_owner="document_core",
        payload={"title": "Spanner"},
    )

    builder.upsert(event)
    persisted_state = builder.get_state("tenant-1", "customer_documents", "customerDocument", "doc-3")

    assert persisted_state is not None
    assert persisted_state.status == ProjectionStatus.PUBLISHED
    assert persisted_state.entity_revision >= 1
    assert store.pending_count() == 0
    assert len(database.fragments) == 1


def test_spanner_store_ignores_older_fragment_versions() -> None:
    database = _FakeSpannerDatabase()
    store = SpannerProjectionStateStore(database)
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ],
        state_store=store,
    )
    newer = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-4",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=10,
        fragment_owner="document_core",
        payload={"title": "Newer"},
    )
    older = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-4",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=9,
        fragment_owner="document_core",
        payload={"title": "Older"},
    )

    builder.upsert(newer)
    builder.upsert(older)
    fragments = store.get_fragments(
        ProjectionKey(
            tenant_id="tenant-1",
            domain_name="customer_documents",
            entity_type="customerDocument",
            logical_entity_id="doc-4",
        )
    )

    assert fragments["document_core"].payload["title"] == "Newer"


def test_projection_entity_revision_increments_across_updates() -> None:
    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ]
    )
    first = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-5",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=1,
        fragment_owner="document_core",
        payload={"title": "First"},
    )
    second = CanonicalDomainEvent(
        domain_name="customer_documents",
        entity_type="customerDocument",
        logical_entity_id="doc-5",
        tenant_id="tenant-1",
        source_technology=SourceTechnology.COSMOS,
        source_version=2,
        fragment_owner="document_core",
        payload={"title": "Second"},
    )

    first_decision = builder.upsert(first)
    second_decision = builder.upsert(second)

    assert first_decision.state.entity_revision == 1
    assert second_decision.state.entity_revision == 2


def test_projection_builder_rejects_in_memory_state_in_prod() -> None:
    with pytest.raises(ValueError):
        build_projection_builder(
            policies=[
                DependencyPolicy(
                    entity_type="customerDocument",
                    rules=[DependencyRule(owner="document_core", required=True)],
                )
            ],
            environment="prod",
        )
