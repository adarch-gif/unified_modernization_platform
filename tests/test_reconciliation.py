from unified_modernization.reconciliation.engine import ReconciliationEngine, StoreSnapshot


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
