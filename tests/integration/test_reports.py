import json
import os
from pathlib import Path

import pytest

from etl_task1 import pipeline, reporting

from _pipeline_helpers import params, read_summary, run_all


@pytest.fixture(autouse=True)
def out_dir(monkeypatch, tmp_path):
    d = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(d))
    return d


def test_sample_run_writes_complete_report(conn, fixture_files, out_dir):
    r = run_all(params(fixture_files))
    assert r["gate_ok"] and r["final"]["final_status"] == "succeeded" and r["final"]["delivery_complete"]
    d = Path(r["final"]["report_dir"])
    assert d == out_dir / "task1" / r["final"]["extraction_id"] / "manual__test"
    assert {p.name for p in d.iterdir()} >= {"summary.json", "report.md", "manifest.json", "quality_issues_by_code.csv"}
    assert not (d / "distinct_products.csv").exists()  # 样本不导出产品汇总
    s = read_summary(r["final"])
    assert (s["dag_id"], s["run_key"], s["extraction_id"], s["scope"]) == (
        "amazon_reviews_task1", "manual__test", r["final"]["extraction_id"], "sample")
    assert s["counts"]["records_read"] == 10 and s["counts"]["records_valid"] == 6
    assert s["baseline"]["status"] == "not_applicable"
    assert all(i["ok"] for i in s["invariants"]) and s["tasks"] == {n: "success" for n in pipeline.STEP_NAMES}
    md = (d / "report.md").read_text(encoding="utf-8")
    assert r["final"]["extraction_id"] in md and "样本运行" in md and "AI 辅助说明" in md
    assert "quality_issues_by_code" in md or "质量问题分布" in md
    assert s["source"]["sha256_is_official"] is False and s["program"]["source_tree_sha256"]


def test_step_payloads_are_small_and_hold_no_review_text(conn, fixture_files):
    r = run_all(params(fixture_files))
    for name, payload in r["steps"].items():
        text = json.dumps(payload, default=str)
        assert len(text) < 20_000, name
        assert "Plain review text" not in text and "bad bytes" not in text, name


def test_full_scope_exports_distinct_products_and_requires_baseline_review(conn, fixture_files):
    p = params(fixture_files, scope="full", sample_max_records=None)
    r = run_all(p)
    # 夹具只有 10 条，与官方基准不符：未解释前不能声称通过，门禁失败
    assert r["final"]["final_status"] == "review_required" and not r["final"]["delivery_complete"]
    assert not r["gate_ok"] and "review_required" in r["gate_reason"]
    d = Path(r["final"]["report_dir"])
    lines = (d / "distinct_products.csv").read_text().splitlines()
    assert lines[0] == "product_id,records_total,records_valid,records_quarantined,first_record_no,last_record_no,id_format_issue_records"
    assert [l.split(",")[0] for l in lines[1:]] == ["AAAAAAAAAA", "BBBBBBBBBB", "CCCCCCCCCC", "DDDDDDDDDD", "EEEEEEEEEE", "FFFFFFFFFF"]
    a = dict(zip(lines[0].split(","), lines[1].split(",")))
    assert (a["records_total"], a["records_valid"], a["records_quarantined"], a["first_record_no"], a["last_record_no"]) == ("3", "3", "0", "1", "3")
    b = dict(zip(lines[0].split(","), lines[2].split(",")))
    assert (b["records_total"], b["records_valid"], b["records_quarantined"]) == ("2", "1", "1")  # 隔离记录的 ID 也计入 observed
    s = read_summary(r["final"])
    assert s["baseline"]["status"] == "review_required"
    assert {x["metric"]: x["difference"] for x in s["baseline"]["rows"]}["records_read"] == 10 - 7_911_684


def test_explained_baseline_difference_allows_success_without_changing_counts(conn, fixture_files):
    r = run_all(params(fixture_files, scope="full", sample_max_records=None, baseline_explanation="人工夹具仅 10 条"))
    s = read_summary(r["final"])
    assert r["gate_ok"] and s["baseline"]["status"] == "explained"
    assert s["counts"]["records_read"] == 10 and "人工夹具" in (Path(r["final"]["report_dir"]) / "report.md").read_text()


def test_sample_stopped_early_does_not_claim_whole_file(conn, fixture_files):
    r = run_all(params(fixture_files, sample_max_records=4))
    assert r["gate_ok"]
    ex = read_summary(r["final"])["extraction"]
    assert (ex["scope_complete"], ex["source_eof_reached"], ex["gzip_verified"]) == (True, False, False)
    md = (Path(r["final"]["report_dir"]) / "report.md").read_text()
    assert "source_eof_reached=False" in md and "gzip_verified=False" in md


def test_rerun_produces_a_new_run_directory_and_same_counts(conn, fixture_files):
    a = run_all(params(fixture_files, run_key="run_a"))
    b = run_all(params(fixture_files, run_key="run_b"))
    assert a["final"]["report_dir"] != b["final"]["report_dir"]
    assert read_summary(a["final"])["counts"] == read_summary(b["final"])["counts"]
    assert b["steps"]["extract_load"]["outcome"] == "reused"


def test_report_files_are_written_atomically(tmp_path):
    target = tmp_path / "x" / "f.txt"
    reporting.write_atomic(target, b"one")
    reporting.write_atomic(target, b"two")
    assert target.read_bytes() == b"two" and not list(target.parent.glob(".*.tmp"))
