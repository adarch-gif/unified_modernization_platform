from unified_modernization.reconciliation.engine import DocumentFingerprint, ReconciliationEngine, StoreSnapshot


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
