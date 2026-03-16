from pathlib import Path

import pytest

from unified_modernization.cutover.bootstrap import CutoverRuntimeConfig, build_domain_migration_state
from unified_modernization.cutover.state_machine import (
    BackendPrimaryState,
    DomainMigrationState,
    FirestoreCutoverStateStore,
    JsonFileCutoverStateStore,
    SearchServingState,
)


def test_valid_backend_transition() -> None:
    state = DomainMigrationState(domain_name="customer_documents")
    state.transition_backend(BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING)
    assert state.backend_state == BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING


def test_invalid_search_transition_raises() -> None:
    state = DomainMigrationState(domain_name="customer_documents")
    with pytest.raises(ValueError):
        state.transition_search(SearchServingState.ELASTIC_CANARY)


def test_cutover_state_rehydrates_from_persisted_store(tmp_path: Path) -> None:
    store = JsonFileCutoverStateStore(tmp_path / "cutover_state.jsonl")
    state = DomainMigrationState(domain_name="customer_documents", store=store)
    state.transition_backend(
        BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING,
        operator="tester",
        reason="warmup",
    )

    rehydrated = DomainMigrationState(domain_name="customer_documents", store=store)

    assert rehydrated.backend_state == BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING


class _FakeFirestoreDocumentReference:
    def __init__(self, documents: list[dict[str, object]]) -> None:
        self._documents = documents

    def set(self, document_data: dict[str, object]) -> None:
        self._documents.append(dict(document_data))


class _FakeFirestoreSnapshot:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return dict(self._payload)


class _FakeFirestoreCollection:
    def __init__(self, documents: list[dict[str, object]]) -> None:
        self._documents = documents

    def document(self, document_id: str | None = None) -> _FakeFirestoreDocumentReference:
        del document_id
        return _FakeFirestoreDocumentReference(self._documents)

    def stream(self) -> list[_FakeFirestoreSnapshot]:
        return [_FakeFirestoreSnapshot(document) for document in self._documents]


class _FakeFirestoreClient:
    def __init__(self) -> None:
        self._collections: dict[str, list[dict[str, object]]] = {}

    def collection(self, collection_name: str) -> _FakeFirestoreCollection:
        documents = self._collections.setdefault(collection_name, [])
        return _FakeFirestoreCollection(documents)


def test_firestore_cutover_store_rehydrates_domain_state() -> None:
    store = FirestoreCutoverStateStore(_FakeFirestoreClient())
    state = DomainMigrationState(domain_name="customer_documents", store=store)

    state.transition_backend(
        BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING,
        operator="tester",
        reason="warmup",
    )
    state.transition_search(
        SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW,
        operator="tester",
        reason="shadow",
    )

    rehydrated = DomainMigrationState(domain_name="customer_documents", store=store)

    assert rehydrated.backend_state == BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING
    assert rehydrated.search_state == SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW


def test_cutover_bootstrap_requires_durable_store_in_prod() -> None:
    with pytest.raises(ValueError):
        build_domain_migration_state(
            domain_name="customer_documents",
            config=CutoverRuntimeConfig(environment="prod"),
        )


def test_cutover_bootstrap_allows_durable_store_in_prod(tmp_path: Path) -> None:
    state = build_domain_migration_state(
        domain_name="customer_documents",
        config=CutoverRuntimeConfig(environment="prod"),
        store=JsonFileCutoverStateStore(tmp_path / "cutover_state.jsonl"),
    )

    assert state.backend_state == BackendPrimaryState.AZURE_PRIMARY
