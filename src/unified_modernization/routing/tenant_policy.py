from __future__ import annotations

from enum import StrEnum
import hashlib

from pydantic import BaseModel


class TenantRoutingClass(StrEnum):
    SHARED_A = "shared_a"
    SHARED_B = "shared_b"
    DEDICATED = "dedicated"


class TenantPolicy(BaseModel):
    tenant_id: str
    routing_class: TenantRoutingClass
    write_alias: str
    read_alias: str
    routing_key: str | None = None


class IngestionPartitionPolicy(BaseModel):
    tenant_id: str
    entity_type: str
    partition_key: str
    partition_group: str
    dedicated: bool = False


class TenantPolicyEngine:
    """Deterministic starter policy for shared versus dedicated routing."""

    def __init__(self, dedicated_tenants: set[str] | None = None) -> None:
        self._dedicated_tenants = dedicated_tenants or set()

    def resolve(self, tenant_id: str, entity_type: str) -> TenantPolicy:
        if tenant_id in self._dedicated_tenants:
            base = f"{entity_type}-{tenant_id}"
            return TenantPolicy(
                tenant_id=tenant_id,
                routing_class=TenantRoutingClass.DEDICATED,
                write_alias=f"{base}-write",
                read_alias=f"{base}-read",
                routing_key=None,
            )
        bucket = (
            TenantRoutingClass.SHARED_A
            if self._stable_hash(tenant_id) % 2 == 0
            else TenantRoutingClass.SHARED_B
        )
        return TenantPolicy(
            tenant_id=tenant_id,
            routing_class=bucket,
            write_alias=f"{entity_type}-{bucket.value}-write",
            read_alias=f"{entity_type}-{bucket.value}-read",
            routing_key=tenant_id,
        )

    @staticmethod
    def _stable_hash(value: str) -> int:
        return int(hashlib.sha256(value.encode("utf-8")).hexdigest(), 16)


class IngestionPartitionPolicyEngine:
    """Deterministic ingestion partitioning with dedicated lanes for whale tenants."""

    def __init__(
        self,
        *,
        whale_tenants: set[str] | None = None,
        shared_partition_count: int = 32,
    ) -> None:
        self._whale_tenants = whale_tenants or set()
        self._shared_partition_count = shared_partition_count

    def resolve(self, tenant_id: str, entity_type: str) -> IngestionPartitionPolicy:
        if tenant_id in self._whale_tenants:
            partition_group = f"{entity_type}-{tenant_id}"
            return IngestionPartitionPolicy(
                tenant_id=tenant_id,
                entity_type=entity_type,
                partition_key=f"{partition_group}:dedicated",
                partition_group=partition_group,
                dedicated=True,
            )

        partition_id = self._stable_hash(f"{tenant_id}:{entity_type}") % self._shared_partition_count
        partition_group = f"{entity_type}-shared-{partition_id:02d}"
        return IngestionPartitionPolicy(
            tenant_id=tenant_id,
            entity_type=entity_type,
            partition_key=f"{partition_group}:{tenant_id}",
            partition_group=partition_group,
            dedicated=False,
        )

    @staticmethod
    def _stable_hash(value: str) -> int:
        return int(hashlib.sha256(value.encode("utf-8")).hexdigest(), 16)
