"""任务 2 清单流程：按 DAG 顺序执行步骤，验证导出、失败记录、重试分类与门禁（AC01、AC07、AC08）。"""

from __future__ import annotations

import csv
import hashlib
import json
import stat
from pathlib import Path

import pytest

from etl_task1.loader import load_extraction
from etl_task2 import pipeline
from etl_task2.pipeline import RunParams, StepError

EXPORT_COLUMNS = ["asin", "original_url", "url_issue", "url_template_version", "status", "final_url",
                  "http_status", "attempt_count", "fetched_at", "snapshot_path", "content_hash", "status_reason",
                  "in_valid_records", "records_total", "records_valid", "records_quarantined",
                  "first_record_no", "last_record_no", "earliest_review_time", "latest_review_time"]


@pytest.fixture(autouse=True)
def out_dir(monkeypatch, tmp_path):
    d = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(d))
    return d


@pytest.fixture
def eid(conn, make_settings, verify):
    return load_extraction(make_settings(), verify(), conn).extraction_id


def run_all(p: RunParams) -> dict:
    """顺序执行，等价于 DAG：上游失败时记录 None，收尾与门禁照常运行。"""
    out: dict = {}
    failed: dict[str, StepError] = {}

    def attempt(name, fn):
        if fn is None:  # 上游失败：Airflow 中该任务为 upstream_failed，不运行也不留失败记录
            out[name] = None
            return
        try:
            out[name] = pipeline.run_step(name, p, fn)
        except StepError as exc:
            out[name] = None
            failed[name] = exc

    attempt("check_extraction", lambda: pipeline.check_extraction_step(p))
    checked = out["check_extraction"]
    attempt("build_manifest", (lambda: pipeline.build_manifest_step(p, checked)) if checked else None)
    ok = checked is not None and out["build_manifest"] is not None
    attempt("verify_manifest", (lambda: pipeline.verify_manifest_step(p, checked["extraction_id"])) if ok else None)
    ok = ok and out["verify_manifest"] is not None
    attempt("export_manifest", (lambda: pipeline.export_manifest_step(
        p, checked["extraction_id"], checked["build_id"], out["verify_manifest"])) if ok else None)
    final = pipeline.run_step("finalize_report", p,
                              lambda: pipeline.finalize_step(p, {n: out.get(n) for n in pipeline.STEP_NAMES}))
    gate_ok, reason = pipeline.gate_decision(final)
    return {"steps": out, "failed": failed, "final": final, "gate_ok": gate_ok, "gate_reason": reason}


def summary_of(r: dict) -> dict:
    return json.loads((Path(r["final"]["report_dir"]) / "summary.json").read_text(encoding="utf-8"))


def failure_files(out_dir: Path, run_key: str = "manual__t2") -> dict[str, dict]:
    return {p.name: json.loads(p.read_text()) for p in (out_dir / "task2" / "_failures" / run_key).glob("*.json")}


def manifest_rows(conn) -> int:
    return conn.execute("SELECT count(*) FROM task2.product_manifest").fetchone()[0]


# AC08
def test_successful_run_exports_csv_and_consistent_summary(conn, eid, out_dir):
    r = run_all(RunParams(run_key="manual__t2", extraction_id=eid))
    assert r["gate_ok"], r["gate_reason"]
    s = summary_of(r)
    assert s["final_status"] == "succeeded" and s["delivery_complete"] is True
    assert set(s["tasks"].values()) == {"success"}
    assert (s["manifest_rows"], s["products_observed"], s["products_valid"]) == (6, 6, 4)
    assert s["in_valid_distribution"] == {"true": 4, "false": 2} and s["url_issue_rows"] == 0
    assert s["status_distribution"]["not_collected"] == 6 and sum(s["status_distribution"].values()) == 6
    assert s["build"]["status"] == "succeeded" and s["build"]["inserted_rows"] == 6

    directory = Path(r["final"]["report_dir"])
    assert directory == out_dir / "task2" / eid / s["build_id"] / "manual__t2"
    csv_path = directory / pipeline.EXPORT_NAME
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == EXPORT_COLUMNS and len(rows) == 7
    assert [row[0] for row in rows[1:]] == [c * 10 for c in "ABCDEF"]
    [export] = s["exports"]
    assert export["rows"] == 6 and export["bytes"] == csv_path.stat().st_size
    assert export["sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    db_rows = conn.execute("SELECT count(*), count(*) FILTER (WHERE in_valid_records) FROM task2.product_manifest "
                           "WHERE extraction_id = %s", (eid,)).fetchone()
    assert db_rows == (6, 4)
    assert (directory / "report.md").read_text().count("not_collected") == 1


def test_export_contains_no_review_text(conn, eid, out_dir):
    r = run_all(RunParams(run_key="manual__t2", extraction_id=eid))
    texts = [t for (t,) in conn.execute("SELECT review_text_raw FROM review_staging WHERE extraction_id = %s "
                                        "AND length(review_text_raw) > 3", (eid,)).fetchall()]
    assert texts
    data = (Path(r["final"]["report_dir"]) / pipeline.EXPORT_NAME).read_text(encoding="utf-8")
    assert not any(t in data for t in texts)


