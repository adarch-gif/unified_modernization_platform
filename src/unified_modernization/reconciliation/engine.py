from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field, model_validator


class DocumentFingerprint(BaseModel):
    checksum: str
    tenant_id: str | None = None
    cohort: str | None = None
    is_deleted: bool = False


class StoreSnapshot(BaseModel):
    name: str
    count: int | None = Field(default=None, ge=0)
    checksums: dict[str, str] = Field(default_factory=dict)
    documents: dict[str, DocumentFingerprint] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_documents(self) -> "StoreSnapshot":
        if not self.documents and self.checksums:
            self.documents = {
                document_id: DocumentFingerprint(checksum=checksum)
                for document_id, checksum in self.checksums.items()
            }
        elif self.documents and not self.checksums:
            self.checksums = {
                document_id: fingerprint.checksum
                for document_id, fingerprint in self.documents.items()
                if not fingerprint.is_deleted
            }
        return self

    @property
    def effective_count(self) -> int:
        if self.count is not None:
            return self.count
        return len([fingerprint for fingerprint in self.documents.values() if not fingerprint.is_deleted])

    def active_document_ids(self) -> set[str]:
        if self.documents:
            return {
                document_id
                for document_id, fingerprint in self.documents.items()
                if not fingerprint.is_deleted
            }
        return set(self.checksums)

    def deleted_document_ids(self) -> set[str]:
        return {
            document_id
            for document_id, fingerprint in self.documents.items()
            if fingerprint.is_deleted
        }

    def scope_counts(self, *, attribute: str) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for fingerprint in self.documents.values():
            if fingerprint.is_deleted:
                continue
            value = getattr(fingerprint, attribute)
            if value:
                counter[str(value)] += 1
        return dict(counter)

    def delete_scope_counts(self, *, attribute: str) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for fingerprint in self.documents.values():
            if not fingerprint.is_deleted:
                continue
            value = getattr(fingerprint, attribute)
            if value:
                counter[str(value)] += 1
        return dict(counter)


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
        if source.effective_count != target.effective_count:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="count_mismatch",
                    message=f"count mismatch: {source.name}={source.effective_count}, {target.name}={target.effective_count}",
                )
            )

        source_active_ids = source.active_document_ids()
        target_active_ids = target.active_document_ids()

        missing = sorted(source_active_ids - target_active_ids)
        if missing:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="missing_documents",
                    message=f"{len(missing)} documents missing from target",
                    affected_ids=missing[:25],
                )
            )

        extras = sorted(target_active_ids - source_active_ids)
        if extras:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="unexpected_documents",
                    message=f"{len(extras)} extra documents present in target",
                    affected_ids=extras[:25],
                )
            )

        drifted = sorted(
            key
            for key in source_active_ids.intersection(target_active_ids)
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

        delete_mismatches = sorted(source.deleted_document_ids() ^ target.deleted_document_ids())
        if delete_mismatches:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="delete_state_mismatch",
                    message=f"{len(delete_mismatches)} documents have mismatched delete state",
                    affected_ids=delete_mismatches[:25],
                )
            )

        findings.extend(self._compare_scope(source, target, attribute="tenant_id", code="tenant_scope_mismatch"))
        findings.extend(self._compare_scope(source, target, attribute="cohort", code="cohort_scope_mismatch"))
        findings.extend(
            self._compare_scope(
                source,
                target,
                attribute="tenant_id",
                code="tenant_delete_scope_mismatch",
                deleted_only=True,
            )
        )

        return ReconciliationReport(findings=findings)

    def _compare_scope(
        self,
        source: StoreSnapshot,
        target: StoreSnapshot,
        *,
        attribute: str,
        code: str,
        deleted_only: bool = False,
    ) -> list[ReconciliationFinding]:
        source_counts = source.delete_scope_counts(attribute=attribute) if deleted_only else source.scope_counts(attribute=attribute)
        target_counts = target.delete_scope_counts(attribute=attribute) if deleted_only else target.scope_counts(attribute=attribute)
        mismatched_scopes = sorted(
            scope
            for scope in set(source_counts).union(target_counts)
            if source_counts.get(scope, 0) != target_counts.get(scope, 0)
        )
        if not mismatched_scopes:
            return []
        return [
            ReconciliationFinding(
                severity="critical",
                code=code,
                message=(
                    f"{len(mismatched_scopes)} {attribute} scopes differ between "
                    f"{source.name} and {target.name}"
                ),
                affected_ids=mismatched_scopes[:25],
            )
        ]
