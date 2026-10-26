import dataclasses
import gzip
import hashlib
import json

import pytest

from etl_task1.checkpoint import CheckpointMismatch, plan_extraction_id
from etl_task1.db import run_integrity
from etl_task1.loader import InjectedFault, load_extraction
from etl_task1.source import verify_source

import build_fixtures as bf
from _db_helpers import dump_state


def totals(conn, eid):
    return conn.execute("SELECT count(*), count(*) FILTER (WHERE classification = 'valid'), "
                        "count(*) FILTER (WHERE classification = 'quarantined') FROM review_record "
                        "WHERE extraction_id = %s", (eid,)).fetchone()


def baseline(conn, make_settings, verify):
    r = load_extraction(make_settings(), verify(), conn)
    snap = dump_state(conn, r.extraction_id)
    conn.execute("TRUNCATE quality_issue, quarantine_record, review_staging, review_record, etl_batch, etl_run, extraction, source_file CASCADE")
    return snap


def test_fault_before_commit_keeps_first_batch_then_resumes_to_same_result(conn, make_settings, verify):
    expected = baseline(conn, make_settings, verify)
    faulty = make_settings(env="test", fault_injection="before_commit:2")
    with pytest.raises(InjectedFault):
        load_extraction(faulty, verify(), conn)
    eid = plan_extraction_id(faulty, verify())
    assert totals(conn, eid)[0] == 3  # 仅第 1 批的 3 条
    assert conn.execute("SELECT count(*) FROM etl_batch WHERE extraction_id = %s", (eid,)).fetchone()[0] == 1
    st = conn.execute("SELECT status, failure_reason FROM extraction WHERE extraction_id = %s", (eid,)).fetchone()
    assert st[0] == "failed" and "InjectedFault" in st[1]
    r = load_extraction(make_settings(), verify(), conn)
    assert r.extraction_id == eid and r.outcome == "loaded"
    assert (r.records_valid, r.records_quarantined) == (6, 4)
    assert dump_state(conn, eid) == expected
    assert all(v == 0 for v in run_integrity(conn, eid).values())


def test_lost_commit_ack_resumes_without_duplicates(conn, make_settings, verify):
    expected = baseline(conn, make_settings, verify)
    faulty = make_settings(env="test", fault_injection="after_commit:2")
    with pytest.raises(InjectedFault):
        load_extraction(faulty, verify(), conn)
    eid = plan_extraction_id(faulty, verify())
    assert totals(conn, eid)[0] == 6  # 第 2 批已提交，只是客户端没有收到确认
    r = load_extraction(make_settings(), verify(), conn)
    assert (r.records_valid, r.records_quarantined) == (6, 4)
    assert dump_state(conn, eid) == expected
    runs = [x[0] for x in conn.execute("SELECT status FROM etl_run WHERE extraction_id = %s ORDER BY started_at", (eid,))]
    assert runs == ["failed", "succeeded"]


def test_resume_after_every_possible_failure_point(conn, make_settings, verify):
    expected = baseline(conn, make_settings, verify)
    for kind in ("before_commit", "after_commit"):
        for n in (1, 2, 3, 4):
            faulty = make_settings(env="test", fault_injection=f"{kind}:{n}")
            with pytest.raises(InjectedFault):
                load_extraction(faulty, verify(), conn)
            r = load_extraction(make_settings(), verify(), conn)
            assert dump_state(conn, r.extraction_id) == expected, (kind, n)
            conn.execute("TRUNCATE quality_issue, quarantine_record, review_staging, review_record, etl_batch, etl_run, extraction, source_file CASCADE")


def test_changed_config_cannot_reuse_checkpoint(conn, make_settings, verify):
    faulty = make_settings(env="test", fault_injection="after_commit:1")
    with pytest.raises(InjectedFault):
        load_extraction(faulty, verify(), conn)
    old = plan_extraction_id(faulty, verify())
    changed = make_settings(batch_rows=4)
    with pytest.raises(CheckpointMismatch):
        load_extraction(changed, verify(), conn, expected_extraction_id=old)
    # 不指定期望时，新配置是另一个抽取，从头开始，不沿用旧检查点
    r = load_extraction(changed, verify(), conn)
    assert r.extraction_id != old and totals(conn, r.extraction_id) == (10, 6, 4)
    assert totals(conn, old)[0] == 3 and conn.execute(
        "SELECT status FROM extraction WHERE extraction_id = %s", (old,)).fetchone()[0] == "failed"


