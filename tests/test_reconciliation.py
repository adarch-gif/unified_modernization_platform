from unified_modernization.reconciliation.engine import (
    BucketDocumentPage,
    BucketDigest,
    BucketedReconciliationEngine,
    DocumentFingerprint,
    RemoteBucketStore,
    ReconciliationEngine,
    StoreSnapshot,
)


def test_reconciliation_detects_missing_and_drift() -> None:
    engine = ReconciliationEngine()
    source = StoreSnapshot(
        name="source",
        count=2,
        checksums={"1": "aaa", "2": "bbb"},
    )
    target = StoreSnapshot(
        name="target",
        count=1,
        checksums={"1": "zzz"},
    )

    report = engine.compare(source, target)

    assert report.passed is False
    codes = {finding.code for finding in report.findings}
    assert "count_mismatch" in codes
    assert "missing_documents" in codes
    assert "checksum_drift" in codes


def test_reconciliation_detects_tenant_cohort_and_delete_scope_drift() -> None:
    engine = ReconciliationEngine()
    source = StoreSnapshot(
        name="source",
        documents={
            "1": DocumentFingerprint(checksum="aaa", tenant_id="t1", cohort="gold"),
            "2": DocumentFingerprint(checksum="bbb", tenant_id="t1", cohort="silver", is_deleted=True),
        },
    )
    target = StoreSnapshot(
        name="target",
        documents={
            "1": DocumentFingerprint(checksum="aaa", tenant_id="t2", cohort="gold"),
            "2": DocumentFingerprint(checksum="bbb", tenant_id="t1", cohort="silver"),
        },
    )

    report = engine.compare(source, target)

    codes = {finding.code for finding in report.findings}
    assert "tenant_scope_mismatch" in codes
    assert "delete_state_mismatch" in codes
    assert "tenant_delete_scope_mismatch" in codes


def test_bucketed_reconciliation_detects_bucket_mismatch_and_drills_down() -> None:
    engine = BucketedReconciliationEngine()
    source = StoreSnapshot(
        name="source",
        documents={
            "1": DocumentFingerprint(checksum="aaa", tenant_id="t1", cohort="gold"),
            "2": DocumentFingerprint(checksum="bbb", tenant_id="t1", cohort="gold"),
            "3": DocumentFingerprint(checksum="ccc", tenant_id="t2", cohort="silver"),
        },
    )
    target = StoreSnapshot(
        name="target",
        documents={
            "1": DocumentFingerprint(checksum="aaa", tenant_id="t1", cohort="gold"),
            "2": DocumentFingerprint(checksum="zzz", tenant_id="t1", cohort="gold"),
            "4": DocumentFingerprint(checksum="ddd", tenant_id="t2", cohort="silver"),
        },
    )

    report = engine.compare_store_snapshots(source, target, bucket_count=2)

    codes = {finding.code for finding in report.findings}
    assert "bucket_mismatch" in codes
    assert "bucket_checksum_drift" in codes or "bucket_missing_documents" in codes


def test_bucketed_snapshot_can_compare_without_document_drilldown() -> None:
    engine = BucketedReconciliationEngine()
    source_snapshot = engine.build_snapshot(
        "source",
        {"1": DocumentFingerprint(checksum="aaa")},
        bucket_count=4,
        include_bucket_documents=False,
    )
    target_snapshot = engine.build_snapshot(
        "target",
        {"1": DocumentFingerprint(checksum="bbb")},
        bucket_count=4,
        include_bucket_documents=False,
    )

    report = engine.compare_snapshots(source_snapshot, target_snapshot, drill_down=True)

    codes = {finding.code for finding in report.findings}
    assert "bucket_mismatch" in codes
    assert "bucket_drilldown_unavailable" in codes


def test_bucketed_reconciliation_recurses_before_leaf_comparison() -> None:
    engine = BucketedReconciliationEngine()
    source = StoreSnapshot(
        name="source",
        documents={
            "1": DocumentFingerprint(checksum="aaa"),
            "2": DocumentFingerprint(checksum="bbb"),
            "3": DocumentFingerprint(checksum="ccc"),
            "4": DocumentFingerprint(checksum="ddd"),
        },
    )
    target = StoreSnapshot(
        name="target",
        documents={
            "1": DocumentFingerprint(checksum="aaa"),
            "2": DocumentFingerprint(checksum="bbb"),
            "3": DocumentFingerprint(checksum="zzz"),
            "4": DocumentFingerprint(checksum="ddd"),
        },
    )

    report = engine.compare_store_snapshots(
        source,
        target,
        bucket_count=1,
        max_recursive_depth=3,
        target_leaf_size=1,
    )

    codes = {finding.code for finding in report.findings}
    assert "bucket_mismatch" in codes
    assert "bucket_checksum_drift" in codes


