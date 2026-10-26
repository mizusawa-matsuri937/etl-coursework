import json
import os
import shutil
import stat
from pathlib import Path

import psycopg
import pytest

from etl_task1 import pipeline
from etl_task1.checkpoint import ExtractionBusy
from etl_task1.pipeline import StepError, is_transient
from etl_task1.source import SourceError

import build_fixtures as bf
from _pipeline_helpers import params, read_summary, run_all


@pytest.fixture(autouse=True)
def out_dir(monkeypatch, tmp_path):
    d = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(d))
    return d


def failures(out_dir, run_key="manual__test"):
    return {p.name: json.loads(p.read_text()) for p in (out_dir / "task1" / "_failures" / run_key).glob("*.json")}


def assert_failed_run(r, step, *, transient=False):
    assert not r["gate_ok"] and r["final"]["final_status"] == "failed" and not r["final"]["delivery_complete"]
    assert r["failed"][step].transient is transient
    assert r["steps"][step] is None


def test_wrong_hash_fails_before_any_load(conn, fixture_files, out_dir):
    gz, _ = fixture_files
    bad = gz.parent / bf.BAD_HASH_MANIFEST_NAME
    bad.write_text(json.dumps({**json.loads(gz.with_name(bf.MANIFEST_NAME).read_text()), "sha256": "0" * 64}))
    r = run_all(params(fixture_files, source_manifest_path=str(bad)))
    assert_failed_run(r, "verify_source")
    assert r["steps"]["extract_load"] is None
    tasks = read_summary(r["final"])["tasks"]
    assert tasks == {"verify_source": "failed", "extract_load": "not_run", "validate_counts": "not_run", "export_results": "not_run"}
    assert conn.execute("SELECT count(*) FROM review_record").fetchone()[0] == 0  # 不新增评论
    assert conn.execute("SELECT count(*) FROM source_file").fetchone()[0] == 0
    f = failures(out_dir)["verify_source-try1.json"]
    assert f["error_type"] == "SourceError" and "hash" in f["message"].lower() or "SHA" in f["message"]
    assert f["transient"] is False
    # 抽取 ID 未知，报告落在 _failures 下，但失败原因可查
    s = read_summary(r["final"])
    assert [f["task"] for f in s["failures"]] == ["verify_source"] and "_failures" in r["final"]["report_dir"]


def test_missing_input_is_deterministic_failure(conn, fixture_files, tmp_path):
    r = run_all(params(fixture_files, source_gzip_path=str(tmp_path / "nope.gz")))
    assert_failed_run(r, "verify_source")
    assert "input_missing" == r["failed"]["verify_source"].cause.code


def test_truncated_gzip_keeps_progress_and_reports_incomplete(conn, make_settings, fixture_files, out_dir, tmp_path):
    d = tmp_path / "trunc"
    shutil.copytree(Path(fixture_files[0]).parent, d)
    bf.build_failure_cases(d, (d / bf.GZIP_NAME).read_bytes())
    r = run_all(params(fixture_files, source_gzip_path=str(d / bf.TRUNCATED_GZIP_NAME),
                       source_manifest_path=str(d / bf.TRUNCATED_MANIFEST_NAME)))
    assert_failed_run(r, "extract_load")
    assert isinstance(r["failed"]["extract_load"].cause, EOFError)
    s = read_summary(r["final"])
    assert s["extraction"]["status"] == "failed" and s["extraction"]["gzip_verified"] is False
    assert "EOFError" in s["extraction"]["failure_reason"]
    assert s["tasks"] == {"verify_source": "success", "extract_load": "failed", "validate_counts": "not_run", "export_results": "not_run"}
    assert "counts" not in s
    assert any(f["task"] == "extract_load" for f in s["failures"])


