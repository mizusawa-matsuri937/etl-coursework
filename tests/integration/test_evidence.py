import hashlib
from pathlib import Path

import pytest

from etl_task1 import loader
from etl_task1.db import run_integrity
from etl_task1.evidence import EvidenceError, read_record_bytes
from etl_task1.loader import load_extraction

import build_fixtures as bf


def test_every_loaded_record_replays_from_source(conn, make_settings, verify):
    s = make_settings()
    r = load_extraction(s, verify(), conn)
    rows = conn.execute("SELECT record_no, start_offset, end_offset, raw_sha256, classification, source_review_id "
                        "FROM review_record WHERE extraction_id = %s ORDER BY record_no", (r.extraction_id,)).fetchall()
    for no, start, end, sha, cls, sid in rows:
        raw = read_record_bytes(s.source_gzip_path, start, end)
        assert hashlib.sha256(raw).hexdigest() == sha and len(raw) == end - start
        if cls == "quarantined":
            stored = conn.execute("SELECT raw_bytes FROM quarantine_record WHERE extraction_id = %s AND source_review_id = %s",
                                  (r.extraction_id, sid)).fetchone()[0]
            assert bytes(stored) == raw  # 隔离字节与源文件逐字节一致
    bad = conn.execute("SELECT raw_bytes FROM quarantine_record q JOIN review_record r USING (extraction_id, source_review_id) "
                       "WHERE q.extraction_id = %s AND r.record_no IN (5, 8) ORDER BY r.record_no", (r.extraction_id,)).fetchall()
    assert b"\xff\xfe" in bytes(bad[0][0]) and b"\x00" in bytes(bad[1][0])


def test_issues_trace_to_fields(conn, make_settings, verify):
    r = load_extraction(make_settings(), verify(), conn)
    rows = conn.execute("SELECT rr.record_no, i.field, i.code FROM quality_issue i JOIN review_record rr "
                        "USING (extraction_id, source_review_id) WHERE i.extraction_id = %s ORDER BY 1, 2",
                        (r.extraction_id,)).fetchall()
    assert (4, "score", "invalid_score") in rows and (5, "review_text", "invalid_utf8") in rows
    assert (6, "score", "duplicate_prefix") in rows and (7, "product_id", "missing_product_id") in rows
    keys = conn.execute("SELECT count(DISTINCT issue_key), count(*) FROM quality_issue WHERE extraction_id = %s",
                        (r.extraction_id,)).fetchone()
    assert keys[0] == keys[1]


def _big_source(tmp_path):
    big = list(bf.record(text=b"x" * 5000))
    records = [bf.record(), big, bf.record(product=b"BBBBBBBBBB")]
    d = tmp_path / "big"
    d.mkdir()
    gz = d / "big.gz"
    gz.write_bytes(bf.gzip_bytes(bf.build_bytes(records)))
    manifest = d / "big.json"
    bf.write_manifest(manifest, gz.read_bytes())
    return gz, manifest


def test_oversize_record_is_spilled_and_referenced(conn, make_settings, tmp_path):
    from etl_task1.source import verify_source
    gz, manifest = _big_source(tmp_path)
    s = make_settings(source_gzip_path=gz, source_manifest_path=manifest, max_record_bytes=1000, batch_rows=10)
    r = load_extraction(s, verify_source(gz, manifest), conn)
    assert (r.records_valid, r.records_quarantined) == (2, 1)
    path, sha, size, raw, reasons = conn.execute(
        "SELECT spill_path, spill_sha256, spill_size, raw_bytes, reasons FROM quarantine_record WHERE extraction_id = %s",
        (r.extraction_id,)).fetchone()
    assert raw is None and reasons == ["oversize_record"] and path.startswith(f"evidence/{r.extraction_id}/")
    data = (s.output_dir / path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == sha and len(data) == size > 5000
    start, end = conn.execute("SELECT start_offset, end_offset FROM review_record WHERE extraction_id = %s AND record_no = 2",
                              (r.extraction_id,)).fetchone()
    assert data == read_record_bytes(gz, start, end)
    assert all(v == 0 for v in run_integrity(conn, r.extraction_id).values())
    assert not list((s.output_dir / "evidence" / r.extraction_id).glob("oversize-*"))  # 无遗留临时文件


def test_incomplete_spill_is_never_referenced(conn, make_settings, tmp_path, monkeypatch):
    from etl_task1.checkpoint import plan_extraction_id
    from etl_task1.source import verify_source
    gz, manifest = _big_source(tmp_path)
    s = make_settings(source_gzip_path=gz, source_manifest_path=manifest, max_record_bytes=1000, batch_rows=10)
    real = loader.persist_spill

    def truncating(block, dest_dir, name):
        block.spill_path.write_bytes(block.spill_path.read_bytes()[:-1])  # 模拟旁路写入不完整
        return real(block, dest_dir, name)

    monkeypatch.setattr(loader, "persist_spill", truncating)
    v = verify_source(gz, manifest)
    with pytest.raises(EvidenceError):
        load_extraction(s, v, conn)
    eid = plan_extraction_id(s, v)
    assert conn.execute("SELECT count(*) FROM review_record WHERE extraction_id = %s", (eid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM quarantine_record WHERE spill_path IS NOT NULL").fetchone()[0] == 0
    assert not list((s.output_dir / "evidence" / eid).glob("*.bin")) or all(
        p.name.startswith("oversize-") for p in (s.output_dir / "evidence" / eid).glob("*.bin"))
    monkeypatch.setattr(loader, "persist_spill", real)
    r = load_extraction(s, v, conn)  # 修复后重跑成功
    assert (r.records_valid, r.records_quarantined) == (2, 1)