class _RemoteBucketStore(RemoteBucketStore):
    def __init__(self, documents: dict[str, DocumentFingerprint]) -> None:
        self._documents = documents
        self._engine = BucketedReconciliationEngine()
        self._root_bucket_count = 1024

    def fetch_bucket_digests(
        self,
        *,
        bucket_count: int,
        parent_bucket_id: str | None = None,
        depth: int = 0,
        fanout: int = 16,
    ) -> dict[str, BucketDigest]:
        if parent_bucket_id is None:
            self._root_bucket_count = bucket_count
            snapshot = self._engine.build_snapshot(
                "remote",
                self._documents,
                bucket_count=bucket_count,
                include_bucket_documents=True,
            )
            return snapshot.buckets

        parent_documents = self._documents_for_bucket(parent_bucket_id)
        child_groups = self._engine._split_documents(
            parent_documents,
            parent_bucket_id=parent_bucket_id,
            depth=depth,
            fanout=fanout,
        )
        return {
            child_bucket_id: self._engine._digest_bucket(child_bucket_id, bucket_documents)
            for child_bucket_id, bucket_documents in child_groups.items()
        }

    def fetch_bucket_documents(
        self,
        bucket_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 1000,
    ) -> BucketDocumentPage:
        matching = sorted(self._collect_bucket_documents(bucket_id).items())
        start = int(page_token or "0")
        page_items = matching[start : start + page_size]
        next_page_token = None if start + page_size >= len(matching) else str(start + page_size)
        return BucketDocumentPage(
            bucket_id=bucket_id,
            documents=dict(page_items),
            next_page_token=next_page_token,
        )

    def _collect_bucket_documents(self, bucket_id: str) -> dict[str, DocumentFingerprint]:
        return self._documents_for_bucket(bucket_id)

    def _documents_for_bucket(self, bucket_id: str) -> dict[str, DocumentFingerprint]:
        if "/child-" not in bucket_id:
            return {
                document_id: fingerprint
                for document_id, fingerprint in self._documents.items()
                if self._engine._bucket_id(document_id, self._root_bucket_count) == bucket_id
            }

        parent_bucket_id = bucket_id.rsplit("/child-", 1)[0]
        depth = bucket_id.count("/child-")
        parent_documents = self._documents_for_bucket(parent_bucket_id)
        child_groups = self._engine._split_documents(
            parent_documents,
            parent_bucket_id=parent_bucket_id,
            depth=depth,
        )
        return child_groups.get(bucket_id, {})

    def _collect_child_documents(self, parent_bucket_id: str, depth: int) -> set[str]:
        return {
            document_id
            for document_id in self._documents
            if parent_bucket_id in self._engine._split_documents(
                {document_id: self._documents[document_id]},
                parent_bucket_id=parent_bucket_id,
                depth=depth,
            )
        }


def test_remote_bucketed_reconciliation_fetches_paginated_documents_only_on_mismatch() -> None:
    engine = BucketedReconciliationEngine()
    source_store = _RemoteBucketStore(
        {
            "1": DocumentFingerprint(checksum="aaa"),
            "2": DocumentFingerprint(checksum="bbb"),
            "3": DocumentFingerprint(checksum="ccc"),
        }
    )
    target_store = _RemoteBucketStore(
        {
            "1": DocumentFingerprint(checksum="aaa"),
            "2": DocumentFingerprint(checksum="zzz"),
            "3": DocumentFingerprint(checksum="ccc"),
        }
    )

    report = engine.compare_remote_stores(
        source_store,
        target_store,
        bucket_count=1024,
        max_recursive_depth=2,
        target_leaf_size=1,
        page_size=1,
    )

    codes = {finding.code for finding in report.findings}
    assert "bucket_mismatch" in codes
    assert "bucket_checksum_drift" in codes


def test_remote_bucketed_reconciliation_handles_many_mismatched_buckets() -> None:
    engine = BucketedReconciliationEngine()
    source_store = _RemoteBucketStore(
        {
            f"doc-{index}": DocumentFingerprint(checksum=f"source-{index}")
            for index in range(32)
        }
    )
    target_store = _RemoteBucketStore(
        {
            f"doc-{index}": DocumentFingerprint(checksum=f"target-{index}")
            for index in range(32)
        }
    )

    report = engine.compare_remote_stores(
        source_store,
        target_store,
        bucket_count=1,
        max_recursive_depth=5,
        target_leaf_size=1,
        page_size=4,
    )

    codes = {finding.code for finding in report.findings}
    assert "bucket_mismatch" in codes
    assert "bucket_checksum_drift" in codes
