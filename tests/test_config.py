from pathlib import Path

from unified_modernization.config.loader import load_dependency_policies, load_domain_configs


def test_domain_config_loader_reads_yaml(example_config: Path = Path("examples/domain_config.yaml")) -> None:
    configs = load_domain_configs(example_config)
    policies = load_dependency_policies(example_config)

    assert configs[0].name == "customer_documents"
    assert configs[0].routing.dedicated_tenants == ["tenant-enterprise-001"]
    assert policies[0].entity_type == "customerDocument"
    assert len(policies[0].rules) == 3
