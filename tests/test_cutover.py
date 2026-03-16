from pathlib import Path

import pytest

from unified_modernization.cutover.state_machine import (
    BackendPrimaryState,
    DomainMigrationState,
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
