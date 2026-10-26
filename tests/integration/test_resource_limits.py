"""AC12：资源记录、超长记录的内存上限、低磁盘空间安全停止，以及真实数据的 100,000 条试跑。"""

import dataclasses
import hashlib
import json
import os
import tracemalloc
from pathlib import Path

import pytest

from etl_task1 import metrics
from etl_task1.checkpoint import plan_extraction_id
from etl_task1.db import run_integrity
from etl_task1.loader import load_extraction
from etl_task1.metrics import LowDiskSpace, ResourceMonitor
from etl_task1.source import verify_source

import build_fixtures as bf
from _db_helpers import dump_state

MIB = 1024 * 1024
REAL_GZ = Path(os.environ.get("SOURCE_GZIP_PATH", "/data/source/movies.txt.gz"))
REAL_MANIFEST = Path(os.environ.get("SOURCE_MANIFEST_PATH", "/data/source/movies.txt.gz.download.json"))


def _monitor(tmp_path, s):
    return ResourceMonitor(tmp_path / "resources.jsonl", s.disk_paths(), s.min_free_bytes)


def test_monitor_records_a_sample_per_batch_and_summary(conn, make_settings, verify, tmp_path):
    s = make_settings()
    mon = _monitor(tmp_path, s)
    r = load_extraction(s, verify(), conn, monitor=mon)
    mon.finish(conn)
    samples = [json.loads(line) for line in (tmp_path / "resources.jsonl").read_text().splitlines()]
    assert len(samples) == r.batches_committed == 4  # 10 条、每批 3 条
    assert [x["last_record_no"] for x in samples] == [3, 6, 9, 10]
    assert all(x["rss_bytes"] and x["peak_rss_bytes"] >= x["rss_bytes"] for x in samples)
    assert all(x["disk_free_bytes"] for x in samples)
    summ = mon.summary()
    assert summ["kind"] == "measured" and summ["records_this_run"] == 10 and summ["batches"] == 4
    assert summ["db_bytes_end"] and summ["db_growth_bytes"] is not None
    assert summ["peak_rss_bytes"] > 0 and summ["disk_free_min"]


def test_estimate_follows_the_design_formula():
    est = metrics.estimate_full({"records_this_run": 100_000, "elapsed_seconds": 10.0,
                                 "db_growth_bytes": 300 * MIB}, 7_911_684)
    assert est["kind"] == "estimate"
    assert est["estimated_load_seconds"] == round(10.0 / 100_000 * 7_911_684)
    assert est["estimated_db_bytes"] == round(300 * MIB / 100_000 * 7_911_684)
    assert metrics.estimate_full({"records_this_run": 0, "elapsed_seconds": 0}, 10) is None


def test_low_disk_before_start_writes_nothing(conn, make_settings, verify, tmp_path):
    s = make_settings(min_free_bytes=1 << 60)  # 任何磁盘都低于此阈值
    with pytest.raises(LowDiskSpace):
        load_extraction(s, verify(), conn, monitor=_monitor(tmp_path, s))
    eid = plan_extraction_id(s, verify())
    assert conn.execute("SELECT count(*) FROM review_record WHERE extraction_id = %s", (eid,)).fetchone()[0] == 0
    assert conn.execute("SELECT status FROM extraction WHERE extraction_id = %s", (eid,)).fetchone()[0] == "failed"


