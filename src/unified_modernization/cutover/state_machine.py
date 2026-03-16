from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class BackendPrimaryState(StrEnum):
    AZURE_PRIMARY = "azure_primary"
    AZURE_PRIMARY_GCP_WARMING = "azure_primary_gcp_warming"
    GCP_SHADOW_VALIDATED = "gcp_shadow_validated"
    GCP_PRIMARY_FALLBACK_WINDOW = "gcp_primary_fallback_window"
    GCP_PRIMARY_STABLE = "gcp_primary_stable"


class SearchServingState(StrEnum):
    AZURE_SEARCH_PRIMARY_ELASTIC_DARK = "azure_search_primary_elastic_dark"
    AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW = "azure_search_primary_elastic_shadow"
    ELASTIC_CANARY = "elastic_canary"
    ELASTIC_PRIMARY_FALLBACK_WINDOW = "elastic_primary_fallback_window"
    ELASTIC_PRIMARY_STABLE = "elastic_primary_stable"


_BACKEND_TRANSITIONS = {
    BackendPrimaryState.AZURE_PRIMARY: {BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING},
    BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING: {BackendPrimaryState.GCP_SHADOW_VALIDATED},
    BackendPrimaryState.GCP_SHADOW_VALIDATED: {BackendPrimaryState.GCP_PRIMARY_FALLBACK_WINDOW},
    BackendPrimaryState.GCP_PRIMARY_FALLBACK_WINDOW: {BackendPrimaryState.GCP_PRIMARY_STABLE, BackendPrimaryState.AZURE_PRIMARY},
    BackendPrimaryState.GCP_PRIMARY_STABLE: set(),
}

_SEARCH_TRANSITIONS = {
    SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_DARK: {SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW},
    SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW: {SearchServingState.ELASTIC_CANARY},
    SearchServingState.ELASTIC_CANARY: {
        SearchServingState.ELASTIC_PRIMARY_FALLBACK_WINDOW,
        SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW,
    },
    SearchServingState.ELASTIC_PRIMARY_FALLBACK_WINDOW: {
        SearchServingState.ELASTIC_PRIMARY_STABLE,
        SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW,
    },
    SearchServingState.ELASTIC_PRIMARY_STABLE: set(),
}


class CutoverTransitionEvent(BaseModel):
    domain_name: str
    track: str
    from_state: str
    to_state: str
    operator: str
    reason: str
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PersistedCutoverState(BaseModel):
    domain_name: str
    backend_state: BackendPrimaryState
    search_state: SearchServingState
    updated_at_utc: datetime


class CutoverStateStore(Protocol):
    def append(self, event: CutoverTransitionEvent) -> None:
        raise NotImplementedError

    def load_latest(self, domain_name: str) -> PersistedCutoverState | None:
        raise NotImplementedError


class FirestoreDocumentSnapshotProtocol(Protocol):
    def to_dict(self) -> dict[str, object] | None:
        raise NotImplementedError


class FirestoreDocumentReferenceProtocol(Protocol):
    def set(self, document_data: dict[str, object]) -> None:
        raise NotImplementedError


class FirestoreCollectionProtocol(Protocol):
    def document(self, document_id: str | None = None) -> FirestoreDocumentReferenceProtocol:
        raise NotImplementedError

    def stream(self) -> list[FirestoreDocumentSnapshotProtocol]:
        raise NotImplementedError


class FirestoreClientProtocol(Protocol):
    def collection(self, collection_name: str) -> FirestoreCollectionProtocol:
        raise NotImplementedError


class InMemoryCutoverStateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, PersistedCutoverState] = {}
        self._events: list[CutoverTransitionEvent] = []

    def append(self, event: CutoverTransitionEvent) -> None:
        with self._lock:
            current = self._states.get(
                event.domain_name,
                PersistedCutoverState(
                    domain_name=event.domain_name,
                    backend_state=BackendPrimaryState.AZURE_PRIMARY,
                    search_state=SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_DARK,
                    updated_at_utc=event.timestamp_utc,
                ),
            )
            if event.track == "backend":
                current.backend_state = BackendPrimaryState(event.to_state)
            elif event.track == "search":
                current.search_state = SearchServingState(event.to_state)
            else:
                raise ValueError(f"unknown cutover track {event.track!r}")
            current.updated_at_utc = event.timestamp_utc
            self._states[event.domain_name] = current.model_copy(deep=True)
            self._events.append(event.model_copy(deep=True))

    def load_latest(self, domain_name: str) -> PersistedCutoverState | None:
        with self._lock:
            state = self._states.get(domain_name)
            return None if state is None else state.model_copy(deep=True)


