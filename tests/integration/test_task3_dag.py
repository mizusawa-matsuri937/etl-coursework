"""任务 3 DAG（spec AC09）：定义检查，以及按 DAG 顺序执行步骤验证成功、重跑、失败分支与门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from _task3_helpers import setup, standard_products
from etl_task1.config import ConfigError
from etl_task3 import pipeline
from etl_task3.classify import ClassifyBusy
from etl_task3.pipeline import ClassifyParams, StepError

pytestmark = pytest.mark.integration

DAGS = Path("/opt/etl/dags")
ORDER = ["check_input", "classify_products", "verify_classification", "finalize_report", "run_gate"]


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_products_task3_classify"]


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
    assert set(dag.params) == {"extraction_id", "limit", "batch_size", "fault_injection"}
    assert dag.params["limit"] is None and dag.params["batch_size"] == 500


def test_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_products_task3_classify.py").read_text().split("with DAG(")[0]
    assert "from etl_task3" not in top.replace("    from etl_task3", "")


@pytest.fixture(autouse=True)
def out_dir(monkeypatch, tmp_path):
    d = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(d))
    return d


@pytest.fixture
def fx(conn, make_settings, tmp_path):
    return setup(conn, make_settings, tmp_path, standard_products())


def run_all(p: ClassifyParams) -> tuple[dict, dict, tuple[bool, str]]:
    """顺序执行，等价于 DAG：上游失败时记 None，收尾与门禁照常运行。"""
    out: dict = {}
    failed: dict[str, StepError] = {}

    def attempt(name, fn):
        if fn is None:
            out[name] = None
            return
        try:
            out[name] = pipeline.run_step(name, p, fn)
        except StepError as exc:
            out[name] = None
            failed[name] = exc

    attempt("check_input", lambda: pipeline.check_input_step(p))
    checked = out["check_input"]
    attempt("classify_products", (lambda: pipeline.classify_step(p, checked)) if checked else None)
    attempt("verify_classification", (lambda: pipeline.verify_step(p, checked["extraction_id"]))
            if out["classify_products"] else None)
    final = pipeline.run_step("finalize_report", p,
                              lambda: pipeline.finalize_step(p, {n: out.get(n) for n in pipeline.STEP_NAMES}))
    return out, failed, pipeline.gate_decision(final)


def test_success_then_rerun_reuses(conn, fx):
    out, failed, (ok, reason) = run_all(ClassifyParams(run_key="t3_ok", extraction_id=fx.eid))
    assert not failed and ok, (failed, reason)
    assert out["classify_products"]["status"] == "succeeded" and out["classify_products"]["dp_classified"] == 3
    assert out["verify_classification"]["classification_rows"] == 10
    out, failed, (ok, _) = run_all(ClassifyParams(run_key="t3_rerun", extraction_id=fx.eid))
    assert ok and out["classify_products"]["status"] == "reused" and out["classify_products"]["dp_classified"] == 0


def test_unknown_extraction_fails_check_without_retry(out_dir):
    _, failed, (ok, reason) = run_all(ClassifyParams(run_key="t3_missing", extraction_id="0" * 64))
    assert not ok and set(failed) == {"check_input"} and not failed["check_input"].transient
    summary = json.loads((out_dir / "task3" / "_failures" / "t3_missing" / "summary.json").read_text())
    assert summary["tasks"] == {"check_input": "failed", "classify_products": "not_run",
                                "verify_classification": "not_run"}
    assert summary["final_status"] == "failed"


def test_limited_run_fails_verify_and_gate(conn, fx, out_dir):
    out, failed, (ok, _) = run_all(ClassifyParams(run_key="t3_limit", extraction_id=fx.eid, limit=1))
    assert not ok and set(failed) == {"verify_classification"}
    assert out["classify_products"]["remaining_dp"] == 2


def test_fault_injection_before_commit_and_transient(conn, fx, tmp_path):
    p = ClassifyParams(run_key="t3_fault", extraction_id=fx.eid, batch_size=1, fault_injection="before_commit:2")
    _, failed, (ok, _) = run_all(p)
    assert not ok and set(failed) == {"classify_products"} and not failed["classify_products"].transient
    marker = tmp_path / "marker"
    p = ClassifyParams(run_key="t3_transient", extraction_id=fx.eid, fault_injection=f"transient_once:{marker}")
    with pytest.raises(StepError) as e:
        pipeline.run_step("classify_products", p, lambda: pipeline.classify_step(p, {"extraction_id": fx.eid}))
    assert e.value.transient
    assert pipeline.classify_step(p, {"extraction_id": fx.eid})["status"] == "succeeded"  # 第 2 次成功


def test_fault_injection_rejected_outside_test(monkeypatch, fx):
    monkeypatch.setenv("ETL_ENV", "prod")
    with pytest.raises(ConfigError):
        pipeline.settings_for(ClassifyParams(run_key="x", fault_injection="before_commit:1"))


def test_lock_contention_and_db_errors_are_transient():
    assert pipeline.is_transient(ClassifyBusy("busy"))
    assert pipeline.is_transient(psycopg.OperationalError("down"))
