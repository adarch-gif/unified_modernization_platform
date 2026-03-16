from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from unified_modernization.contracts.events import CanonicalDomainEvent, ChangeType
from unified_modernization.contracts.projection import (
    CompletenessStatus,
    DependencyPolicy,
    FragmentRecord,
    ProjectionEntityRecord,
    ProjectionKey,
    ProjectionMutationResult,
    ProjectionStateRecord,
    ProjectionStatus,
    PublicationDecision,
    SearchDocument,
)
from unified_modernization.observability.telemetry import NoopTelemetrySink, TelemetryEvent, TelemetrySink
from unified_modernization.projection.store import InMemoryProjectionStateStore, ProjectionStateStore


_NON_PRODUCTION_ENVIRONMENTS = {"local", "dev", "test"}


class ProjectionBuilder:
    """Projection completeness and publication logic backed by a pluggable state store."""

    def __init__(
        self,
        policies: list[DependencyPolicy],
        state_store: ProjectionStateStore | None = None,
        environment: str = "dev",
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        self._policies: dict[tuple[str | None, str], DependencyPolicy] = {}
        for policy in policies:
            key = (policy.domain_name, policy.entity_type)
            if key in self._policies:
                raise ValueError(f"duplicate dependency policy for domain={policy.domain_name!r}, entity_type={policy.entity_type!r}")
            self._policies[key] = policy
        self._environment = environment.lower()
        resolved_state_store = state_store or InMemoryProjectionStateStore()
        if self._environment not in _NON_PRODUCTION_ENVIRONMENTS and isinstance(
            resolved_state_store,
            InMemoryProjectionStateStore,
        ):
            raise ValueError("in-memory projection state is not allowed outside local/dev/test environments")
        self._state_store = resolved_state_store
        self._telemetry_sink = telemetry_sink or NoopTelemetrySink()

    def _resolve_policy(self, *, domain_name: str, entity_type: str) -> DependencyPolicy:
        exact = self._policies.get((domain_name, entity_type))
        if exact is not None:
            return exact
        fallback = self._policies.get((None, entity_type))
        if fallback is not None:
            return fallback
        raise KeyError(f"no dependency policy configured for domain={domain_name!r}, entity_type={entity_type!r}")

    @staticmethod
    def _projection_key(event: CanonicalDomainEvent) -> ProjectionKey:
        return ProjectionKey(
            tenant_id=event.tenant_id,
            domain_name=event.domain_name,
            entity_type=event.entity_type,
            logical_entity_id=event.logical_entity_id,
        )

    def upsert(self, event: CanonicalDomainEvent, now_utc: datetime | None = None) -> PublicationDecision:
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        try:
            policy = self._resolve_policy(domain_name=event.domain_name, entity_type=event.entity_type)
        except KeyError as exc:
            raise ValueError(
                f"no dependency policy configured for domain={event.domain_name!r}, entity_type={event.entity_type!r}"
            ) from exc
        key = self._projection_key(event)

        incoming = FragmentRecord(
            tenant_id=event.tenant_id,
            domain_name=event.domain_name,
            entity_type=event.entity_type,
            logical_entity_id=event.logical_entity_id,
            fragment_owner=event.fragment_owner,
            source_technology=event.source_technology,
            source_version=event.source_version,
            event_time_utc=event.event_time_utc,
            payload=event.payload,
            delete_flag=event.change_type == ChangeType.DELETE,
        )

        state_capture: dict[str, ProjectionStateRecord | None] = {"previous": None}

        def capture_previous_state(state: ProjectionStateRecord | None) -> None:
            state_capture["previous"] = None if state is None else state.model_copy(deep=True)

        with self._telemetry_sink.start_span(
            "projection.builder.upsert",
            attributes={
                "domain_name": event.domain_name,
                "entity_type": event.entity_type,
                "fragment_owner": event.fragment_owner,
                "change_type": event.change_type.value,
            },
        ) as span:
            result = self._state_store.mutate_entity(
                key,
                lambda entity: self._capture_and_mutate_entity(
                    entity=entity,
                    incoming=incoming,
                    event=event,
                    now=now,
                    policy=policy,
                    previous_state_ref=capture_previous_state,
                ),
            )
            self._telemetry_sink.increment(
                "projection.upsert.total",
                tags={
                    "domain_name": event.domain_name,
                    "entity_type": event.entity_type,
                    "change_type": event.change_type.value,
                    "published": str(result.decision.publish).lower(),
                },
            )
            self._record_completion_telemetry(
                trace_id=span.trace_id,
                previous_state=state_capture["previous"],
                result=result,
                now=now,
            )
        return result.decision

    def quarantine(
        self,
        tenant_id: str,
        domain_name: str,
        entity_type: str,
        logical_entity_id: str,
        *,
        reason: str,
        now_utc: datetime | None = None,
        trace_id: str | None = None,
    ) -> ProjectionStateRecord:
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        key = ProjectionKey(
            tenant_id=tenant_id,
            domain_name=domain_name,
            entity_type=entity_type,
            logical_entity_id=logical_entity_id,
        )

        def mutator(entity: ProjectionEntityRecord) -> ProjectionMutationResult:
            state = entity.state or ProjectionStateRecord(
                tenant_id=tenant_id,
                domain_name=domain_name,
                entity_type=entity_type,
                logical_entity_id=logical_entity_id,
                status=ProjectionStatus.QUARANTINED,
                reason_code="quarantined",
            )
            state.status = ProjectionStatus.QUARANTINED
            state.reason_code = "quarantined"
            state.quarantine_reason = reason
            state.quarantine_at_utc = now
            entity.state = state
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=state),
            )

        result = self._state_store.mutate_entity(key, mutator)
        self._telemetry_sink.increment(
            "projection.quarantined",
            tags={"domain_name": domain_name, "entity_type": entity_type},
        )
        self._telemetry_sink.emit(
            TelemetryEvent(
                event_type="projection_quarantined",
                severity="warning",
                trace_id=trace_id,
                attributes={
                    "tenant_id": tenant_id,
                    "domain_name": domain_name,
                    "entity_type": entity_type,
                    "logical_entity_id": logical_entity_id,
                    "reason": reason,
                },
            )
        )
        return result.decision.state

    def _capture_and_mutate_entity(
        self,
        *,
        entity: ProjectionEntityRecord,
        incoming: FragmentRecord,
        event: CanonicalDomainEvent,
        now: datetime,
        policy: DependencyPolicy,
        previous_state_ref: Callable[[ProjectionStateRecord | None], None],
    ) -> ProjectionMutationResult:
        previous_state_ref(entity.state)
        return self._mutate_entity(
            entity=entity,
            incoming=incoming,
            event=event,
            now=now,
            policy=policy,
        )

    def _record_completion_telemetry(
        self,
        *,
        trace_id: str,
        previous_state: ProjectionStateRecord | None,
        result: ProjectionMutationResult,
        now: datetime,
    ) -> None:
        state = result.decision.state
        if not self._is_completion_transition(previous_state, state):
            return

        duration_ms = self._time_to_completeness_ms(result.entity, now)
        if duration_ms is None:
            return

        tags = {
            "domain_name": state.domain_name,
            "entity_type": state.entity_type,
            "status": state.status.value,
            "completeness_status": state.completeness_status.value,
        }
        self._telemetry_sink.increment("projection.completeness.achieved", tags=tags)
        self._telemetry_sink.record_timing(
            "projection.time_to_completeness",
            duration_ms,
            tags=tags,
            trace_id=trace_id,
        )
        self._telemetry_sink.emit(
            TelemetryEvent(
                event_type="projection_completeness_achieved",
                trace_id=trace_id,
                attributes={
                    "tenant_id": state.tenant_id,
                    "domain_name": state.domain_name,
                    "entity_type": state.entity_type,
                    "logical_entity_id": state.logical_entity_id,
                    "status": state.status.value,
                    "prior_status": None if previous_state is None else previous_state.status.value,
                    "projection_version": state.projection_version,
                    "time_to_completeness_ms": duration_ms,
                },
            )
        )

    @staticmethod
    def _is_completion_transition(
        previous_state: ProjectionStateRecord | None,
        current_state: ProjectionStateRecord,
    ) -> bool:
        terminal_states = {ProjectionStatus.PUBLISHED, ProjectionStatus.DELETED}
        if current_state.status not in terminal_states:
            return False
        if previous_state is None:
            return True
        return previous_state.status not in terminal_states

    @staticmethod
    def _time_to_completeness_ms(
        entity: ProjectionEntityRecord,
        now: datetime,
    ) -> float | None:
        fragment_event_times = [fragment.event_time_utc for fragment in entity.fragments.values()]
        if not fragment_event_times:
            return None
        earliest_fragment_time = min(fragment_event_times).astimezone(UTC)
        return max(0.0, (now - earliest_fragment_time).total_seconds() * 1000)

    def _mutate_entity(
        self,
        *,
        entity: ProjectionEntityRecord,
        incoming: FragmentRecord,
        event: CanonicalDomainEvent,
        now: datetime,
        policy: DependencyPolicy,
    ) -> ProjectionMutationResult:
        current = entity.fragments.get(incoming.fragment_owner)
        if current is None or incoming.source_version >= current.source_version:
            entity.fragments[incoming.fragment_owner] = incoming

        fragments = entity.fragments
        state = entity.state or ProjectionStateRecord(
            tenant_id=event.tenant_id,
            domain_name=event.domain_name,
            entity_type=event.entity_type,
            logical_entity_id=event.logical_entity_id,
            status=ProjectionStatus.PENDING_REQUIRED_FRAGMENT,
            reason_code="initial_state",
        )
        entity.state = state

        prior_source_change = state.last_source_change_utc
        if prior_source_change is None or event.event_time_utc > prior_source_change:
            state.last_source_change_utc = event.event_time_utc

        missing: list[str] = []
        stale: list[str] = []
        for rule in policy.rules:
            fragment = fragments.get(rule.owner)
            if fragment is None:
                if rule.required:
                    missing.append(rule.owner)
                continue
            if rule.required and rule.is_stale(fragment.event_time_utc, now):
                stale.append(rule.owner)

        state.missing_required_fragments = missing
        state.stale_required_fragments = stale

        if missing:
            state.status = ProjectionStatus.PENDING_REQUIRED_FRAGMENT
            state.reason_code = "missing_required_fragment"
            state.completeness_status = CompletenessStatus.PARTIAL
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=state),
            )

        if stale:
            state.status = ProjectionStatus.PENDING_REHYDRATION
            state.reason_code = "stale_required_fragment"
            state.completeness_status = CompletenessStatus.PARTIAL
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=state),
            )

        merged_payload: dict[str, Any] = {}
        source_versions: dict[str, int] = {}
        delete_flags: list[bool] = []
        for rule in policy.rules:
            fragment = fragments.get(rule.owner)
            if fragment is None:
                continue
            merged_payload.update(fragment.payload)
            source_versions[rule.owner] = fragment.source_version
            delete_flags.append(fragment.delete_flag)

        if delete_flags and all(delete_flags):
            delete_payload_hash = hashlib.sha256(
                json.dumps({"is_deleted": True, "source_versions": source_versions}, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if delete_payload_hash == state.last_payload_hash and state.status == ProjectionStatus.DELETED:
                state.reason_code = "all_fragments_deleted"
                state.completeness_status = CompletenessStatus.DELETED
                state.last_built_utc = now
                state.last_published_utc = now
                return ProjectionMutationResult(
                    entity=entity,
                    decision=PublicationDecision(publish=False, state=state),
                )

            state.projection_version += 1
            state.status = ProjectionStatus.DELETED
            state.reason_code = "all_fragments_deleted"
            state.completeness_status = CompletenessStatus.DELETED
            state.last_payload_hash = delete_payload_hash
            state.last_built_utc = now
            state.last_published_utc = now
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(
                    publish=True,
                    state=state,
                    document=SearchDocument(
                        document_id=event.logical_entity_id,
                        tenant_id=event.tenant_id,
                        domain_name=event.domain_name,
                        entity_type=event.entity_type,
                        projection_version=state.projection_version,
                        completeness_status=CompletenessStatus.DELETED,
                        source_versions=source_versions,
                        payload={"is_deleted": True},
                    ),
                ),
            )

        completeness = CompletenessStatus.COMPLETE
        if not policy.allow_partial_optional_publish and len(source_versions) < len(policy.rules):
            state.status = ProjectionStatus.PENDING_REQUIRED_FRAGMENT
            state.reason_code = "optional_publish_disabled"
            state.completeness_status = CompletenessStatus.PARTIAL
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=state),
            )
        if len(source_versions) < len(policy.rules):
            completeness = CompletenessStatus.PARTIAL

        payload_hash = hashlib.sha256(json.dumps(merged_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if payload_hash != state.last_payload_hash or state.status != ProjectionStatus.PUBLISHED:
            state.projection_version += 1
            state.last_payload_hash = payload_hash

        state.status = ProjectionStatus.PUBLISHED
        state.reason_code = "published"
        state.completeness_status = completeness
        state.last_built_utc = now
        state.last_published_utc = now

        document = SearchDocument(
            document_id=event.logical_entity_id,
            tenant_id=event.tenant_id,
            domain_name=event.domain_name,
            entity_type=event.entity_type,
            projection_version=state.projection_version,
            completeness_status=completeness,
            source_versions=source_versions,
            payload=merged_payload,
        )
        return ProjectionMutationResult(
            entity=entity,
            decision=PublicationDecision(publish=True, state=state, document=document),
        )

    def get_state(
        self,
        tenant_id: str,
        domain_name: str,
        entity_type: str,
        logical_entity_id: str,
    ) -> ProjectionStateRecord | None:
        key = ProjectionKey(
            tenant_id=tenant_id,
            domain_name=domain_name,
            entity_type=entity_type,
            logical_entity_id=logical_entity_id,
        )
        return self._state_store.get_state(key)

    def pending_count(self) -> int:
        return self._state_store.pending_count()
