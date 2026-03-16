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
    def __init__(self, documents: dict[str, dict[str, object]], document_id: str | None = None) -> None:
        self._documents = documents
        self._document_id = document_id or f"doc-{len(documents) + 1}"

    def set(self, document_data: dict[str, object]) -> None:
        self._documents[self._document_id] = dict(document_data)

    def get(self) -> "_FakeFirestoreSnapshot":
        return _FakeFirestoreSnapshot(self._documents.get(self._document_id, {}))


class _FakeFirestoreSnapshot:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return dict(self._payload)


class _FakeFirestoreCollection:
    def __init__(self, documents: dict[str, dict[str, object]]) -> None:
        self._documents = documents

    def document(self, document_id: str | None = None) -> _FakeFirestoreDocumentReference:
        return _FakeFirestoreDocumentReference(self._documents, document_id)

    def stream(self) -> list[_FakeFirestoreSnapshot]:
        return [_FakeFirestoreSnapshot(document) for document in self._documents.values()]


class _FakeFirestoreTransaction:
    def get(self, document_reference: _FakeFirestoreDocumentReference) -> _FakeFirestoreSnapshot:
        return document_reference.get()

    def set(self, document_reference: _FakeFirestoreDocumentReference, document_data: dict[str, object]) -> None:
        document_reference.set(document_data)


class _FakeFirestoreClient:
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, object]]] = {}
        self.transaction_calls = 0

    def collection(self, collection_name: str) -> _FakeFirestoreCollection:
        documents = self._collections.setdefault(collection_name, {})
        return _FakeFirestoreCollection(documents)

    def run_transaction(self, func):  # type: ignore[no-untyped-def]
        self.transaction_calls += 1
        return func(_FakeFirestoreTransaction())


def test_firestore_cutover_store_rehydrates_domain_state() -> None:
    client = _FakeFirestoreClient()
    store = FirestoreCutoverStateStore(client)
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
    assert client.transaction_calls == 2


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
