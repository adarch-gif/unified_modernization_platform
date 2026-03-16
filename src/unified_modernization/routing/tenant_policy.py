from __future__ import annotations

from enum import StrEnum

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
        bucket = TenantRoutingClass.SHARED_A if sum(map(ord, tenant_id)) % 2 == 0 else TenantRoutingClass.SHARED_B
        return TenantPolicy(
            tenant_id=tenant_id,
            routing_class=bucket,
            write_alias=f"{entity_type}-{bucket.value}-write",
            read_alias=f"{entity_type}-{bucket.value}-read",
            routing_key=tenant_id,
        )
