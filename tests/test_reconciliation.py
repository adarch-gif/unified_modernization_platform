from unified_modernization.reconciliation.engine import (
    BucketedReconciliationEngine,
    DocumentFingerprint,
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
