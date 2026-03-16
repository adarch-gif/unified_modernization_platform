from unified_modernization.routing.tenant_policy import IngestionPartitionPolicyEngine, TenantPolicyEngine, TenantRoutingClass


def test_tenant_policy_engine_routes_dedicated_tenants() -> None:
    policy = TenantPolicyEngine(dedicated_tenants={"tenant-a"}).resolve("tenant-a", "customerDocument")

    assert policy.routing_class == TenantRoutingClass.DEDICATED
    assert policy.read_alias == "customerDocument-tenant-a-read"


def test_ingestion_partition_policy_engine_dedicates_whale_tenants() -> None:
    whale = IngestionPartitionPolicyEngine(whale_tenants={"tenant-whale"}).resolve("tenant-whale", "customerDocument")
    shared = IngestionPartitionPolicyEngine(whale_tenants={"tenant-whale"}).resolve("tenant-small", "customerDocument")

    assert whale.dedicated is True
    assert whale.partition_group == "customerDocument-tenant-whale"
    assert shared.dedicated is False
    assert shared.partition_group.startswith("customerDocument-shared-")
