from __future__ import annotations

import os
from collections.abc import Mapping

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


def load_projection_publisher_runtime_config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    prefix: str = "UMP_",
) -> ProjectionPublisherRuntimeConfig:
    source = env or os.environ
    endpoint = _required_str(source.get(f"{prefix}PUBLISHER_ENDPOINT"), field_name=f"{prefix}PUBLISHER_ENDPOINT")
    return ProjectionPublisherRuntimeConfig(
        endpoint=endpoint,
        api_key=_optional_str(source.get(f"{prefix}PUBLISHER_API_KEY")),
        bearer_token=_optional_str(source.get(f"{prefix}PUBLISHER_BEARER_TOKEN")),
        refresh=_optional_str(source.get(f"{prefix}PUBLISHER_REFRESH")),
        dedicated_tenants=_parse_set(source.get(f"{prefix}DEDICATED_TENANTS")),
        write_aliases_by_entity_type=_parse_mapping(source.get(f"{prefix}PUBLISHER_WRITE_ALIAS_MAP")),
    )


def build_elasticsearch_document_publisher_from_env(
    *,
    env: Mapping[str, str] | None = None,
    prefix: str = "UMP_",
) -> ElasticsearchDocumentPublisher:
    return build_elasticsearch_document_publisher(
        config=load_projection_publisher_runtime_config_from_env(env, prefix=prefix)
    )


def _parse_mapping(raw: str | None) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        pairs[key] = value.strip()
    return pairs


def _parse_set(raw: str | None) -> set[str]:
    if raw is None or not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_str(value: str | None, *, field_name: str) -> str:
    stripped = _optional_str(value)
    if stripped is None:
        raise ValueError(f"{field_name} is required")
    return stripped