class JsonFileCutoverStateStore:
    """Simple append-only JSONL store for local and pilot durability testing."""

    def __init__(self, path: str | Path = "cutover_state.jsonl") -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("", encoding="utf-8")

    def append(self, event: CutoverTransitionEvent) -> None:
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json())
                handle.write("\n")

    def load_latest(self, domain_name: str) -> PersistedCutoverState | None:
        with self._lock:
            lines = [line.strip() for line in self._path.read_text(encoding="utf-8").splitlines() if line.strip()]
        backend_state = BackendPrimaryState.AZURE_PRIMARY
        search_state = SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_DARK
        updated_at: datetime | None = None
        found = False
        for line in lines:
            event = CutoverTransitionEvent.model_validate(json.loads(line))
            if event.domain_name != domain_name:
                continue
            found = True
            if event.track == "backend":
                backend_state = BackendPrimaryState(event.to_state)
            elif event.track == "search":
                search_state = SearchServingState(event.to_state)
            updated_at = event.timestamp_utc
        if not found or updated_at is None:
            return None
        return PersistedCutoverState(
            domain_name=domain_name,
            backend_state=backend_state,
            search_state=search_state,
            updated_at_utc=updated_at,
        )


class FirestoreCutoverStateStore:
    """Append-only Firestore-backed cutover transition store for deployed environments."""

    def __init__(self, client: FirestoreClientProtocol, *, collection_prefix: str = "cutover_transitions") -> None:
        self._client = client
        self._collection_prefix = collection_prefix

    def append(self, event: CutoverTransitionEvent) -> None:
        collection = self._client.collection(self._collection_name(event.domain_name))
        collection.document().set(event.model_dump(mode="json"))

    def load_latest(self, domain_name: str) -> PersistedCutoverState | None:
        collection = self._client.collection(self._collection_name(domain_name))
        events = [
            CutoverTransitionEvent.model_validate(snapshot.to_dict() or {})
            for snapshot in collection.stream()
            if snapshot.to_dict()
        ]
        if not events:
            return None
        events.sort(key=lambda item: item.timestamp_utc)
        backend_state = BackendPrimaryState.AZURE_PRIMARY
        search_state = SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_DARK
        updated_at = events[-1].timestamp_utc
        for event in events:
            if event.track == "backend":
                backend_state = BackendPrimaryState(event.to_state)
            elif event.track == "search":
                search_state = SearchServingState(event.to_state)
        return PersistedCutoverState(
            domain_name=domain_name,
            backend_state=backend_state,
            search_state=search_state,
            updated_at_utc=updated_at,
        )

    def _collection_name(self, domain_name: str) -> str:
        sanitized = re.sub(r"[^0-9A-Za-z_-]+", "_", domain_name)
        return f"{self._collection_prefix}__{sanitized}"


class DomainMigrationState:
    def __init__(
        self,
        *,
        domain_name: str,
        store: CutoverStateStore | None = None,
    ) -> None:
        self.domain_name = domain_name
        self._store = store or InMemoryCutoverStateStore()
        saved = self._store.load_latest(domain_name)
        self.backend_state = saved.backend_state if saved is not None else BackendPrimaryState.AZURE_PRIMARY
        self.search_state = (
            saved.search_state if saved is not None else SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_DARK
        )

    def transition_backend(
        self,
        target: BackendPrimaryState,
        *,
        operator: str = "system",
        reason: str = "manual_transition",
    ) -> None:
        allowed = _BACKEND_TRANSITIONS[self.backend_state]
        if target not in allowed:
            raise ValueError(
                f"Invalid transition: {self.backend_state} -> {target}. "
                f"Valid targets from this state: {sorted(item.value for item in allowed)}"
            )
        event = CutoverTransitionEvent(
            domain_name=self.domain_name,
            track="backend",
            from_state=self.backend_state.value,
            to_state=target.value,
            operator=operator,
            reason=reason,
            timestamp_utc=datetime.now(UTC),
        )
        self._store.append(event)
        self.backend_state = target

    def transition_search(
        self,
        target: SearchServingState,
        *,
        operator: str = "system",
        reason: str = "manual_transition",
    ) -> None:
        allowed = _SEARCH_TRANSITIONS[self.search_state]
        if target not in allowed:
            raise ValueError(
                f"Invalid transition: {self.search_state} -> {target}. "
                f"Valid targets from this state: {sorted(item.value for item in allowed)}"
            )
        event = CutoverTransitionEvent(
            domain_name=self.domain_name,
            track="search",
            from_state=self.search_state.value,
            to_state=target.value,
            operator=operator,
            reason=reason,
            timestamp_utc=datetime.now(UTC),
        )
        self._store.append(event)
        self.search_state = target
