from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


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


class DomainMigrationState(BaseModel):
    domain_name: str
    backend_state: BackendPrimaryState = BackendPrimaryState.AZURE_PRIMARY
    search_state: SearchServingState = SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_DARK

    def transition_backend(self, target: BackendPrimaryState) -> None:
        allowed = _BACKEND_TRANSITIONS[self.backend_state]
        if target not in allowed:
            raise ValueError(f"invalid backend transition: {self.backend_state} -> {target}")
        self.backend_state = target

    def transition_search(self, target: SearchServingState) -> None:
        allowed = _SEARCH_TRANSITIONS[self.search_state]
        if target not in allowed:
            raise ValueError(f"invalid search transition: {self.search_state} -> {target}")
        self.search_state = target
