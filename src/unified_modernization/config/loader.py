from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from unified_modernization.contracts.projection import DependencyPolicy, DependencyRule


class ProjectionPolicyConfig(BaseModel):
    allow_partial_optional_publish: bool = False
    rules: list[DependencyRule] = Field(default_factory=list)


class DomainRoutingConfig(BaseModel):
    dedicated_tenants: list[str] = Field(default_factory=list)


class DomainConfig(BaseModel):
    name: str
    entity_type: str
    current_source: str
    target_store: str
    routing: DomainRoutingConfig = Field(default_factory=DomainRoutingConfig)
    projection_policy: ProjectionPolicyConfig = Field(default_factory=ProjectionPolicyConfig)

    def to_dependency_policy(self) -> DependencyPolicy:
        return DependencyPolicy(
            entity_type=self.entity_type,
            rules=[rule.model_copy(deep=True) for rule in self.projection_policy.rules],
            allow_partial_optional_publish=self.projection_policy.allow_partial_optional_publish,
        )


def load_domain_configs(path: str | Path) -> list[DomainConfig]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [DomainConfig.model_validate(item) for item in payload.get("domains", [])]


def load_dependency_policies(path: str | Path) -> list[DependencyPolicy]:
    return [config.to_dependency_policy() for config in load_domain_configs(path)]
