"""任务 9 DAG（spec AC16）：amazon_movies_task9_lineage 的定义，以及按 DAG 顺序执行步骤验证成功、重跑复用、上游缺失、
verify 失败后重跑与批次故障。真实 scheduler 的验证见 doc/task9/runbook.md 第 2 节。

命令的工作目录为项目根目录：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_dag.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _task9_helpers as h
from etl_task1.config import ConfigError
from etl_task1.pipeline import gate_decision
from etl_task9 import pipeline
from etl_task9.pipeline import LineageParams, StepError

pytestmark = pytest.mark.integration

DAGS = Path("/opt/etl/dags")
ORDER = ["check_inputs", "build_sources", "build_assertions", "build_derivations", "build_selections", "build_impact",
         "finalize_version", "verify", "finalize_report", "gate"]


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_movies_task9_lineage"]


def _rule(d, task_id):
    rule = d.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_ac16_dag_ten_tasks_in_order_and_trigger_rules(dag):
    assert set(dag.task_dict) == set(ORDER) and len(ORDER) == 10
    for a, b in zip(ORDER, ORDER[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "gate") == "all_done"
    assert all(_rule(dag, t) == "all_success" for t in ORDER[1:8])


def test_ac16_dag_manual_trigger_retries_and_params(dag):
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    p = dag.params
    assert (p["t4_mapping_version_id"], p["t5_person_mapping_version_id"], p["t6_release_version_id"],
            p["conflict_version_id"], p["review_dedup_version_id"], p["imdb_snapshot_id"], p["rule_version"]) == \
        ("map0002", "pmap0001", "rv0001", "cv0003", "rd0001", "imdb_2026-10-29", "t9r1")


def test_ac16_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_movies_task9_lineage.py").read_text().split("with DAG(")[0]
    assert "from etl_task9" not in top.replace("    from etl_task9", "")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(out))
    return out


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    return h.seed(conn, make_settings, tmp_path)


def P(t9, key: str, **over) -> LineageParams:
    base = dict(run_key=key, extraction_id=t9.eid, t4_mapping_version_id=h.h7.T4,
                t5_person_mapping_version_id=h.h7.PMAP, t6_release_version_id=h.h7.RV, conflict_version_id=t9.cv,
                review_dedup_version_id="rd0001", imdb_snapshot_id=h.SNAPSHOT, batch_size=50,
                snapshot_root=str(t9.snapshot_root))
    base.update(over)
    return LineageParams(**base)


def run_all(p: LineageParams) -> tuple[dict, dict, tuple[bool, str]]:
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
    ok, reason = gate_decision(rep)
    return out, failed, (ok, reason)


def test_ac16_pipeline_success_then_rerun_is_reused(conn, t9, env):
    out1, failed1, (gate1, _) = run_all(P(t9, "t9_dag_1"))
    assert not failed1 and gate1, failed1
    assert out1["verify"]["status"] == "done" and out1["verify"]["lineage_version_id"] == "lv0001"
    assert conn.execute("SELECT status FROM task9.run WHERE kind = 'lineage'").fetchone()[0] == "succeeded"
    rd = Path(out1["finalize_report"]["report_dir"])
    assert rd == env / "task9" / t9.eid / "lv0001" / "t9_dag_1"
    assert {"summary.json", "examples.md", "report.md", "run_summary.json", "checksums.json"} <= {x.name for x in rd.iterdir()}
    out2, failed2, (gate2, _) = run_all(P(t9, "t9_dag_2"))
    assert not failed2 and gate2
    assert out2["build_sources"]["status"] == "reused" and out2["verify"]["status"] == "reused"
    assert [r[0] for r in conn.execute("SELECT status FROM task9.run WHERE kind = 'lineage' ORDER BY started_at")] == \
        ["succeeded", "reused"]


def test_ac16_missing_upstream_cv_fails_check_inputs_once(conn, t9, env):
    out, failed, (gate, _) = run_all(P(t9, "t9_dag_missing", conflict_version_id="cv9999"))
    assert "check_inputs" in failed and not failed["check_inputs"].transient
    assert not gate and out["build_sources"] is None and out["verify"] is None
    assert out["finalize_report"]["final_status"] == "failed"
    assert (env / "task9" / "_failures" / "t9_dag_missing" / "check_inputs-try1.json").exists()


def test_ac16_verify_failure_then_rerun_rewrites_same_version(conn, t9, env):
    out, failed, (gate, _) = run_all(P(t9, "t9_dag_vf", fault_injection="verify_fail"))
    assert set(failed) == {"verify"} and not failed["verify"].transient and not gate
    assert conn.execute("SELECT status FROM task9.lineage_version").fetchone()[0] == "unverified"
    out2, failed2, (gate2, _) = run_all(P(t9, "t9_dag_vf2"))
    assert not failed2 and gate2 and out2["build_sources"]["status"] == "done"
    assert out2["build_sources"]["counts"].get("skipped") is True               # 阶段结果按阶段键保留
    assert out2["verify"]["lineage_version_id"] == "lv0001"
    assert [r[0] for r in conn.execute("SELECT status FROM task9.run WHERE kind = 'lineage' ORDER BY started_at")] == \
        ["failed", "succeeded"]
    assert conn.execute("SELECT status FROM task9.lineage_version").fetchall() == [("verified",)]


def test_ac16_fault_injection_requires_test_environment(conn, t9, monkeypatch):
    monkeypatch.setenv("ETL_ENV", "production")
    with pytest.raises(ConfigError, match="故障注入仅允许"):
        pipeline.check_inputs_step(P(t9, "t9_dag_prod", fault_injection="verify_fail"))


def test_ac16_before_commit_fault_is_deterministic_and_resumes(conn, t9, env):
    out, failed, (gate, _) = run_all(P(t9, "t9_dag_bc", batch_size=5, fault_injection="before_commit:assertions:2"))
    assert set(failed) == {"build_assertions"} and not failed["build_assertions"].transient and not gate
    out2, failed2, (gate2, _) = run_all(P(t9, "t9_dag_bc2", batch_size=5))
    assert not failed2 and gate2 and out2["verify"]["status"] == "done"
