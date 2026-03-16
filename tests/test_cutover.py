import pytest

from unified_modernization.cutover.state_machine import (
    BackendPrimaryState,
    DomainMigrationState,
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