def test_low_disk_mid_run_stops_after_commit_and_resumes(conn, make_settings, verify, tmp_path, monkeypatch):
    s = make_settings(min_free_bytes=100)
    clean = load_extraction(s, verify(), conn)
    expected = dump_state(conn, clean.extraction_id)
    conn.execute("TRUNCATE quality_issue, quarantine_record, review_staging, review_record, etl_batch, "
                 "etl_run, extraction, source_file CASCADE")

    calls = {"n": 0}

    def shrinking(paths):
        calls["n"] += 1  # 第 1 次是启动检查；第 2 次起（第 1 批提交后）剩余空间降到阈值以下
        return {str(p): (10_000 if calls["n"] == 1 else 50) for p in paths}

    with monkeypatch.context() as m:
        m.setattr(metrics, "disk_free", shrinking)
        with pytest.raises(LowDiskSpace):
            load_extraction(s, verify(), conn, monitor=_monitor(tmp_path, s))
    eid = plan_extraction_id(s, verify())
    assert conn.execute("SELECT count(*) FROM etl_batch WHERE extraction_id = %s", (eid,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM review_record WHERE extraction_id = %s", (eid,)).fetchone()[0] == 3
    status, reason = conn.execute("SELECT status, failure_reason FROM extraction WHERE extraction_id = %s",
                                  (eid,)).fetchone()
    assert status == "failed" and "LowDiskSpace" in reason and "保留" in reason
    last = json.loads((tmp_path / "resources.jsonl").read_text().splitlines()[-1])
    assert last["last_record_no"] == 3  # 停止前最后一批的样本已落盘

    resumed = load_extraction(s, verify(), conn, monitor=_monitor(tmp_path / "again", s))
    assert (resumed.records_valid, resumed.records_quarantined) == (6, 4)
    assert dump_state(conn, eid) == expected
    assert all(v == 0 for v in run_integrity(conn, eid).values())


def test_oversize_record_memory_is_bounded_by_the_limit_not_the_record_size(conn, make_settings, tmp_path):
    big = 160 * MIB  # 远超默认 max_record_bytes（32 MiB）；内存上界应由该上限而非记录大小决定
    records = [bf.record(), bf.record(text=b"x" * big), bf.record(product=b"BBBBBBBBBB")]
    d = tmp_path / "huge"
    d.mkdir()
    gz, manifest = d / "huge.gz", d / "huge.json"
    gz.write_bytes(bf.gzip_bytes(bf.build_bytes(records)))
    bf.write_manifest(manifest, gz.read_bytes())
    s = make_settings(source_gzip_path=gz, source_manifest_path=manifest, batch_rows=10)
    assert s.max_record_bytes == 32 * MIB

    tracemalloc.start()
    try:
        r = load_extraction(s, verify_source(gz, manifest), conn)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (r.records_valid, r.records_quarantined) == (2, 1)
    # 读取器在超过上限前最多缓存 max_record_bytes，其后直接写旁路文件；再留 16 MiB 给块与对象开销
    assert peak < s.max_record_bytes + 16 * MIB, f"Python 内存峰值 {peak / MIB:.1f} MiB 随记录大小增长"
    assert peak < big / 2
    path, sha, size = conn.execute("SELECT spill_path, spill_sha256, spill_size FROM quarantine_record "
                                   "WHERE extraction_id = %s", (r.extraction_id,)).fetchone()
    spilled = s.output_dir / path
    assert size > big and spilled.stat().st_size == size
    h = hashlib.sha256()
    with open(spilled, "rb") as fh:
        for chunk in iter(lambda: fh.read(MIB), b""):
            h.update(chunk)
    assert h.hexdigest() == sha


@pytest.mark.skipif(not (REAL_GZ.exists() and REAL_MANIFEST.exists()), reason="未挂载真实数据")
def test_real_data_trial_of_100k_records(conn, base_settings):
    """前 100,000 条真实记录的隔离试跑（前缀样本，非随机；不是正式全量）。结果落到 outputs/resource-trial。"""
    out = base_settings.output_dir / "resource-trial"
    s = dataclasses.replace(base_settings, source_gzip_path=REAL_GZ, source_manifest_path=REAL_MANIFEST,
                            output_dir=out, scope="sample", sample_max_records=100_000)
    mon = ResourceMonitor(out / "resources.jsonl", s.disk_paths(), s.min_free_bytes)
    (out).mkdir(parents=True, exist_ok=True)
    (out / "resources.jsonl").write_text("")
    v = verify_source(REAL_GZ, REAL_MANIFEST)
    r = load_extraction(s, v, conn, monitor=mon)
    mon.finish(conn)
    assert r.records_valid + r.records_quarantined == 100_000
    assert r.scope_complete and not r.source_eof_reached
    assert all(x == 0 for x in run_integrity(conn, r.extraction_id).values())

    summ = mon.summary()
    summ["estimate_full_7911684"] = metrics.estimate_full(summ, 7_911_684)
    summ["records_valid"], summ["records_quarantined"] = r.records_valid, r.records_quarantined
    (out / "trial-100k-summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2) + "\n")
    assert summ["peak_rss_bytes"] < 1024 * MIB, "解析进程峰值 RSS 超过 1 GiB 目标，需分析并调整"
    assert summ["batches"] == 10 and summ["db_growth_bytes"] > 0
