import dataclasses
import json
from pathlib import Path

import psycopg
import pytest

from etl_task1.checkpoint import LoadConflict
from etl_task1.db import apply_schema, connect, run_integrity
from etl_task1.evidence import read_record_bytes
from etl_task1.loader import build_batch, commit_batch, load_extraction
from etl_task1.parser import ScanStatus, scan_records
from etl_task1.source import open_gzip

from _db_helpers import dump_state

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "task1_cases.json").read_text())


def counts(conn, eid):
    return {t: conn.execute(f"SELECT count(*) FROM {t} WHERE extraction_id = %s", (eid,)).fetchone()[0]
            for t in ("review_record", "review_staging", "quarantine_record", "quality_issue", "etl_batch")}


def test_schema_apply_is_idempotent(conn):
    assert apply_schema(conn) is False


def test_fixture_load_matches_expectations(conn, make_settings, verify):
    s = make_settings()
    r = load_extraction(s, verify(), conn)
    assert (r.outcome, r.records_valid, r.records_quarantined) == ("loaded", 6, 4)
    assert (r.scope_complete, r.source_eof_reached, r.gzip_verified) == (True, True, True)
    assert r.batches_committed == 4  # 10 条按每批 3 条
    c = counts(conn, r.extraction_id)
    assert c == {"review_record": 10, "review_staging": 6, "quarantine_record": 4,
                 "quality_issue": 9, "etl_batch": 4}
    assert all(v == 0 for v in run_integrity(conn, r.extraction_id).values())
    exp = CASES["totals"]
    q = lambda sql: conn.execute(sql, (r.extraction_id,)).fetchone()[0]
    assert q("SELECT count(DISTINCT product_id_observed) FROM review_record WHERE extraction_id = %s") == exp["distinct_products_observed"]
    assert q("SELECT count(DISTINCT reviewer_id_observed) FROM review_record WHERE extraction_id = %s") == exp["distinct_users_observed"]
    assert q("SELECT count(DISTINCT product_id) FROM review_staging WHERE extraction_id = %s") == exp["distinct_products_valid"]
    assert q("SELECT count(DISTINCT reviewer_id) FROM review_staging WHERE extraction_id = %s") == exp["distinct_users_valid"]
    assert q("SELECT count(*) FROM review_record WHERE extraction_id = %s AND reviewer_id_status = 'blank'") == exp["user_id_empty"]


def test_values_stored_per_spec(conn, make_settings, verify):
    r = load_extraction(make_settings(), verify(), conn)
    row = lambda no: conn.execute(
        "SELECT s.* FROM review_staging s JOIN review_record r USING (extraction_id, source_review_id) "
        "WHERE r.extraction_id = %s AND r.record_no = %s", (r.extraction_id, no)).fetchone()
    cols = [d.name for d in conn.execute("SELECT * FROM review_staging LIMIT 0").description]
    rec3 = dict(zip(cols, row(3)))
    assert rec3["reviewer_id_raw"] == "" and rec3["reviewer_id"] is None
    assert (rec3["helpful_votes"], rec3["total_votes"], rec3["helpfulness_ratio"]) == (0, 0, None)
    rec4 = dict(zip(cols, row(4)))
    assert (rec4["score_raw"], rec4["score"], rec4["helpfulness_raw"], rec4["helpful_votes"]) == ("6", None, "2/1", None)
    assert (rec4["review_time_raw"], rec4["review_time_unix"], rec4["review_time_utc"]) == ("bad", None, None)
    rec1 = dict(zip(cols, row(1)))
    assert rec1["review_time_utc"].isoformat() == "1970-01-01T00:00:00+00:00" and str(rec1["score"]) == "5"
    assert "\t" in rec1["review_text_raw"] and "\\" in rec1["review_text_raw"]
    rec10 = dict(zip(cols, row(10)))
    assert rec10["review_text_raw"] == "" and rec10["field_present"]["review_text"] is True
    rec9 = dict(zip(cols, row(9)))
    assert rec9["reviewer_id"] == "unknown"


