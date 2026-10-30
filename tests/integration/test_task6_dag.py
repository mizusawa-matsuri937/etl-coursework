"""任务 6 DAG（spec AC15）：amazon_movies_task6_release 的定义，以及按 DAG 顺序执行步骤验证成功、重跑、失败分支与门禁。

命令的工作目录为项目根目录：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_dag.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import _task6_helpers as h
from _task6_helpers import World
from etl_task1.config import ConfigError
from etl_task1.db import connect
from etl_task6 import pipeline
from etl_task6.pipeline import ReleaseParams, StepError

pytestmark = pytest.mark.integration

DAGS = Path("/opt/etl/dags")
ORDER = ["check_inputs", "candidates", "cooccur", "product_dates", "review_times", "select", "bounds", "verify",
         "finalize_report", "run_gate"]


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_movies_task6_release"]


def _rule(d, task_id):
    rule = d.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_dag_tasks_in_dependency_order_and_trigger_rules(dag):
    assert set(dag.task_dict) == set(ORDER)
    for a, b in zip(ORDER, ORDER[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "run_gate") == "all_done"
    assert all(_rule(dag, t) == "all_success" for t in ORDER[1:8])


def test_dag_manual_trigger_retries_and_params(dag):
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    assert set(dag.params) == {"extraction_id", "t4_mapping_version_id", "t5_person_mapping_version_id",
                               "imdb_snapshot_id", "rule_version", "batch_size", "limit", "fault_injection"}
    assert dag.params["limit"] is None and dag.params["batch_size"] == 500 and dag.params["rule_version"] == "t6r1"


def test_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_movies_task6_release.py").read_text().split("with DAG(")[0]
    assert "from etl_task6" not in top.replace("    from etl_task6", "")


def test_ac22_dag_imports_airflow_fail_exception_without_deprecation_warning():
    """AC22（ISSUE-12）：解析/执行 dags/amazon_movies_task6_release.py 时不触发 AirflowFailException 弃用警告。"""
    pytest.importorskip("airflow")
    import runpy
    import warnings

    dag_file = DAGS / "amazon_movies_task6_release.py"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runpy.run_path(str(dag_file))
    dep_msgs = [
        str(w.message)
        for w in caught
        if issubclass(w.category, (DeprecationWarning, UserWarning))
    ]
    assert not any("AirflowFailException" in m for m in dep_msgs), dep_msgs


def test_dag_default_rule_version_is_registered(dag):
    from etl_task6 import CURRENT_RULE_VERSION
    from etl_task6.rules import RULESETS

    assert dag.params["rule_version"] == CURRENT_RULE_VERSION and CURRENT_RULE_VERSION in RULESETS


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(out))
    return out


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return h.make_world(conn, make_settings, tmp_path)


def P(w: World, key: str, **over) -> ReleaseParams:
    base = dict(run_key=key, extraction_id=w.extraction_id, t4_mapping_version_id=w.mapping,
                t5_person_mapping_version_id=w.pmap_with_hints, imdb_snapshot_id=w.sid)
    base.update(over)
    return ReleaseParams(**base)


def run_all(p: ReleaseParams) -> tuple[dict, dict, tuple[bool, str]]:
    """顺序执行，等价于 DAG：上游失败时下游不执行，收尾与门禁照常运行。"""
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

    attempt("check_inputs", lambda: pipeline.check_inputs_step(p))
    chk = out["check_inputs"]
    attempt("candidates", None if chk is None else lambda: pipeline.candidates_step(p, chk))
    attempt("cooccur", None if out["candidates"] is None else lambda: pipeline.cooccur_step(p, chk))
    attempt("product_dates", None if out["cooccur"] is None else lambda: pipeline.product_dates_step(p, chk))
    attempt("review_times", None if out["product_dates"] is None else lambda: pipeline.review_times_step(p, chk))
    attempt("select", None if out["review_times"] is None else lambda: pipeline.select_step(p, chk))
    attempt("bounds", None if out["select"] is None else lambda: pipeline.bounds_step(p, chk))
    bnd = out["bounds"]
    attempt("verify", None if bnd is None else lambda: pipeline.release_verify_step(p, bnd))

    rep = pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_step(p, out))
    out["finalize_report"] = rep

    from etl_task1.pipeline import gate_decision
    gate_ok, gate_reason = gate_decision(rep)
    out["run_gate"] = {"status": "passed" if gate_ok else "failed"}
    return out, failed, (gate_ok, gate_reason)


def test_pipeline_success_and_reuse(world):
    """正常流程全部成功，换 run_key 重跑为 reused，门禁均通过。"""
    p1 = P(world, "t6_dag_test_1")
    out1, failed1, (gate1, _) = run_all(p1)
    assert not failed1 and gate1
    assert out1["bounds"]["status"] == "done"
    rv = out1["bounds"]["release_version_id"]
    assert rv is not None

    p2 = P(world, "t6_dag_test_2")
    out2, failed2, (gate2, _) = run_all(p2)
    assert not failed2 and gate2
    assert out2["candidates"]["status"] == "reused"
    assert out2["bounds"]["release_version_id"] == rv


def test_pipeline_nonexistent_mapping_fails_deterministically(world, env):
    """不存在的任务 4 映射版本在 check_inputs 失败，下游跳过，收尾记录失败，门禁失败。"""
    p = P(world, "t6_dag_bad_map", t4_mapping_version_id="map_nonexistent")
    out, failed, (gate, _) = run_all(p)
    assert "check_inputs" in failed and not failed["check_inputs"].transient
    assert not gate
    assert out["candidates"] is None and out["bounds"] is None
    rep = out["finalize_report"]
    assert rep["final_status"] == "failed"
    assert (env / "task6" / "_failures" / "t6_dag_bad_map" / "check_inputs-try1.json").exists()
