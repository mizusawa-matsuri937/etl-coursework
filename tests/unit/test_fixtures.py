import json
from pathlib import Path

from etl_task1.parser import ScanStatus, scan_records
from etl_task1.source import open_gzip, verify_source

import build_fixtures as bf

FIXTURES = Path(bf.__file__).parent
CASES = json.loads((FIXTURES / "task1_cases.json").read_text(encoding="utf-8"))


def scan_fixture(tmp_path):
    gz, manifest = bf.build(tmp_path)
    verification = verify_source(gz, manifest)
    status = ScanStatus()
    with open_gzip(gz) as fh:
        records = list(scan_records(fh, verification.source_file_id, status))
    return records, status


def test_committed_fixture_matches_builder(tmp_path):
    gz, manifest = bf.build(tmp_path)
    assert gz.read_bytes() == (bf.DATA_DIR / bf.GZIP_NAME).read_bytes()
    assert manifest.read_text() == (bf.DATA_DIR / bf.MANIFEST_NAME).read_text()


def test_per_record_expectations(tmp_path):
    records, _ = scan_fixture(tmp_path)
    assert len(records) == len(CASES["records"])
    for rec, exp in zip(records, CASES["records"]):
        assert rec.block.record_no == exp["record_no"]
        assert rec.status == exp["status"], exp
        assert list(rec.quarantine_reasons) == exp.get("reasons", [])
        assert rec.observed_product_id == exp["product"]
        assert rec.observed_reviewer_id == exp["user"]
        if "product_status" in exp:
            assert rec.product_id_status == exp["product_status"]
        if "user_status" in exp:
            assert rec.reviewer_id_status == exp["user_status"]
        assert [i.code for i in rec.issues] == exp["issues"]


def test_totals(tmp_path):
    records, status = scan_fixture(tmp_path)
    valid = [r for r in records if r.is_valid]
    exp = CASES["totals"]
    assert len(records) == exp["records_read"] == status.records_read
    assert len(valid) == exp["records_valid"]
    assert len(records) - len(valid) == exp["records_quarantined"]
    assert len({r.observed_product_id for r in records if r.observed_product_id}) == exp["distinct_products_observed"]
    assert len({r.observed_reviewer_id for r in records if r.observed_reviewer_id}) == exp["distinct_users_observed"]
    assert len({r.observed_product_id for r in valid if r.observed_product_id}) == exp["distinct_products_valid"]
    assert len({r.observed_reviewer_id for r in valid if r.observed_reviewer_id}) == exp["distinct_users_valid"]
    assert sum(r.reviewer_id_status == "blank" and r.present["reviewer_id"] for r in records) == exp["user_id_empty"]
    assert sum(r.reviewer_id_status == "missing" for r in records) == exp["user_id_missing"]
    assert (status.source_eof_reached, status.gzip_verified, status.scope_complete) == (True, True, True)