def test_rerun_is_reused_and_still_succeeds(conn, eid):
    run_all(RunParams(run_key="manual__first", extraction_id=eid))
    r = run_all(RunParams(run_key="manual__second", extraction_id=eid))
    assert r["gate_ok"] and summary_of(r)["build"]["status"] == "reused"
    assert manifest_rows(conn) == 6


# AC01
@pytest.mark.parametrize("bad_id", [None, "not-an-id", "0" * 64])
def test_bad_extraction_id_fails_check_without_retry(conn, out_dir, bad_id):
    r = run_all(RunParams(run_key="manual__t2", extraction_id=bad_id))
    assert not r["gate_ok"] and r["final"]["final_status"] == "failed"
    assert r["failed"]["check_extraction"].transient is False
    assert summary_of(r)["tasks"] == {"check_extraction": "failed", "build_manifest": "not_run",
                                      "verify_manifest": "not_run", "export_manifest": "not_run"}
    f = failure_files(out_dir)["check_extraction-try1.json"]
    assert f["transient"] is False and f["error_type"] in ("ConfigError", "ManifestInputError")
    assert "_failures" in r["final"]["report_dir"]
    assert manifest_rows(conn) == 0


def test_sample_extraction_needs_allow_sample(conn, make_settings, verify, out_dir):
    sample = load_extraction(make_settings(scope="sample", sample_max_records=5), verify(), conn).extraction_id
    r = run_all(RunParams(run_key="manual__t2", extraction_id=sample))
    assert not r["gate_ok"] and r["failed"]["check_extraction"].transient is False
    assert "样本" in failure_files(out_dir)["check_extraction-try1.json"]["message"]
    assert manifest_rows(conn) == 0
    ok = run_all(RunParams(run_key="manual__t2_ok", extraction_id=sample, allow_sample=True))
    assert ok["gate_ok"] and summary_of(ok)["scope"] == "sample"


# AC07
def test_transient_failure_is_retryable_then_succeeds(conn, eid, out_dir, tmp_path):
    p = RunParams(run_key="manual__t2", extraction_id=eid, fault_injection=f"transient_once:{tmp_path / 'marker'}")
    checked = pipeline.check_extraction_step(p)
    with pytest.raises(StepError) as exc:
        pipeline.run_step("build_manifest", p, lambda: pipeline.build_manifest_step(p, checked))
    assert exc.value.transient is True and manifest_rows(conn) == 0
    retry = RunParams(**{**p.__dict__, "try_number": 2})
    built = pipeline.run_step("build_manifest", retry, lambda: pipeline.build_manifest_step(retry, checked))
    assert built["status"] == "succeeded" and manifest_rows(conn) == 6
    files = failure_files(out_dir)
    assert set(files) == {"build_manifest-try1.json"} and files["build_manifest-try1.json"]["transient"] is True


def test_failure_before_commit_is_deterministic_and_leaves_no_rows(conn, eid, out_dir):
    r = run_all(RunParams(run_key="manual__t2", extraction_id=eid, fault_injection="before_commit"))
    assert not r["gate_ok"] and r["failed"]["build_manifest"].transient is False
    assert manifest_rows(conn) == 0
    assert conn.execute("SELECT status FROM task2.manifest_run").fetchall() == [("failed",)]
    assert summary_of(r)["tasks"]["verify_manifest"] == "not_run"


def test_fault_injection_is_rejected_outside_test_env(eid, monkeypatch):
    monkeypatch.setenv("ETL_ENV", "prod")
    p = RunParams(run_key="manual__t2", extraction_id=eid, fault_injection="before_commit")
    with pytest.raises(StepError) as exc:
        pipeline.run_step("check_extraction", p, lambda: pipeline.check_extraction_step(p))
    assert exc.value.transient is False and "ETL_ENV=test" in str(exc.value)


def test_verification_detects_missing_rows(conn, eid):
    p = RunParams(run_key="manual__t2", extraction_id=eid)
    pipeline.build_manifest_step(p, pipeline.check_extraction_step(p))
    conn.execute("DELETE FROM task2.product_manifest WHERE asin = 'CCCCCCCCCC'")
    with pytest.raises(StepError) as exc:
        pipeline.run_step("verify_manifest", p, lambda: pipeline.verify_manifest_step(p, eid))
    assert exc.value.transient is False and "observed" in str(exc.value)


def test_export_failure_is_not_complete_delivery(conn, eid, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    p = RunParams(run_key="manual__t2", extraction_id=eid)
    monkeypatch.setattr(pipeline.reporting, "export_csv", boom)
    r = run_all(p)
    assert not r["gate_ok"] and r["failed"]["export_manifest"].transient is False
    s = summary_of(r)
    assert s["tasks"]["export_manifest"] == "failed" and s["delivery_complete"] is False and s["exports"] == []


def test_unwritable_output_dir_fails_finalize_and_gate(conn, eid, out_dir):
    out_dir.mkdir(parents=True)
    out_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        p = RunParams(run_key="manual__t2", extraction_id=eid)
        with pytest.raises(StepError):
            pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_step(p, {}), output_dir=out_dir)
        assert not pipeline.gate_decision(None)[0]
    finally:
        out_dir.chmod(stat.S_IRWXU)
