from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from unified_modernization.contracts.events import CanonicalDomainEvent, ChangeType
from unified_modernization.contracts.projection import (
    CompletenessStatus,
    DependencyPolicy,
    FragmentRecord,
    ProjectionStateRecord,
    ProjectionStatus,
    PublicationDecision,
    SearchDocument,
)


class ProjectionBuilder:
    """In-memory starter implementation of projection completeness and publication."""

    def __init__(self, policies: list[DependencyPolicy]) -> None:
        self._policies = {policy.entity_type: policy for policy in policies}
        self._fragments: dict[tuple[str, str], dict[str, FragmentRecord]] = {}
        self._states: dict[tuple[str, str], ProjectionStateRecord] = {}

    def upsert(self, event: CanonicalDomainEvent, now_utc: datetime | None = None) -> PublicationDecision:
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        policy = self._policies[event.entity_type]
        key = (event.tenant_id, event.logical_entity_id)
        fragments = self._fragments.setdefault(key, {})

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
        current = fragments.get(event.fragment_owner)
        if current is None or incoming.source_version >= current.source_version:
            fragments[event.fragment_owner] = incoming

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

        state = self._states.get(key) or ProjectionStateRecord(
            tenant_id=event.tenant_id,
            domain_name=event.domain_name,
            entity_type=event.entity_type,
            logical_entity_id=event.logical_entity_id,
            status=ProjectionStatus.PENDING_REQUIRED_FRAGMENT,
            reason_code="initial_state",
        )
        state.last_source_change_utc = event.event_time_utc
        state.missing_required_fragments = missing
        state.stale_required_fragments = stale

        if missing:
            state.status = ProjectionStatus.PENDING_REQUIRED_FRAGMENT
            state.reason_code = "missing_required_fragment"
            state.completeness_status = CompletenessStatus.PARTIAL
            self._states[key] = state
            return PublicationDecision(publish=False, state=state)

        if stale:
            state.status = ProjectionStatus.PENDING_REHYDRATION
            state.reason_code = "stale_required_fragment"
            state.completeness_status = CompletenessStatus.PARTIAL
            self._states[key] = state
            return PublicationDecision(publish=False, state=state)

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
            state.projection_version += 1
            state.status = ProjectionStatus.DELETED
            state.reason_code = "all_fragments_deleted"
            state.completeness_status = CompletenessStatus.DELETED
            state.last_built_utc = now
            state.last_published_utc = now
            self._states[key] = state
            return PublicationDecision(
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
            )

        completeness = CompletenessStatus.COMPLETE
        if not policy.allow_partial_optional_publish and len(source_versions) < len(policy.rules):
            state.status = ProjectionStatus.PENDING_REQUIRED_FRAGMENT
            state.reason_code = "optional_publish_disabled"
            state.completeness_status = CompletenessStatus.PARTIAL
            self._states[key] = state
            return PublicationDecision(publish=False, state=state)
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
        self._states[key] = state

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
        return PublicationDecision(publish=True, state=state, document=document)

    def get_state(self, tenant_id: str, logical_entity_id: str) -> ProjectionStateRecord | None:
        return self._states.get((tenant_id, logical_entity_id))