def test_changed_input_cannot_reuse_checkpoint(conn, make_settings, verify, tmp_path):
    faulty = make_settings(env="test", fault_injection="after_commit:1")
    with pytest.raises(InjectedFault):
        load_extraction(faulty, verify(), conn)
    old = plan_extraction_id(faulty, verify())
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    gz = other_dir / "other.gz"
    gz.write_bytes(bf.gzip_bytes(bf.build_bytes(bf.RECORDS[:9])))
    manifest = other_dir / "other.json"
    bf.write_manifest(manifest, gz.read_bytes())
    s = make_settings(source_gzip_path=gz, source_manifest_path=manifest)
    v = verify_source(gz, manifest)
    with pytest.raises(CheckpointMismatch):
        load_extraction(s, v, conn, expected_extraction_id=old)


def test_tampered_checkpoint_is_detected(conn, make_settings, verify):
    faulty = make_settings(env="test", fault_injection="after_commit:1")
    with pytest.raises(InjectedFault):
        load_extraction(faulty, verify(), conn)
    eid = plan_extraction_id(faulty, verify())
    conn.execute("UPDATE review_record SET raw_sha256 = repeat('0', 64) WHERE extraction_id = %s AND record_no = 3", (eid,))
    with pytest.raises(CheckpointMismatch, match="不一致"):
        load_extraction(make_settings(), verify(), conn)


def _truncated_source(tmp_path, records):
    bad = tmp_path / "bad"
    bad.mkdir()
    gz = bad / "t.gz"
    gz.write_bytes(bf.gzip_bytes(bf.build_bytes(records))[:-8])  # 去掉 CRC 与长度尾部
    manifest = bad / "t.json"
    bf.write_manifest(manifest, gz.read_bytes())
    return gz, manifest


def test_truncated_gzip_fails_and_never_reports_success(conn, make_settings, tmp_path):
    import base64
    import random
    rng = random.Random(7)
    # 每条约 270 KB 的不可压缩正文：文件远大于读取块，尾部错误只在读到末尾时才出现
    records = [bf.record(text=base64.b64encode(rng.randbytes(200_000))) for _ in range(30)]
    gz, manifest = _truncated_source(tmp_path, records)
    s = make_settings(source_gzip_path=gz, source_manifest_path=manifest)
    v = verify_source(gz, manifest)
    with pytest.raises(EOFError):
        load_extraction(s, v, conn)
    eid = plan_extraction_id(s, v)
    status, eof, verified = conn.execute(
        "SELECT status, source_eof_reached, gzip_verified FROM extraction WHERE extraction_id = %s", (eid,)).fetchone()
    assert (status, eof, verified) == ("failed", False, False)
    committed = totals(conn, eid)[0]
    assert 0 < committed < 30  # 已提交批次保留，但不完整
    assert conn.execute("SELECT count(*) FROM etl_batch WHERE extraction_id = %s", (eid,)).fetchone()[0] == committed // 3
    run = conn.execute("SELECT status, failure_reason FROM etl_run WHERE extraction_id = %s", (eid,)).fetchone()
    assert run[0] == "failed" and "EOFError" in run[1]


def test_truncated_small_gzip_commits_nothing(conn, make_settings, tmp_path):
    gz, manifest = _truncated_source(tmp_path, bf.RECORDS)
    s = make_settings(source_gzip_path=gz, source_manifest_path=manifest)
    v = verify_source(gz, manifest)
    with pytest.raises(EOFError):
        load_extraction(s, v, conn)
    assert totals(conn, plan_extraction_id(s, v))[0] == 0


def test_full_extraction_cannot_be_marked_complete_without_eof(conn, make_settings, verify):
    import psycopg
    r = load_extraction(make_settings(), verify(), conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("UPDATE extraction SET gzip_verified = false WHERE extraction_id = %s", (r.extraction_id,))


def test_run_left_running_by_a_killed_process_is_closed_by_the_next_run(conn, make_settings, verify):
    faulty = make_settings(env="test", fault_injection="after_commit:1")
    with pytest.raises(InjectedFault):
        load_extraction(faulty, verify(), conn)
    eid = plan_extraction_id(faulty, verify())
    # 模拟进程被直接杀死：运行记录停在 running，抽取状态也未登记
    conn.execute("UPDATE etl_run SET status = 'running', finished_at = NULL, failure_reason = NULL "
                 "WHERE extraction_id = %s", (eid,))
    load_extraction(make_settings(), verify(), conn)
    rows = conn.execute("SELECT status, failure_reason, batches_committed FROM etl_run "
                        "WHERE extraction_id = %s ORDER BY started_at", (eid,)).fetchall()
    assert [r[0] for r in rows] == ["failed", "succeeded"]
    assert "进程中断" in rows[0][1] and rows[0][2] == 1
