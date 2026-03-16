from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, deque
from collections.abc import Iterable
from typing import Protocol

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
    total_affected: int = 0


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
                    message=f"{len(missing)} documents missing from target (showing first 25)",
                    affected_ids=missing[:25],
                    total_affected=len(missing),
                )
            )

        extras = sorted(target_active_ids - source_active_ids)
        if extras:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="unexpected_documents",
                    message=f"{len(extras)} extra documents present in target (showing first 25)",
                    affected_ids=extras[:25],
                    total_affected=len(extras),
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
                    message=f"{len(drifted)} documents differ between source and target (showing first 25)",
                    affected_ids=drifted[:25],
                    total_affected=len(drifted),
                )
            )

        delete_mismatches = sorted(source.deleted_document_ids() ^ target.deleted_document_ids())
        if delete_mismatches:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="delete_state_mismatch",
                    message=f"{len(delete_mismatches)} documents have mismatched delete state (showing first 25)",
                    affected_ids=delete_mismatches[:25],
                    total_affected=len(delete_mismatches),
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
                total_affected=len(mismatched_scopes),
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


class BucketDocumentPage(BaseModel):
    bucket_id: str
    documents: dict[str, DocumentFingerprint] = Field(default_factory=dict)
    next_page_token: str | None = None


class RemoteBucketStore(Protocol):
    def fetch_bucket_digests(
        self,
        *,
        bucket_count: int,
        parent_bucket_id: str | None = None,
        depth: int = 0,
        fanout: int = 16,
    ) -> dict[str, BucketDigest]:
        raise NotImplementedError

    def fetch_bucket_documents(
        self,
        bucket_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 1000,
    ) -> BucketDocumentPage:
        raise NotImplementedError


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
        max_recursive_depth: int = 3,
        target_leaf_size: int = 128,
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
                        total_affected=1,
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
                        total_affected=1,
                    )
                )
                mismatched_buckets.append(bucket_id)

        if drill_down and mismatched_buckets:
            findings.extend(
                self._drill_down(
                    source,
                    target,
                    mismatched_buckets[:max_drilldown_buckets],
                    depth=1,
                    max_recursive_depth=max_recursive_depth,
                    target_leaf_size=target_leaf_size,
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
        max_recursive_depth: int = 3,
        target_leaf_size: int = 128,
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
            max_recursive_depth=max_recursive_depth,
            target_leaf_size=target_leaf_size,
        )

    def compare_remote_stores(
        self,
        source_store: RemoteBucketStore,
        target_store: RemoteBucketStore,
        *,
        bucket_count: int = 1024,
        max_recursive_depth: int = 3,
        target_leaf_size: int = 128,
        page_size: int = 1000,
        fanout: int = 16,
        parallel_fetch_workers: int = 2,
    ) -> ReconciliationReport:
        with ThreadPoolExecutor(max_workers=parallel_fetch_workers) as executor:
            source_buckets, target_buckets = self._fetch_bucket_digest_pair(
                source_store,
                target_store,
                bucket_count=bucket_count,
                executor=executor,
            )
            source_snapshot = BucketedStoreSnapshot(
                name="source",
                bucket_count=bucket_count,
                buckets=source_buckets,
            )
            target_snapshot = BucketedStoreSnapshot(
                name="target",
                bucket_count=bucket_count,
                buckets=target_buckets,
            )
            findings: list[ReconciliationFinding] = []
            mismatched_bucket_ids: list[str] = []

            for bucket_id in sorted(set(source_snapshot.buckets).union(target_snapshot.buckets)):
                source_bucket = source_snapshot.buckets.get(bucket_id)
                target_bucket = target_snapshot.buckets.get(bucket_id)
                if source_bucket is None or target_bucket is None:
                    findings.append(
                        ReconciliationFinding(
                            severity="critical",
                            code="bucket_missing",
                            message=f"bucket {bucket_id} missing from one side of reconciliation",
                            affected_ids=[bucket_id],
                            total_affected=1,
                        )
                    )
                    mismatched_bucket_ids.append(bucket_id)
                    continue
                if source_bucket != target_bucket:
                    findings.append(
                        ReconciliationFinding(
                            severity="critical",
                            code="bucket_mismatch",
                            message=f"bucket {bucket_id} hash or counts differ between source and target",
                            affected_ids=[bucket_id],
                            total_affected=1,
                        )
                    )
                    mismatched_bucket_ids.append(bucket_id)

            findings.extend(
                self._drill_down_remote(
                    source_store,
                    target_store,
                    source_snapshot,
                    target_snapshot,
                    bucket_ids=mismatched_bucket_ids,
                    depth=1,
                    max_recursive_depth=max_recursive_depth,
                    target_leaf_size=target_leaf_size,
                    page_size=page_size,
                    fanout=fanout,
                    executor=executor,
                )
            )
            return ReconciliationReport(findings=findings)

    def _drill_down(
        self,
        source: BucketedStoreSnapshot,
        target: BucketedStoreSnapshot,
        bucket_ids: list[str],
        *,
        depth: int,
        max_recursive_depth: int,
        target_leaf_size: int,
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
                        total_affected=1,
                    )
                )
                continue

            if depth >= max_recursive_depth or max(len(source_documents), len(target_documents)) <= target_leaf_size:
                findings.extend(self._compare_document_maps(bucket_id, source_documents, target_documents))
                continue

            source_children = self._split_documents(source_documents, parent_bucket_id=bucket_id, depth=depth)
            target_children = self._split_documents(target_documents, parent_bucket_id=bucket_id, depth=depth)
            child_bucket_ids = sorted(set(source_children).union(target_children))
            child_source_snapshot = BucketedStoreSnapshot(
                name=source.name,
                bucket_count=len(child_bucket_ids) or 1,
                buckets={
                    child_id: self._digest_bucket(child_id, bucket_documents)
                    for child_id, bucket_documents in source_children.items()
                },
                bucket_documents=source_children,
            )
            child_target_snapshot = BucketedStoreSnapshot(
                name=target.name,
                bucket_count=len(child_bucket_ids) or 1,
                buckets={
                    child_id: self._digest_bucket(child_id, bucket_documents)
                    for child_id, bucket_documents in target_children.items()
                },
                bucket_documents=target_children,
            )
            findings.extend(
                self._drill_down(
                    child_source_snapshot,
                    child_target_snapshot,
                    child_bucket_ids,
                    depth=depth + 1,
                    max_recursive_depth=max_recursive_depth,
                    target_leaf_size=target_leaf_size,
                )
            )

        return findings

    def _drill_down_remote(
        self,
        source_store: RemoteBucketStore,
        target_store: RemoteBucketStore,
        source_snapshot: BucketedStoreSnapshot,
        target_snapshot: BucketedStoreSnapshot,
        *,
        bucket_ids: list[str],
        depth: int,
        max_recursive_depth: int,
        target_leaf_size: int,
        page_size: int,
        fanout: int,
        executor: ThreadPoolExecutor,
    ) -> list[ReconciliationFinding]:
        findings: list[ReconciliationFinding] = []
        work_queue: deque[tuple[str, int, dict[str, BucketDigest], dict[str, BucketDigest]]] = deque(
            (bucket_id, depth, source_snapshot.buckets, target_snapshot.buckets)
            for bucket_id in bucket_ids
        )
        while work_queue:
            bucket_id, bucket_depth, source_buckets, target_buckets = work_queue.popleft()
            source_bucket = source_buckets.get(bucket_id)
            target_bucket = target_buckets.get(bucket_id)
            if source_bucket is None or target_bucket is None:
                continue

            max_bucket_documents = max(
                source_bucket.active_count + source_bucket.deleted_count,
                target_bucket.active_count + target_bucket.deleted_count,
            )
            if bucket_depth >= max_recursive_depth or max_bucket_documents <= target_leaf_size:
                source_documents, target_documents = self._fetch_bucket_document_pair(
                    source_store,
                    target_store,
                    bucket_id=bucket_id,
                    page_size=page_size,
                    executor=executor,
                )
                findings.extend(
                    self._compare_document_maps(
                        bucket_id,
                        source_documents,
                        target_documents,
                    )
                )
                continue

            source_children, target_children = self._fetch_bucket_digest_pair(
                source_store,
                target_store,
                bucket_count=fanout,
                parent_bucket_id=bucket_id,
                depth=bucket_depth,
                fanout=fanout,
                executor=executor,
            )
            child_bucket_ids: list[str] = []
            for child_bucket_id in sorted(set(source_children).union(target_children)):
                source_child = source_children.get(child_bucket_id)
                target_child = target_children.get(child_bucket_id)
                if source_child is None or target_child is None:
                    findings.append(
                        ReconciliationFinding(
                            severity="critical",
                            code="bucket_missing",
                            message=f"bucket {child_bucket_id} missing from one side of reconciliation",
                            affected_ids=[child_bucket_id],
                            total_affected=1,
                        )
                    )
                    child_bucket_ids.append(child_bucket_id)
                    continue
                if source_child != target_child:
                    findings.append(
                        ReconciliationFinding(
                            severity="critical",
                            code="bucket_mismatch",
                            message=f"bucket {child_bucket_id} hash or counts differ between source and target",
                            affected_ids=[child_bucket_id],
                            total_affected=1,
                        )
                    )
                    child_bucket_ids.append(child_bucket_id)
            for child_bucket_id in child_bucket_ids:
                work_queue.append(
                    (
                        child_bucket_id,
                        bucket_depth + 1,
                        source_children,
                        target_children,
                    )
                )
        return findings

    def _fetch_bucket_digest_pair(
        self,
        source_store: RemoteBucketStore,
        target_store: RemoteBucketStore,
        *,
        bucket_count: int,
        executor: ThreadPoolExecutor,
        parent_bucket_id: str | None = None,
        depth: int = 0,
        fanout: int = 16,
    ) -> tuple[dict[str, BucketDigest], dict[str, BucketDigest]]:
        source_future = executor.submit(
            self._fetch_remote_bucket_digests,
            source_store,
            bucket_count=bucket_count,
            parent_bucket_id=parent_bucket_id,
            depth=depth,
            fanout=fanout,
        )
        target_future = executor.submit(
            self._fetch_remote_bucket_digests,
            target_store,
            bucket_count=bucket_count,
            parent_bucket_id=parent_bucket_id,
            depth=depth,
            fanout=fanout,
        )
        return source_future.result(), target_future.result()

    def _fetch_bucket_document_pair(
        self,
        source_store: RemoteBucketStore,
        target_store: RemoteBucketStore,
        *,
        bucket_id: str,
        page_size: int,
        executor: ThreadPoolExecutor,
    ) -> tuple[dict[str, DocumentFingerprint], dict[str, DocumentFingerprint]]:
        source_future = executor.submit(
            self._fetch_bucket_documents_remote,
            source_store,
            bucket_id,
            page_size=page_size,
        )
        target_future = executor.submit(
            self._fetch_bucket_documents_remote,
            target_store,
            bucket_id,
            page_size=page_size,
        )
        return source_future.result(), target_future.result()

    @staticmethod
    def _fetch_remote_bucket_digests(
        store: RemoteBucketStore,
        *,
        bucket_count: int,
        parent_bucket_id: str | None,
        depth: int,
        fanout: int,
    ) -> dict[str, BucketDigest]:
        return store.fetch_bucket_digests(
            bucket_count=bucket_count,
            parent_bucket_id=parent_bucket_id,
            depth=depth,
            fanout=fanout,
        )

    def _compare_document_maps(
        self,
        bucket_id: str,
        source_documents: dict[str, DocumentFingerprint],
        target_documents: dict[str, DocumentFingerprint],
    ) -> list[ReconciliationFinding]:
        findings: list[ReconciliationFinding] = []
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
                    message=f"{len(missing_ids)} documents missing in mismatched bucket {bucket_id} (showing first 25)",
                    affected_ids=missing_ids[:25],
                    total_affected=len(missing_ids),
                )
            )

        unexpected_ids = sorted(target_ids - source_ids)
        if unexpected_ids:
            findings.append(
                ReconciliationFinding(
                    severity="critical",
                    code="bucket_unexpected_documents",
                    message=f"{len(unexpected_ids)} extra documents in mismatched bucket {bucket_id} (showing first 25)",
                    affected_ids=unexpected_ids[:25],
                    total_affected=len(unexpected_ids),
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
                    message=f"{len(drifted_ids)} documents drifted in mismatched bucket {bucket_id} (showing first 25)",
                    affected_ids=drifted_ids[:25],
                    total_affected=len(drifted_ids),
                )
            )
        return findings

    def _fetch_bucket_documents_remote(
        self,
        store: RemoteBucketStore,
        bucket_id: str,
        *,
        page_size: int,
    ) -> dict[str, DocumentFingerprint]:
        documents: dict[str, DocumentFingerprint] = {}
        page_token: str | None = None
        while True:
            page = store.fetch_bucket_documents(
                bucket_id,
                page_token=page_token,
                page_size=page_size,
            )
            documents.update(page.documents)
            if page.next_page_token is None:
                break
            page_token = page.next_page_token
        return documents

    @staticmethod
    def _bucket_id(document_id: str, bucket_count: int) -> str:
        bucket = int(hashlib.md5(document_id.encode("utf-8"), usedforsecurity=False).hexdigest(), 16) % bucket_count
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
    def _split_documents(
        documents: dict[str, DocumentFingerprint],
        *,
        parent_bucket_id: str,
        depth: int,
        fanout: int = 16,
    ) -> dict[str, dict[str, DocumentFingerprint]]:
        grouped: dict[str, dict[str, DocumentFingerprint]] = {}
        for document_id, fingerprint in documents.items():
            suffix = int(hashlib.sha256(f"{parent_bucket_id}:{depth}:{document_id}".encode("utf-8")).hexdigest(), 16) % fanout
            child_bucket_id = f"{parent_bucket_id}/child-{suffix:02d}"
            grouped.setdefault(child_bucket_id, {})[document_id] = fingerprint
        return grouped

    @staticmethod
    def _hash_values(values: list[str]) -> str:
        return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_counter(counter: Counter[str]) -> str:
        normalized = [f"{key}:{counter[key]}" for key in sorted(counter)]
        return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
