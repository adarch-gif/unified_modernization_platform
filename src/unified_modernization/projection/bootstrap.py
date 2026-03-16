from __future__ import annotations

from pydantic import BaseModel, Field

from unified_modernization.contracts.projection import DependencyPolicy
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.projection.publisher import ElasticsearchDocumentPublisher, ElasticsearchPublisherConfig
from unified_modernization.projection.store import ProjectionStateStore
from unified_modernization.routing.tenant_policy import TenantPolicyEngine


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


class ProjectionPublisherRuntimeConfig(BaseModel):
    endpoint: str
    api_key: str | None = None
    bearer_token: str | None = None
    refresh: str | None = None
    dedicated_tenants: set[str] = Field(default_factory=set)
    write_aliases_by_entity_type: dict[str, str] = Field(default_factory=dict)


def build_elasticsearch_document_publisher(
    *,
    config: ProjectionPublisherRuntimeConfig,
) -> ElasticsearchDocumentPublisher:
    return ElasticsearchDocumentPublisher(
        ElasticsearchPublisherConfig(
            endpoint=config.endpoint,
            api_key=config.api_key,
            bearer_token=config.bearer_token,
            refresh=config.refresh,
            write_aliases_by_entity_type=dict(config.write_aliases_by_entity_type),
        ),
        tenant_policy_engine=TenantPolicyEngine(dedicated_tenants=set(config.dedicated_tenants)),
    )
