"""任务 5 DAG（spec AC11）：amazon_movies_task5_person 的定义，以及按 DAG 顺序执行步骤验证成功、重跑、失败分支、重试与门禁。

真实 scheduler 的三次运行（经 compose.e2e.yaml）见 doc/task5/runbook.md；这里在测试库里按同样的步骤顺序直接调用步骤函数。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from task5 import resolve as fx

from _task5_helpers import World, clone_mapping, make_world
from etl_task1.config import ConfigError
from etl_task1.db import connect
from etl_task5 import pipeline
from etl_task5.pipeline import PersonParams, StepError
from etl_task5.run import RunBusy, extraction_lock

pytestmark = pytest.mark.integration

DAGS = Path("/opt/etl/dags")
ORDER = ["check_inputs", "mentions", "link_external", "resolve", "movie_person", "verify", "finalize_report", "run_gate"]
R = "B0T5R00"


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_movies_task5_person"]


def _rule(d, task_id):
    rule = d.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_dag_tasks_in_dependency_order_and_trigger_rules(dag):
    assert set(dag.task_dict) == set(ORDER)
    for a, b in zip(ORDER, ORDER[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "run_gate") == "all_done"
    assert all(_rule(dag, t) == "all_success" for t in ORDER[1:6])


def test_dag_manual_trigger_retries_and_params(dag):
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    assert set(dag.params) == {"extraction_id", "t4_mapping_version_id", "imdb_snapshot_id", "norm_version", "rule_version",
                               "batch_size", "limit", "fault_injection"}
    assert dag.params["limit"] is None and dag.params["batch_size"] == 500 and dag.params["rule_version"] == "t5r1"


def test_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_movies_task5_person.py").read_text().split("with DAG(")[0]
    assert "from etl_task5" not in top.replace("    from etl_task5", "")


def test_dag_default_rule_version_is_the_registered_current_one(dag):
    from etl_task5 import CURRENT_RULE_VERSION
    from etl_task5.rules import RULESETS

    assert dag.params["rule_version"] == CURRENT_RULE_VERSION and CURRENT_RULE_VERSION in RULESETS


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(out))
    return out


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return make_world(conn, make_settings, tmp_path, fx.case_products(), fx.CASES_IMDB)


def P(w: World, key: str, **over) -> PersonParams:
    return PersonParams(run_key=key, extraction_id=w.fx.eid, t4_mapping_version_id=w.mapping, imdb_snapshot_id=w.sid, **over)


def run_all(p: PersonParams) -> tuple[dict, dict, tuple[bool, str]]:
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

    attempt("check_inputs", lambda: pipeline.check_inputs_step(p))
    c = out["check_inputs"]
    attempt("mentions", (lambda: pipeline.mentions_step(p, c)) if c else None)
    attempt("link_external", (lambda: pipeline.link_external_step(p, c)) if out["mentions"] else None)
    attempt("resolve", (lambda: pipeline.resolve_step(p, c)) if out["link_external"] else None)
    attempt("movie_person", (lambda: pipeline.movie_person_step(p, c)) if out["resolve"] else None)
    attempt("verify", (lambda: pipeline.person_verify_step(p, out["movie_person"])) if out["movie_person"] else None)
    final = pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_step(
        p, {n: out.get(n) for n in pipeline.PERSON_STEP_NAMES}))
    return out, failed, pipeline.gate_decision(final)


def test_success_then_rerun_is_reused(conn, world, env):
    out, failed, (ok, reason) = run_all(P(world, "p_ok"))
    assert not failed and ok, (failed, reason)
    assert {out[n]["status"] for n in ("mentions", "link_external", "resolve", "movie_person")} == {"done"}
    pmap = out["movie_person"]["person_mapping_version_id"]
    assert out["verify"]["persons"] == out["resolve"]["counts"]["persons"] and out["verify"]["matched_products"] == 16
    d = env / "task5" / world.fx.eid / pmap / "p_ok"
    summary = json.loads((d / "summary.json").read_text())
    assert summary["final_status"] == "succeeded" and set(summary["tasks"].values()) == {"success"}
    assert summary["stats"]["person_mapping_version_id"] == pmap
    for name in ("report.md", "examples.md", "persons.csv", "aliases.csv", "movie_person.csv", "decisions_sample.csv",
                 "t4_upgrade_hints.csv"):
        assert (d / name).exists(), name
    rows_before = {t: conn.execute(f"SELECT count(*) FROM task5.{t}").fetchone()[0]
                   for t in ("person_mention", "person", "mention_person", "person_decision", "movie_person",
                             "person_mapping_version", "link_decision")}
    out, failed, (ok, _) = run_all(P(world, "p_rerun"))
    assert ok and not failed
    assert {out[n]["status"] for n in ("mentions", "link_external", "resolve", "movie_person")} == {"reused"}
    assert out["movie_person"]["person_mapping_version_id"] == pmap
    assert {t: conn.execute(f"SELECT count(*) FROM task5.{t}").fetchone()[0] for t in rows_before} == rows_before
    assert conn.execute("SELECT status FROM task5.run WHERE airflow_run_id = 'p_rerun'").fetchone() == ("reused",)
    assert json.loads((env / "task5" / world.fx.eid / pmap / "p_rerun" / "summary.json").read_text())["final_status"] \
        == "succeeded"


def test_missing_mapping_version_fails_check_without_retry_and_downstream_not_run(conn, world, env):
    out, failed, (ok, reason) = run_all(PersonParams(
        run_key="p_missing", extraction_id=world.fx.eid, t4_mapping_version_id="map9999", imdb_snapshot_id=world.sid))
    assert not ok and set(failed) == {"check_inputs"} and not failed["check_inputs"].transient
    summary = json.loads((env / "task5" / "_failures" / "p_missing" / "summary.json").read_text())
    assert summary["tasks"] == {"check_inputs": "failed", "mentions": "not_run", "link_external": "not_run",
                                "resolve": "not_run", "movie_person": "not_run", "verify": "not_run"}
    assert summary["final_status"] == "failed" and summary["failures"][0]["task"] == "check_inputs"
    assert "map9999" in summary["failures"][0]["message"]
    assert (env / "task5" / "_failures" / "p_missing" / "check_inputs-try1.json").exists()
    assert conn.execute("SELECT count(*) FROM task5.run").fetchone()[0] == 0


def test_mapping_version_of_another_extraction_fails_check_and_unknown_rule_is_deterministic(conn, world):
    _, failed, (ok, _) = run_all(PersonParams(run_key="p_eid", extraction_id="0" * 64,
                                              t4_mapping_version_id=world.mapping, imdb_snapshot_id=world.sid))
    assert not ok and set(failed) == {"check_inputs"} and not failed["check_inputs"].transient
    _, failed, (ok, _) = run_all(P(world, "p_rule", rule_version="t5r99"))
    assert not ok and set(failed) == {"check_inputs"} and not failed["check_inputs"].transient
    _, failed, (ok, _) = run_all(PersonParams(run_key="p_snap", extraction_id=world.fx.eid,
                                              t4_mapping_version_id=world.mapping, imdb_snapshot_id="imdb_2099-01-01"))
    assert not ok and set(failed) == {"check_inputs"}


def test_defaults_use_map0002_when_it_belongs_to_the_extraction_else_the_latest_mapping_and_its_snapshot(conn, world):
    assert pipeline.default_t4_mapping(conn, world.fx.eid) == world.mapping              # 只有一个映射版本（map0001）
    clone_mapping(conn, world, f"{R}A02", "map0002")
    assert pipeline.default_t4_mapping(conn, world.fx.eid) == "map0002"
    conn.execute("UPDATE task4.mapping_version SET created_at = created_at + interval '1 day' WHERE mapping_version_id = %s",
                 (world.mapping,))
    conn.execute("DELETE FROM task4.product_match_status WHERE mapping_version_id = 'map0002'")
    conn.execute("DELETE FROM task4.product_movie WHERE mapping_version_id = 'map0002'")
    conn.execute("DELETE FROM task4.movie WHERE mapping_version_id = 'map0002'")
    conn.execute("DELETE FROM task4.mapping_version WHERE mapping_version_id = 'map0002'")
    assert pipeline.default_t4_mapping(conn, world.fx.eid) == world.mapping              # map0002 不存在时取最新
    checked = pipeline.check_inputs_step(PersonParams(run_key="p_def", extraction_id=world.fx.eid))
    assert checked["t4_mapping_version_id"] == world.mapping and checked["imdb_snapshot_id"] == world.sid


def test_limited_run_stops_before_link_external_and_makes_no_person_mapping(conn, world):
    out, failed, (ok, _) = run_all(P(world, "p_limit", limit=3))
    assert not ok and out["mentions"]["counts"]["remaining"] > 0
    assert set(failed) == {"link_external"} and not failed["link_external"].transient
    assert conn.execute("SELECT count(*) FROM task5.person_mapping_version").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM task5.run").fetchall() == [("failed",)]


def test_fault_injection_rejected_outside_test(monkeypatch):
    monkeypatch.setenv("ETL_ENV", "prod")
    with pytest.raises(ConfigError):
        pipeline.settings_for(PersonParams(run_key="x", fault_injection="before_commit:mentions:1"))


def test_transient_once_is_retryable_and_the_second_try_succeeds(conn, world, tmp_path):
    marker = tmp_path / "marker"
    p = P(world, "p_transient", fault_injection=f"transient_once:{marker}")
    checked = pipeline.check_inputs_step(p)
    with pytest.raises(StepError) as e:
        pipeline.run_step("mentions", p, lambda: pipeline.mentions_step(p, checked))
    assert e.value.transient
    assert conn.execute("SELECT count(*) FROM task5.run").fetchone()[0] == 0
    assert pipeline.mentions_step(p, checked)["status"] == "done"            # 第 2 次成功


def test_lock_contention_is_a_deterministic_failure(conn, world, base_settings):
    p = P(world, "p_busy")
    checked = pipeline.check_inputs_step(p)
    with connect(base_settings) as other, extraction_lock(other, world.fx.eid):
        with pytest.raises(StepError) as e:
            pipeline.run_step("mentions", p, lambda: pipeline.mentions_step(p, checked))
        assert isinstance(e.value.cause, RunBusy) and not e.value.transient
        assert conn.execute("SELECT count(*) FROM task5.run").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task5.person_mention").fetchone()[0] == 0
    assert not pipeline.is_transient(RunBusy("busy"))
    assert pipeline.mentions_step(p, checked)["status"] == "done"            # 锁释放后可以运行


@pytest.mark.parametrize("stage,broken", [("mentions", "mentions"), ("credits", "link_external"),
                                          ("decide_pair", "resolve"), ("cluster", "resolve"),
                                          ("movie_person", "movie_person")])
def test_resume_after_an_injected_fault_reuses_the_same_run_and_finishes(conn, world, stage, broken):
    """故障注入后同一 DAG run（同一 run_key）重试：接着用同一条运行，已提交的批次不重做，最终成功。"""
    batch = 2 if stage in ("mentions", "credits", "decide_pair") else 500
    out, failed, (ok, _) = run_all(P(world, "p_resume", batch_size=batch, fault_injection=f"before_commit:{stage}:"
                                     f"{2 if batch == 2 else 1}"))
    assert not ok and set(failed) == {broken} and not failed[broken].transient
    assert conn.execute("SELECT status FROM task5.run").fetchone()[0] == "failed"
    out, failed, (ok, reason) = run_all(P(world, "p_resume", batch_size=batch, try_number=2))
    assert ok and not failed, (failed, reason)
    assert conn.execute("SELECT count(*), min(status) FROM task5.run").fetchone() == (1, "succeeded")     # 接着用同一条运行
    assert conn.execute("SELECT count(*) FROM (SELECT 1 FROM task5.entity_stage GROUP BY extraction_id, stage, stage_key, "
                        "entity_id HAVING count(*) > 1) d").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task5.person_mapping_version").fetchone()[0] == 1
