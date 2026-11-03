"""任务 8 DAG（spec AC11）：amazon_reviews_task8_dedup 的定义，以及按 DAG 顺序执行步骤验证成功、重跑复用、上游缺失与
verify 失败后重跑。真实 scheduler 的验证见 doc/task8/runbook.md 第 2 节。

命令的工作目录为项目根目录：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dag.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _task8_helpers as h
from etl_task1.config import ConfigError
from etl_task1.pipeline import gate_decision
from etl_task8 import pipeline
from etl_task8.pipeline import DedupParams, StepError

pytestmark = pytest.mark.integration

DAGS = Path("/opt/etl/dags")
ORDER = ["check_inputs", "review_clean", "review_family", "review_dedup", "review_attribution", "finalize_version",
         "verify", "finalize_report", "gate"]


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_reviews_task8_dedup"]


def _rule(d, task_id):
    rule = d.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_ac11_dag_nine_tasks_in_order_and_trigger_rules(dag):
    assert set(dag.task_dict) == set(ORDER)
    for a, b in zip(ORDER, ORDER[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "gate") == "all_done"
    assert all(_rule(dag, t) == "all_success" for t in ORDER[1:7])


def test_ac11_dag_manual_trigger_retries_and_params(dag):
    from etl_task8 import CURRENT_RULE_VERSION
    from etl_task8.rules import RULESETS
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    assert set(dag.params) == {"extraction_id", "t3_rule_version", "conflict_version_id", "rule_version", "batch_size",
                               "partitions", "fault_injection"}
    assert (dag.params["t3_rule_version"], dag.params["conflict_version_id"], dag.params["rule_version"]) == \
        ("t3r2", "cv0003", CURRENT_RULE_VERSION) and CURRENT_RULE_VERSION in RULESETS
    assert dag.params["batch_size"] == 500000 and dag.params["partitions"] == 32


def test_ac11_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_reviews_task8_dedup.py").read_text().split("with DAG(")[0]
    assert "from etl_task8" not in top.replace("    from etl_task8", "")
    assert "from airflow.sdk.exceptions import AirflowFailException" in top


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(out))
    return out


@pytest.fixture
def eid(conn, make_settings, tmp_path):
    return h.seed_small_world(conn, make_settings, tmp_path)


def P(eid: str, key: str, **over) -> DedupParams:
    base = dict(run_key=key, extraction_id=eid, batch_size=7, partitions=2)
    base.update(over)
    return DedupParams(**base)


def run_all(p: DedupParams) -> tuple[dict, dict, tuple[bool, str]]:
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


def test_ac11_pipeline_success_then_rerun_is_reused(conn, eid, env):
    out1, failed1, (gate1, _) = run_all(P(eid, "t8_dag_1"))
    assert not failed1 and gate1
    assert out1["verify"]["status"] == "done" and out1["verify"]["review_dedup_version_id"] == "rd0001"
    assert conn.execute("SELECT status FROM task8.run").fetchone()[0] == "succeeded"
    assert conn.execute("SELECT status FROM task8.dedup_version").fetchone()[0] == "verified"
    rd = Path(out1["finalize_report"]["report_dir"])
    assert rd == env / "task8" / eid / "rd0001" / "t8_dag_1"
    assert {"summary.json", "examples.md", "run_summary.json", "checksums.json"} <= {x.name for x in rd.iterdir()}
    out2, failed2, (gate2, _) = run_all(P(eid, "t8_dag_2"))
    assert not failed2 and gate2
    assert out2["review_clean"]["status"] == "reused" and out2["verify"]["status"] == "reused"
    assert out2["verify"]["review_dedup_version_id"] == "rd0001"
    assert [r[0] for r in conn.execute("SELECT status FROM task8.run ORDER BY started_at")] == ["succeeded", "reused"]


def test_ac11_missing_upstream_cv_fails_check_inputs_once(conn, eid, env):
    out, failed, (gate, _) = run_all(P(eid, "t8_dag_missing", conflict_version_id="cv9999"))
    assert "check_inputs" in failed and not failed["check_inputs"].transient
    assert not gate and out["review_clean"] is None and out["verify"] is None
    assert out["finalize_report"]["final_status"] == "failed"
    f = env / "task8" / "_failures" / "t8_dag_missing"
    assert (f / "check_inputs-try1.json").exists() and not list(f.glob("check_inputs-try2.json"))
    assert conn.execute("SELECT count(*) FROM task8.run").fetchone()[0] == 0


def test_ac11_verify_failure_leaves_failed_run_and_unverified_version_then_rerun_rewrites(conn, eid, env):
    out, failed, (gate, _) = run_all(P(eid, "t8_dag_vf", fault_injection="verify_fail"))
    assert set(failed) == {"verify"} and not failed["verify"].transient     # 确定性错误，不重试
    assert not gate and out["finalize_version"] is not None and out["finalize_report"]["final_status"] == "failed"
    assert conn.execute("SELECT status FROM task8.run").fetchone()[0] == "failed"
    assert conn.execute("SELECT status FROM task8.dedup_version").fetchone()[0] == "unverified"
    out2, failed2, (gate2, _) = run_all(P(eid, "t8_dag_vf2"))
    assert not failed2 and gate2
    assert out2["review_clean"]["status"] == "done"                         # 不是 reused
    assert out2["review_clean"]["counts"].get("skipped") == "complete"      # 阶段结果按阶段键保留
    assert out2["verify"]["review_dedup_version_id"] == "rd0001"           # 在原版本上重写
    assert [r[0] for r in conn.execute("SELECT status FROM task8.run ORDER BY started_at")] == ["failed", "succeeded"]
    assert conn.execute("SELECT status FROM task8.dedup_version").fetchone()[0] == "verified"
    assert conn.execute("SELECT count(*) FROM task8.dedup_version").fetchone()[0] == 1


def test_ac11_fault_injection_requires_test_environment(conn, eid, monkeypatch):
    monkeypatch.setenv("ETL_ENV", "production")
    with pytest.raises(ConfigError, match="故障注入仅允许"):
        pipeline.check_inputs_step(P(eid, "t8_dag_prod", fault_injection="verify_fail"))


def test_ac11_before_commit_fault_is_deterministic_and_resumes(conn, eid, env):
    out, failed, (gate, _) = run_all(P(eid, "t8_dag_bc", fault_injection="before_commit:review_dedup:2"))
    assert set(failed) == {"review_dedup"} and not failed["review_dedup"].transient and not gate
    out2, failed2, (gate2, _) = run_all(P(eid, "t8_dag_bc2"))
    assert not failed2 and gate2 and out2["verify"]["status"] == "done"
    assert out2["review_dedup"]["counts"]["units"] == 3                      # 只补做未提交的单元