def test_rerun_is_idempotent(conn, make_settings, verify):
    s = make_settings()
    first = load_extraction(s, verify(), conn)
    before = counts(conn, first.extraction_id)
    snapshot = dump_state(conn, first.extraction_id)
    second = load_extraction(s, verify(), conn)
    assert second.outcome == "reused" and second.extraction_id == first.extraction_id
    assert counts(conn, first.extraction_id) == before
    assert dump_state(conn, first.extraction_id) == snapshot
    runs = conn.execute("SELECT status FROM etl_run WHERE extraction_id = %s ORDER BY started_at",
                        (first.extraction_id,)).fetchall()
    assert [x[0] for x in runs] == ["succeeded", "reused"]  # 允许新增执行审计


def test_identical_content_records_are_both_kept(conn, make_settings, verify):
    r = load_extraction(make_settings(), verify(), conn)
    rows = conn.execute("SELECT source_review_id, raw_sha256 FROM review_record WHERE extraction_id = %s "
                        "AND record_no IN (1, 2) ORDER BY record_no", (r.extraction_id,)).fetchall()
    assert rows[0][0] != rows[1][0]  # 源记录 ID 不同
    assert rows[0][1] == rows[1][1]  # 原始字节相同，但两条都保留
    assert conn.execute("SELECT count(*) FROM review_record WHERE extraction_id = %s", (r.extraction_id,)).fetchone()[0] == 10


def test_sample_and_full_are_separate_extractions(conn, make_settings, verify):
    sample = load_extraction(make_settings(scope="sample", sample_max_records=4), verify(), conn)
    full = load_extraction(make_settings(), verify(), conn)
    assert sample.extraction_id != full.extraction_id
    assert (sample.records_valid + sample.records_quarantined) == 4
    assert (sample.scope_complete, sample.source_eof_reached, sample.gzip_verified) == (True, False, False)
    assert (full.records_valid, full.records_quarantined) == (6, 4)
    assert counts(conn, sample.extraction_id)["review_record"] == 4
    assert counts(conn, full.extraction_id)["review_record"] == 10


def test_lock_blocks_concurrent_loader(conn, base_settings, make_settings, verify):
    from etl_task1.checkpoint import ExtractionBusy, plan_extraction_id, try_lock
    s = make_settings()
    eid = plan_extraction_id(s, verify())
    with connect(base_settings) as other:
        assert try_lock(other, eid)
        with pytest.raises(ExtractionBusy):
            load_extraction(s, verify(), conn)


def _seeded(conn, make_settings, verify):
    r = load_extraction(make_settings(), verify(), conn)
    return r.extraction_id


def _first_batch_rows(make_settings, verify, eid, batch_no):
    s = make_settings()
    status = ScanStatus()
    with open_gzip(s.source_gzip_path) as fh:
        recs = list(scan_records(fh, verify().source_file_id, status))
    return build_batch(eid, batch_no, recs[:3], {}), recs


