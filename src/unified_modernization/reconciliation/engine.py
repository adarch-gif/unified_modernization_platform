from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable

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


class BucketDigest(BaseModel):
    bucket_id: str
    active_count: int
    deleted_count: int
    aggregate_checksum: str
    tenant_scope_checksum: str
    cohort_scope_checksum: str


class BucketedStoreSnapshot(BaseModel):
    name: str
    bucket_count: int
    buckets: dict[str, BucketDigest] = Field(default_factory=dict)
    bucket_documents: dict[str, dict[str, DocumentFingerprint]] = Field(default_factory=dict, exclude=True)


class BucketedReconciliationEngine:
    """Hash-first anti-entropy engine that drills into only mismatched buckets."""

    def build_snapshot(
        self,
        name: str,
        documents: dict[str, DocumentFingerprint] | Iterable[tuple[str, DocumentFingerprint]],
        *,
        bucket_count: int = 1024,
        include_bucket_documents: bool = False,
    ) -> BucketedStoreSnapshot:
        items = documents.items() if isinstance(documents, dict) else documents
        grouped: dict[str, dict[str, DocumentFingerprint]] = {}
        for document_id, fingerprint in items:
            bucket_id = self._bucket_id(document_id, bucket_count)
            grouped.setdefault(bucket_id, {})[document_id] = fingerprint

        buckets = {
            bucket_id: self._digest_bucket(bucket_id, bucket_documents)
            for bucket_id, bucket_documents in grouped.items()
        }
        return BucketedStoreSnapshot(
            name=name,
            bucket_count=bucket_count,
            buckets=buckets,
            bucket_documents=grouped if include_bucket_documents else {},
        )

    def compare_snapshots(
        self,
        source: BucketedStoreSnapshot,
        target: BucketedStoreSnapshot,
        *,
        drill_down: bool = True,
        max_drilldown_buckets: int = 16,
    ) -> ReconciliationReport:
        findings: list[ReconciliationFinding] = []
        all_bucket_ids = sorted(set(source.buckets).union(target.buckets))
        mismatched_buckets: list[str] = []

        for bucket_id in all_bucket_ids:
            source_bucket = source.buckets.get(bucket_id)
            target_bucket = target.buckets.get(bucket_id)
            if source_bucket is None or target_bucket is None:
                findings.append(
                    ReconciliationFinding(
                        severity="critical",
                        code="bucket_missing",
                        message=f"bucket {bucket_id} missing from one side of reconciliation",
                        affected_ids=[bucket_id],
                    )
                )
                mismatched_buckets.append(bucket_id)
                continue
            if source_bucket != target_bucket:
                findings.append(
                    ReconciliationFinding(
                        severity="critical",
                        code="bucket_mismatch",
                        message=f"bucket {bucket_id} hash or counts differ between source and target",
                        affected_ids=[bucket_id],
                    )
                )
                mismatched_buckets.append(bucket_id)

        if drill_down and mismatched_buckets:
            findings.extend(
                self._drill_down(
                    source,
                    target,
                    mismatched_buckets[:max_drilldown_buckets],
                )
            )

        return ReconciliationReport(findings=findings)

    def compare_store_snapshots(
        self,
        source: StoreSnapshot,
        target: StoreSnapshot,
        *,
        bucket_count: int = 1024,
        max_drilldown_buckets: int = 16,
    ) -> ReconciliationReport:
        source_bucketed = self.build_snapshot(
            source.name,
            source.documents,
            bucket_count=bucket_count,
            include_bucket_documents=True,
        )
        target_bucketed = self.build_snapshot(
            target.name,
            target.documents,
            bucket_count=bucket_count,
            include_bucket_documents=True,
        )
        return self.compare_snapshots(
            source_bucketed,
            target_bucketed,
            drill_down=True,
            max_drilldown_buckets=max_drilldown_buckets,
        )

    def _drill_down(
        self,
        source: BucketedStoreSnapshot,
        target: BucketedStoreSnapshot,
        bucket_ids: list[str],
    ) -> list[ReconciliationFinding]:
        findings: list[ReconciliationFinding] = []
        for bucket_id in bucket_ids:
            source_documents = source.bucket_documents.get(bucket_id)
            target_documents = target.bucket_documents.get(bucket_id)
            if source_documents is None or target_documents is None:
                findings.append(
                    ReconciliationFinding(
                        severity="warning",
                        code="bucket_drilldown_unavailable",
                        message=f"bucket {bucket_id} differs but detailed documents were not provided",
                        affected_ids=[bucket_id],
                    )
                )
                continue

            source_ids = {
                document_id
                for document_id, fingerprint in source_documents.items()
                if not fingerprint.is_deleted
            }
            target_ids = {
                document_id
                for document_id, fingerprint in target_documents.items()
                if not fingerprint.is_deleted
            }

            missing_ids = sorted(source_ids - target_ids)
            if missing_ids:
                findings.append(
                    ReconciliationFinding(
                        severity="critical",
                        code="bucket_missing_documents",
                        message=f"{len(missing_ids)} documents missing in mismatched bucket {bucket_id}",
                        affected_ids=missing_ids[:25],
                    )
                )

            unexpected_ids = sorted(target_ids - source_ids)
            if unexpected_ids:
                findings.append(
                    ReconciliationFinding(
                        severity="critical",
                        code="bucket_unexpected_documents",
                        message=f"{len(unexpected_ids)} extra documents in mismatched bucket {bucket_id}",
                        affected_ids=unexpected_ids[:25],
                    )
                )

            shared_ids = sorted(source_ids.intersection(target_ids))
            drifted_ids = [
                document_id
                for document_id in shared_ids
                if source_documents[document_id].checksum != target_documents[document_id].checksum
            ]
            if drifted_ids:
                findings.append(
                    ReconciliationFinding(
                        severity="critical",
                        code="bucket_checksum_drift",
                        message=f"{len(drifted_ids)} documents drifted in mismatched bucket {bucket_id}",
                        affected_ids=drifted_ids[:25],
                    )
                )

        return findings

    @staticmethod
    def _bucket_id(document_id: str, bucket_count: int) -> str:
        bucket = int(hashlib.md5(document_id.encode("utf-8")).hexdigest(), 16) % bucket_count
        width = max(4, len(str(bucket_count)))
        return f"bucket-{bucket:0{width}d}"

    def _digest_bucket(self, bucket_id: str, documents: dict[str, DocumentFingerprint]) -> BucketDigest:
        active_count = 0
        deleted_count = 0
        tenant_scope: Counter[str] = Counter()
        cohort_scope: Counter[str] = Counter()
        normalized_entries: list[str] = []

        for document_id, fingerprint in sorted(documents.items()):
            if fingerprint.is_deleted:
                deleted_count += 1
            else:
                active_count += 1
            if fingerprint.tenant_id:
                tenant_scope[fingerprint.tenant_id] += 1
            if fingerprint.cohort:
                cohort_scope[fingerprint.cohort] += 1
            normalized_entries.append(
                "|".join(
                    [
                        document_id,
                        fingerprint.checksum,
                        fingerprint.tenant_id or "",
                        fingerprint.cohort or "",
                        "1" if fingerprint.is_deleted else "0",
                    ]
                )
            )

        return BucketDigest(
            bucket_id=bucket_id,
            active_count=active_count,
            deleted_count=deleted_count,
            aggregate_checksum=self._hash_values(normalized_entries),
            tenant_scope_checksum=self._hash_counter(tenant_scope),
            cohort_scope_checksum=self._hash_counter(cohort_scope),
        )

    @staticmethod
    def _hash_values(values: list[str]) -> str:
        return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_counter(counter: Counter[str]) -> str:
        normalized = [f"{key}:{counter[key]}" for key in sorted(counter)]
        return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
