from pathlib import Path

import pytest

from unified_modernization.config.loader import TargetStore, load_dependency_policies, load_domain_configs


def test_domain_config_loader_reads_yaml(example_config: Path = Path("examples/domain_config.yaml")) -> None:
    configs = load_domain_configs(example_config)
    policies = load_dependency_policies(example_config)

    assert configs[0].name == "customer_documents"
    assert configs[0].target_store == TargetStore.SPANNER
    assert configs[0].routing.dedicated_tenants == ["tenant-enterprise-001"]
    assert policies[0].entity_type == "customerDocument"
    assert len(policies[0].rules) == 3


def test_domain_config_loader_rejects_unknown_target_store(tmp_path: Path) -> None:
    config_path = tmp_path / "domain_config.yaml"
    config_path.write_text(
        """
domains:
  - name: customer_documents
    entity_type: customerDocument
    current_source: cosmos
    target_store: unsupported_store
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_domain_configs(config_path)