def test_broken_conservation_fails_validation_and_gate(conn, fixture_files, out_dir):
    p = params(fixture_files)
    v = pipeline.verify_source_step(p)
    loaded = pipeline.extract_load_step(p, v)
    conn.execute("DELETE FROM review_staging WHERE extraction_id = %s AND source_review_id = "
                 "(SELECT source_review_id FROM review_staging WHERE extraction_id = %s LIMIT 1)",
                 (loaded["extraction_id"], loaded["extraction_id"]))
    with pytest.raises(StepError) as exc:
        pipeline.run_step("validate_counts", p, lambda: pipeline.validate_counts_step(p, loaded["extraction_id"]))
    assert not exc.value.transient and "staging_rows_equal_valid" in str(exc.value)
    final = pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_report_step(
        p, {"verify_source": v, "extract_load": loaded, "validate_counts": None, "export_results": None}))
    assert final["final_status"] == "failed" and not pipeline.gate_decision(final)[0]


def test_unwritable_report_dir_fails_finalize_and_never_claims_delivery(conn, fixture_files, out_dir):
    out_dir.mkdir(parents=True)
    out_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        p = params(fixture_files)
        v = pipeline.verify_source_step(p)
        with pytest.raises(StepError):
            pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_report_step(
                p, {"verify_source": v, "extract_load": None, "validate_counts": None, "export_results": None}),
                output_dir=out_dir)
        assert not pipeline.gate_decision(None)[0]  # 没有收尾结果，门禁不通过
    finally:
        out_dir.chmod(stat.S_IRWXU)


def test_export_failure_does_not_become_complete_delivery(conn, fixture_files, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    # 导出写不出文件：整个运行不能算完整交付
    p = params(fixture_files)
    v = pipeline.verify_source_step(p)
    loaded = pipeline.extract_load_step(p, v)
    validated = pipeline.validate_counts_step(p, loaded["extraction_id"])
    with monkeypatch.context() as m:
        m.setattr(pipeline.reporting, "write_atomic", boom)
        with pytest.raises(StepError):
            pipeline.run_step("export_results", p, lambda: pipeline.export_results_step(p, loaded["extraction_id"], validated))
    final = pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_report_step(
        p, {"verify_source": v, "extract_load": loaded, "validate_counts": validated, "export_results": None}))
    assert final["final_status"] == "failed" and final["delivery_complete"] is False


def test_database_down_still_leaves_files(fixture_files, out_dir, monkeypatch):
    monkeypatch.setenv("ETL_DB_PORT", "1")  # 连接被拒绝
    r = run_all(params(fixture_files))
    assert_failed_run(r, "extract_load", transient=True)  # 连接错误属于暂时错误，可重试
    s = read_summary(r["final"])
    assert s["database_available"] is False
    assert next(f for f in s["failures"] if f["task"] == "extract_load")["transient"] is True
    assert (Path(r["final"]["report_dir"]) / "report.md").is_file()


def test_transient_failure_is_retryable_once_then_succeeds(conn, fixture_files, tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    p = params(fixture_files, fault_injection=f"transient_once:{marker}")
    v = pipeline.verify_source_step(p)
    with pytest.raises(StepError) as exc:
        pipeline.run_step("extract_load", p, lambda: pipeline.extract_load_step(p, v))
    assert exc.value.transient
    p2 = params(fixture_files, fault_injection=f"transient_once:{marker}", try_number=2)
    r = pipeline.extract_load_step(p2, v)
    assert r["outcome"] == "loaded" and (r["records_valid"], r["records_quarantined"]) == (6, 4)


def test_fault_injection_is_refused_outside_test_env(fixture_files, monkeypatch):
    from etl_task1.config import ConfigError
    monkeypatch.setenv("ETL_ENV", "prod")
    with pytest.raises(ConfigError, match="故障注入"):
        pipeline.settings_for(params(fixture_files, fault_injection="before_commit:1"))


@pytest.mark.parametrize("exc,transient", [
    (psycopg.OperationalError("connection refused"), True),
    (psycopg.InterfaceError("closed"), True),
    (ExtractionBusy("busy"), True),
    (ConnectionResetError(), True),
    (psycopg.errors.AdminShutdown("terminating"), True),
    (psycopg.errors.InvalidPassword("bad password"), False),
    (psycopg.errors.InsufficientPrivilege("denied"), False),
    (SourceError("hash_mismatch", "x"), False),
    (EOFError("gzip"), False),
    (ValueError("x"), False),
    (FileNotFoundError("x"), False),
])
def test_error_classification(exc, transient):
    assert is_transient(exc) is transient
