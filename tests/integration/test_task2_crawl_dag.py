"""任务 2 爬虫 DAG：定义检查，以及按 DAG 顺序执行步骤（离线假请求）验证写回、核对、报告与门禁（AC19–AC21）。"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import psycopg
import pytest

from etl_task1.config import ConfigError
from etl_task1.loader import load_extraction
from etl_task2 import pipeline as manifest_pipeline
from etl_task2.crawl import pipeline
from etl_task2.crawl.crawler import CrawlBusy, apply_crawl_schema, resolve_snapshot
from etl_task2.crawl.pipeline import CrawlParams, fixture_fetch
from etl_task2.manifest import build_manifest
from etl_task2.pipeline import StepError

DAGS = Path("/opt/etl/dags")
ORDER = ["check_crawl_input", "crawl_pages", "verify_crawl", "finalize_report", "run_gate"]


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_products_task2_crawl"]


def _rule(dag, task_id):
    rule = dag.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_five_tasks_in_dependency_order(dag):
    assert set(dag.task_dict) == set(ORDER)
    for a, b in zip(ORDER, ORDER[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "run_gate") == "all_done"
    assert all(_rule(dag, t) == "all_success" for t in ORDER[1:3])


def test_manual_trigger_limits_and_params(dag):
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    assert set(dag.params) == {"extraction_id", "limit", "deadline_minutes", "concurrency", "rate", "timeout",
                               "retry_statuses", "fetch_mode"}
    assert dag.params["fetch_mode"] == "live" and dag.params["limit"] == 1000


def test_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_products_task2_crawl.py").read_text().split("with DAG(")[0]
    assert "from etl_task2" not in top.replace("    from etl_task2", "")


@pytest.fixture(autouse=True)
def out_dir(monkeypatch, tmp_path):
    d = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(d))
    return d


@pytest.fixture
def eid(conn, make_settings, verify):
    apply_crawl_schema(conn)
    eid = load_extraction(make_settings(), verify(), conn).extraction_id
    build_manifest(conn, eid, allow_sample=True)
    return eid


def run_all(p: CrawlParams) -> tuple[dict, dict, tuple[bool, str]]:
    """顺序执行，等价于 DAG：上游失败时记 None，收尾与门禁照常运行。"""
    out: dict = {}
    failed: dict[str, StepError] = {}

    def attempt(name, fn):
        if fn is None:
            out[name] = None
            return
        try:
            out[name] = manifest_pipeline.run_step(name, p, fn)
        except StepError as exc:
            out[name] = None
            failed[name] = exc

    attempt("check_crawl_input", lambda: pipeline.check_crawl_input_step(p))
    checked = out["check_crawl_input"]
    attempt("crawl_pages", (lambda: pipeline.crawl_pages_step(p, checked)) if checked else None)
    crawled = out["crawl_pages"]
    attempt("verify_crawl", (lambda: pipeline.verify_crawl_step(p, checked["extraction_id"], crawled))
            if crawled else None)
    final = manifest_pipeline.run_step("finalize_report", p,
                                       lambda: pipeline.finalize_step(p, {n: out.get(n) for n in pipeline.STEP_NAMES}))
    return out, failed, manifest_pipeline.gate_decision(final)


def fixture_params(eid, run_key="t2c_ok", **kw) -> CrawlParams:
    return CrawlParams(run_key=run_key, extraction_id=eid, fetch_mode="fixture", concurrency=3, **kw)


def test_fixture_run_writes_back_verifies_and_passes_gate(conn, eid, out_dir):
    out, failed, (ok, reason) = run_all(fixture_params(eid))
    assert not failed and ok, (failed, reason)
    assert out["check_crawl_input"]["pending"] == 6 and out["crawl_pages"]["asin_done"] == 6
    assert out["crawl_pages"]["outcomes"] == {"success": 4, "access_restricted": 1, "not_found": 1}

    rows = dict(conn.execute("SELECT asin, status FROM task2.product_manifest WHERE extraction_id = %s",
                             (eid,)).fetchall())
    assert rows["CCCCCCCCCC"] == "access_restricted" and rows["DDDDDDDDDD"] == "not_found"
    chain = conn.execute("SELECT redirect_chain FROM task2.fetch_attempt WHERE asin = 'AAAAAAAAAA'").fetchone()[0]
    assert chain[0]["status"] == 301 and chain[0]["location"].endswith("?ref=fixture")

    summary = json.loads((out_dir / "task2" / eid / "crawl" / "t2c_ok" / "summary.json").read_text())
    assert summary["final_status"] == "succeeded" and summary["redirected"] == 2
    assert summary["page_template"] == {"checkpoint": 1, "dp_product_title": 4, "not_found": 1}
    assert summary["status_before"] == {"not_collected": 6}
    assert sum(summary["status_after"].values()) == summary["manifest_rows"] == 6
    report = (out_dir / "task2" / eid / "crawl" / "t2c_ok" / "report.md").read_text()
    assert "## 页面模板与跳转（本次）" in report and "| 合计 | 6 | 6 |" in report

    # 重跑只处理剩余行；带 retry_statuses 时补爬被拦截的行
    out, _, (ok, _) = run_all(fixture_params(eid, "t2c_rerun"))
    assert ok and out["crawl_pages"]["asin_done"] == 0
    out, _, (ok, _) = run_all(fixture_params(eid, "t2c_retry", retry_statuses=("access_restricted",)))
    assert ok and out["crawl_pages"]["asin_done"] == 1


def test_limit_bounds_one_run(conn, eid):
    out, _, (ok, _) = run_all(fixture_params(eid, limit=2))
    assert ok and out["crawl_pages"]["asin_done"] == 2
    assert conn.execute("SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %s "
                        "AND status = 'not_collected'", (eid,)).fetchone()[0] == 4


def test_fixture_mode_and_bad_params_are_rejected(eid, monkeypatch, out_dir):
    monkeypatch.setenv("ETL_ENV", "prod")
    _, failed, (ok, reason) = run_all(fixture_params(eid, "t2c_prod_fixture"))
    assert not ok and "check_crawl_input" in failed and not failed["check_crawl_input"].transient
    assert "ETL_ENV=test" in str(failed["check_crawl_input"])
    # 检查未通过：报告与失败记录都在 _failures，下游不运行
    summary = json.loads((out_dir / "task2" / "_failures" / "t2c_prod_fixture" / "summary.json").read_text())
    assert summary["tasks"] == {"check_crawl_input": "failed", "crawl_pages": "not_run", "verify_crawl": "not_run"}
    monkeypatch.setenv("ETL_ENV", "test")
    with pytest.raises(ConfigError):
        pipeline.settings_for(fixture_params(eid, retry_statuses=("success",)))


def test_tampered_snapshot_fails_verify_and_gate(conn, eid, out_dir, monkeypatch):
    real = pipeline.crawl_pages_step

    def crawl_then_tamper(p, checked):
        crawled = real(p, checked)
        path = conn.execute("SELECT snapshot_path FROM task2.product_manifest WHERE asin = 'BBBBBBBBBB'").fetchone()[0]
        resolve_snapshot(path).write_bytes(gzip.compress(b"tampered"))
        return crawled

    monkeypatch.setattr(pipeline, "crawl_pages_step", crawl_then_tamper)
    out, failed, (ok, reason) = run_all(fixture_params(eid, "t2c_tamper"))
    assert not ok and set(failed) == {"verify_crawl"} and not failed["verify_crawl"].transient
    assert "snapshot_hash_mismatch=1" in str(failed["verify_crawl"])
    summary = json.loads((out_dir / "task2" / eid / "crawl" / "t2c_tamper" / "summary.json").read_text())
    assert summary["tasks"]["verify_crawl"] == "failed" and summary["final_status"] == "failed"


def test_lock_contention_and_db_errors_are_transient():
    assert manifest_pipeline.is_transient(CrawlBusy("busy"))
    assert manifest_pipeline.is_transient(psycopg.OperationalError("down"))


def test_fixture_fetch_is_deterministic():
    a = asyncio.run(fixture_fetch("https://www.amazon.com/dp/AAAAAAAAAA", None, 1))
    assert a == asyncio.run(fixture_fetch("https://www.amazon.com/dp/AAAAAAAAAA", None, 1))
    assert a.history[0]["status"] == 301
