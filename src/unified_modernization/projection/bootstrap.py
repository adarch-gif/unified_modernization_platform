from __future__ import annotations

from unified_modernization.contracts.projection import DependencyPolicy
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.projection.store import ProjectionStateStore


def build_projection_builder(
    *,
    policies: list[DependencyPolicy],
    environment: str,
    state_store: ProjectionStateStore | None = None,
) -> ProjectionBuilder:
    """Central bootstrap path so runtime environments cannot bypass store rules accidentally."""

    return ProjectionBuilder(
        policies=policies,
        state_store=state_store,
        environment=environment,
    )
