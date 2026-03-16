from __future__ import annotations

from pydantic import BaseModel, Field


class StoreSnapshot(BaseModel):
    name: str
    count: int = Field(ge=0)
    checksums: dict[str, str] = Field(default_factory=dict)


class ReconciliationFinding(BaseModel):
    severity: str
    code: str
    message: str
    affected_ids: list[str] = Field(default_factory=list)


class ReconciliationReport(BaseModel):
    findings: list[ReconciliationFinding]

    @property
    def passed(self) -> bool:
        return not any(item.severity == "critical" for item in self.findings)


class ReconciliationEngine:
    def compare(self, source: StoreSnapshot, target: StoreSnapshot) -> ReconciliationReport:
        findings: list[ReconciliationFinding] = []
        if source.count != target.count:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="count_mismatch",
                    message=f"count mismatch: {source.name}={source.count}, {target.name}={target.count}",
                )
            )

        missing = sorted(set(source.checksums) - set(target.checksums))
        if missing:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="missing_documents",
                    message=f"{len(missing)} documents missing from target",
                    affected_ids=missing[:25],
                )
            )

        drifted = sorted(
            key
            for key in set(source.checksums).intersection(target.checksums)
            if source.checksums[key] != target.checksums[key]
        )
        if drifted:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="checksum_drift",
                    message=f"{len(drifted)} documents differ between source and target",
                    affected_ids=drifted[:25],
                )
            )

        return ReconciliationReport(findings=findings)