@pytest.mark.parametrize("sql,exc", [
    ("UPDATE review_staging SET score = 6 WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_staging SET helpful_votes = 5, total_votes = 1 WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_staging SET helpful_votes = -1, total_votes = 1 WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_staging SET helpful_votes = 1, total_votes = 2, helpfulness_ratio = NULL WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_staging SET helpful_votes = 1, total_votes = NULL WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_staging SET product_id = ' ', product_id_raw = ' ' WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_record SET raw_sha256 = 'xyz' WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_record SET classification = 'other' WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE review_record SET end_offset = end_offset + 1 WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("UPDATE etl_batch SET valid_count = valid_count + 1 WHERE extraction_id = %(e)s", psycopg.errors.CheckViolation),
    ("DELETE FROM review_record WHERE extraction_id = %(e)s AND record_no = 1", psycopg.errors.ForeignKeyViolation),
    ("DELETE FROM etl_batch WHERE extraction_id = %(e)s AND batch_no = 1", psycopg.errors.ForeignKeyViolation),
    ("UPDATE review_record SET classification = 'quarantined' WHERE extraction_id = %(e)s AND record_no = 1", psycopg.errors.ForeignKeyViolation),
    ("INSERT INTO quality_issue (extraction_id, issue_key, source_review_id, code, severity, rule_version) "
     "VALUES (%(e)s, repeat('a', 64), repeat('b', 64), 'x', 'info', 'v')", psycopg.errors.ForeignKeyViolation),
])
def test_constraints_reject_bad_data(conn, make_settings, verify, sql, exc):
    eid = _seeded(conn, make_settings, verify)
    with pytest.raises(exc), conn.transaction():
        conn.execute(sql, {"e": eid})


def test_valid_and_quarantine_subtables_are_exclusive(conn, make_settings, verify):
    eid = _seeded(conn, make_settings, verify)
    valid_sid = conn.execute("SELECT source_review_id FROM review_record WHERE extraction_id = %s AND record_no = 1", (eid,)).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO quarantine_record (extraction_id, source_review_id, reasons, raw_bytes, rules_version) "
                     "VALUES (%s, %s, ARRAY['x'], '\\x00', 'v')", (eid, valid_sid))
    quar_sid = conn.execute("SELECT source_review_id FROM review_record WHERE extraction_id = %s AND record_no = 5", (eid,)).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO review_staging (extraction_id, source_review_id, product_id_raw, product_id, field_present, "
                     "field_start_lines, rules_version) VALUES (%s, %s, 'A', 'A', '{}', '{}', 'v')", (eid, quar_sid))


def test_record_without_child_row_fails_at_commit(conn, make_settings, verify):
    eid = _seeded(conn, make_settings, verify)
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="no review_staging"), conn.transaction():
        conn.execute("INSERT INTO review_record (extraction_id, source_review_id, record_no, start_line, end_line, start_offset, "
                     "end_offset, byte_size, raw_sha256, classification, product_id_observed, product_id_status, "
                     "reviewer_id_status, batch_no) VALUES (%s, repeat('c', 64), 99, 1, 1, 0, 5, 5, repeat('d', 64), "
                     "'valid', 'P', 'ok', 'missing', 1)", (eid,))
    assert conn.execute("SELECT count(*) FROM review_record WHERE extraction_id = %s", (eid,)).fetchone()[0] == 10


def test_same_key_different_content_is_an_error(conn, make_settings, verify):
    eid = _seeded(conn, make_settings, verify)
    rows, _ = _first_batch_rows(make_settings, verify, eid, batch_no=1)
    run_id = conn.execute("SELECT run_id FROM etl_run LIMIT 1").fetchone()[0]
    # 批次已提交且内容一致：视为已处理而跳过
    assert commit_batch(conn, run_id, eid, rows, None) is False
    # 批次登记的哈希被改动后，同一批次号的写入必须报错而不是静默跳过
    conn.execute("UPDATE etl_batch SET batch_sha256 = repeat('0', 64) WHERE extraction_id = %s AND batch_no = 1", (eid,))
    with pytest.raises(LoadConflict, match="内容不同"):
        commit_batch(conn, run_id, eid, rows, None)
    # 新批次号但记录键已存在：唯一约束冲突转为 LoadConflict，且无半批数据
    other, _ = _first_batch_rows(make_settings, verify, eid, batch_no=1)
    other.batch_no = 99
    other.first_record_no, other.last_record_no = 1001, 1003
    other.valid_count, other.quarantined_count = 3, 0
    with pytest.raises(LoadConflict):
        commit_batch(conn, run_id, eid, other, None)
    assert conn.execute("SELECT count(*) FROM etl_batch WHERE extraction_id = %s AND batch_no = 99", (eid,)).fetchone()[0] == 0


def test_business_metadata_and_test_databases_are_isolated(base_settings):
    wrong = dataclasses.replace(base_settings, db_name="etl_movies")
    with pytest.raises(psycopg.OperationalError):
        connect(wrong)
    wrong = dataclasses.replace(base_settings, db_name="airflow_meta")
    with pytest.raises(psycopg.OperationalError):
        connect(wrong)
