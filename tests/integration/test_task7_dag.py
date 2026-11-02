"""任务 7 DAG（spec AC13）：amazon_movies_task7_conflict 的定义，以及按 DAG 顺序执行步骤验证成功、重跑复用、上游缺失与 verify 失败后重跑。

命令的工作目录为项目根目录：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_dag.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _task7_helpers as h
from _task7_worlds import small_world
from etl_task1.config import ConfigError
from etl_task1.db import connect
from etl_task1.pipeline import gate_decision
from etl_task7 import pipeline
from etl_task7.pipeline import ConflictParams, StepError

pytestmark = pytest.mark.integration

DAGS = Path("/opt/etl/dags")
ORDER = ["check_inputs", "edition_resolve", "link_assess", "movie_scalar_resolve", "movie_multivalue_resolve",
         "finalize_version", "verify", "finalize_report", "gate"]


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_movies_task7_conflict"]


def _rule(d, task_id):
    rule = d.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_ac13_dag_tasks_in_dependency_order_and_trigger_rules(dag):
    assert set(dag.task_dict) == set(ORDER)
    for a, b in zip(ORDER, ORDER[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "gate") == "all_done"
    assert all(_rule(dag, t) == "all_success" for t in ORDER[1:7])


def test_ac13_dag_manual_trigger_retries_and_params(dag):
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    assert set(dag.params) == {"extraction_id", "t4_mapping_version_id", "t5_person_mapping_version_id",
                               "t6_release_version_id", "imdb_snapshot_id", "rule_version", "batch_size", "fault_injection"}
    assert dag.params["batch_size"] == 1000 and dag.params["rule_version"] == "t7r3"


def test_ac13_dag_default_rule_version_is_registered(dag):
    from etl_task7 import CURRENT_RULE_VERSION
    from etl_task7.rules import RULESETS
    assert dag.params["rule_version"] == CURRENT_RULE_VERSION and CURRENT_RULE_VERSION in RULESETS


def test_ac13_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_movies_task7_conflict.py").read_text().split("with DAG(")[0]
    assert "from etl_task7" not in top.replace("    from etl_task7", "")


def test_ac13_dag_file_imports_without_deprecation_warning_and_uses_the_sdk_path():
    """解析 dags/amazon_movies_task7_conflict.py 时不触发弃用警告；AirflowFailException 取自 airflow.sdk.exceptions。"""
    pytest.importorskip("airflow")
    import runpy
    import warnings

    dag_file = DAGS / "amazon_movies_task7_conflict.py"
    assert "from airflow.sdk.exceptions import AirflowFailException" in dag_file.read_text()
    assert "airflow.exceptions" not in dag_file.read_text()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runpy.run_path(str(dag_file))
    dep = [str(w.message) for w in caught if issubclass(w.category, (DeprecationWarning, UserWarning, FutureWarning))
           and ("Airflow" in str(w.message) or "airflow" in str(w.filename))]
    assert not dep, dep


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(out))
    return out


@pytest.fixture
def seeded(conn):
    return h.seed(conn, small_world())


def P(s, key: str, **over) -> ConflictParams:
    base = dict(run_key=key, extraction_id=s.eid, batch_size=2)
    base.update(over)
    return ConflictParams(**base)


def run_all(p: ConflictParams) -> tuple[dict, dict, tuple[bool, str]]:
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
    prev = "check_inputs"
    for step in pipeline.STEP_STAGE:
        attempt(step, None if out[prev] is None else (lambda step=step: pipeline.stage_step(p, chk, step)))
        prev = step
    rep = pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_step(p, out))
    out["finalize_report"] = rep
    gate_ok, reason = gate_decision(rep)
    out["gate"] = {"status": "passed" if gate_ok else "failed"}
    return out, failed, (gate_ok, reason)


def test_ac13_pipeline_success_then_rerun_is_reused(conn, seeded, env):
    out1, failed1, (gate1, _) = run_all(P(seeded, "t7_dag_1"))
    assert not failed1 and gate1
    assert out1["verify"]["status"] == "done" and out1["verify"]["conflict_version_id"] == "cv0001"
    assert conn.execute("SELECT status FROM task7.run").fetchone()[0] == "succeeded"
    assert conn.execute("SELECT status FROM task7.conflict_version").fetchone()[0] == "verified"
    rd = Path(out1["finalize_report"]["report_dir"])
    assert rd == env / "task7" / seeded.eid / "cv0001" / "t7_dag_1" and (rd / "summary.json").exists()
    assert (rd / "examples.md").exists() and (rd / "run_summary.json").exists()
    out2, failed2, (gate2, _) = run_all(P(seeded, "t7_dag_2"))
    assert not failed2 and gate2
    assert out2["edition_resolve"]["status"] == "reused" and out2["verify"]["status"] == "reused"
    assert out2["verify"]["conflict_version_id"] == "cv0001"
    assert [r[0] for r in conn.execute("SELECT status FROM task7.run ORDER BY started_at")] == ["succeeded", "reused"]


def test_ac13_missing_upstream_version_fails_check_inputs_once(conn, seeded, env):
    out, failed, (gate, _) = run_all(P(seeded, "t7_dag_missing", t6_release_version_id="rv9999"))
    assert "check_inputs" in failed and not failed["check_inputs"].transient
    assert not gate and out["edition_resolve"] is None and out["verify"] is None
    assert out["finalize_report"]["final_status"] == "failed"
    f = env / "task7" / "_failures" / "t7_dag_missing"
    assert (f / "check_inputs-try1.json").exists() and not list(f.glob("check_inputs-try2.json"))
    assert conn.execute("SELECT count(*) FROM task7.run").fetchone()[0] == 0


def test_ac13_verify_failure_leaves_failed_run_and_unverified_version_then_rerun_rewrites(conn, seeded, env):
    out, failed, (gate, _) = run_all(P(seeded, "t7_dag_vf", fault_injection="verify_fail"))
    assert set(failed) == {"verify"} and not failed["verify"].transient     # 确定性错误，不重试
    assert not gate and out["finalize_version"] is not None and out["finalize_report"]["final_status"] == "failed"
    assert conn.execute("SELECT status FROM task7.run").fetchone()[0] == "failed"
    assert conn.execute("SELECT status FROM task7.conflict_version").fetchone()[0] == "unverified"
    out2, failed2, (gate2, _) = run_all(P(seeded, "t7_dag_vf2"))
    assert not failed2 and gate2
    assert out2["edition_resolve"]["status"] == "done"                       # 不是 reused
    assert out2["verify"]["conflict_version_id"] == "cv0001"                 # 在原版本上重写
    assert [r[0] for r in conn.execute("SELECT status FROM task7.run ORDER BY started_at")] == ["failed", "succeeded"]
    assert conn.execute("SELECT status FROM task7.conflict_version").fetchone()[0] == "verified"
    assert conn.execute("SELECT count(*) FROM task7.conflict_version").fetchone()[0] == 1


def test_ac13_fault_injection_requires_test_environment(conn, seeded, monkeypatch):
    monkeypatch.setenv("ETL_ENV", "production")
    with pytest.raises(ConfigError, match="故障注入仅允许"):
        pipeline.check_inputs_step(P(seeded, "t7_dag_prod", fault_injection="verify_fail"))


def test_ac13_before_commit_fault_is_a_deterministic_failure_and_resumes(conn, seeded, env):
    out, failed, (gate, _) = run_all(P(seeded, "t7_dag_bc", fault_injection="before_commit:link_assess:2"))
    assert set(failed) == {"link_assess"} and not gate
    out2, failed2, (gate2, _) = run_all(P(seeded, "t7_dag_bc2"))
    assert not failed2 and gate2 and out2["verify"]["status"] == "done"
