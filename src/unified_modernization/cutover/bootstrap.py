from __future__ import annotations

from pydantic import BaseModel

from unified_modernization.cutover.state_machine import CutoverStateStore, DomainMigrationState, InMemoryCutoverStateStore


_NON_PRODUCTION_ENVIRONMENTS = {"local", "dev", "test"}


class CutoverRuntimeConfig(BaseModel):
    environment: str = "dev"


def build_domain_migration_state(
    *,
    domain_name: str,
    config: CutoverRuntimeConfig,
    store: CutoverStateStore | None = None,
) -> DomainMigrationState:
    environment = config.environment.lower()
    resolved_store = store or InMemoryCutoverStateStore()
    if environment not in _NON_PRODUCTION_ENVIRONMENTS and isinstance(resolved_store, InMemoryCutoverStateStore):
        raise ValueError("durable cutover state store is required outside local/dev/test environments")
    return DomainMigrationState(domain_name=domain_name, store=resolved_store)
