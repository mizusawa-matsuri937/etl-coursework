"""任务 4 DAG（spec AC11）：imdb_reference_task4_load 的定义检查，以及按 DAG 顺序执行步骤验证成功、重跑、失败分支与门禁。

匹配 DAG amazon_movies_task4_match 的定义、按 DAG 顺序执行的成功、重跑、失败分支，以及分批恢复与抽取锁（AC10）在文件后半部分。
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest
from task4 import imdb_files as fx

from _task4_helpers import build, load_imdb, match_products, reset_task4

from etl_task1.config import ConfigError
from etl_task4 import pipeline
from etl_task4.imdb import ImdbBusy, apply_imdb_schema
from etl_task4.pipeline import ImdbParams, MatchParams, StepError
from etl_task4.run import RunBusy, extraction_lock

pytestmark = pytest.mark.integration

DAGS = Path("/opt/etl/dags")
IMDB_ORDER = ["check_files", "load_snapshot", "verify_snapshot", "run_gate"]


@pytest.fixture(scope="module")
def imdb_dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["imdb_reference_task4_load"]


def _rule(dag, task_id):
    rule = dag.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_imdb_dag_tasks_in_dependency_order(imdb_dag):
    assert set(imdb_dag.task_dict) == set(IMDB_ORDER)
    for a, b in zip(IMDB_ORDER, IMDB_ORDER[1:]):
        assert imdb_dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(imdb_dag, "run_gate") == "all_done"
    assert all(_rule(imdb_dag, t) == "all_success" for t in IMDB_ORDER[:3])


def test_imdb_dag_manual_trigger_retries_and_params(imdb_dag):
    assert imdb_dag.schedule is None and imdb_dag.catchup is False
    assert imdb_dag.max_active_runs == 1 and imdb_dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in imdb_dag.tasks)
    assert set(imdb_dag.params) == {"snapshot_id", "fault_injection"}


def test_imdb_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "imdb_reference_task4_load.py").read_text().split("with DAG(")[0]
    assert "from etl_task4" not in top.replace("    from etl_task4", "")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(out))
    monkeypatch.setenv("IMDB_DATA_DIR", str(tmp_path / "imdb"))
    return out


@pytest.fixture
def snap(conn, tmp_path):
    apply_imdb_schema(conn)
    conn.execute("TRUNCATE imdb.snapshot CASCADE")
    return fx.build_snapshot(tmp_path / "imdb" / "2026-10-30", **fx.BASIC)


def run_all(p: ImdbParams) -> tuple[dict, dict, tuple[bool, str]]:
    """顺序执行，等价于 DAG：上游失败时记 None，门禁照常运行。"""
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

    attempt("check_files", lambda: pipeline.check_files_step(p))
    checked = out["check_files"]
    attempt("load_snapshot", (lambda: pipeline.load_step(p, checked)) if checked else None)
    attempt("verify_snapshot", (lambda: pipeline.verify_step(p, checked["snapshot_id"]))
            if out["load_snapshot"] else None)
    final = pipeline.run_step("run_gate", p, lambda: pipeline.gate_step(p, {n: out.get(n) for n in
                                                                            pipeline.IMDB_STEP_NAMES}))
    return out, failed, pipeline.gate_decision(final)


def test_imdb_success_then_rerun_is_reused(snap, env):
    out, failed, (ok, reason) = run_all(ImdbParams(run_key="ok", snapshot_id=fx.SNAPSHOT_ID))
    assert not failed and ok, (failed, reason)
    assert out["load_snapshot"]["status"] == "loaded" and out["verify_snapshot"]["status"] == "loaded"
    summary = json.loads((env / "task4" / "imdb" / fx.SNAPSHOT_ID / "ok" / "summary.json").read_text())
    assert summary["final_status"] == "succeeded" and summary["tasks"]["load_snapshot"] == "success"
    out, failed, (ok, _) = run_all(ImdbParams(run_key="rerun", snapshot_id=fx.SNAPSHOT_ID))
    assert ok and out["load_snapshot"]["status"] == "reused"


def test_missing_snapshot_fails_check_without_retry(snap, env):
    out, failed, (ok, reason) = run_all(ImdbParams(run_key="missing", snapshot_id="imdb_2099-01-01"))
    assert not ok and set(failed) == {"check_files"} and not failed["check_files"].transient
    summary = json.loads((env / "task4" / "_failures" / "missing" / "summary.json").read_text())
    assert summary["tasks"] == {"check_files": "failed", "load_snapshot": "not_run", "verify_snapshot": "not_run"}
    assert summary["final_status"] == "failed" and summary["failures"][0]["task"] == "check_files"
    assert (env / "task4" / "_failures" / "missing" / "check_files-try1.json").exists()


def test_corrupted_file_fails_check_and_nothing_is_loaded(conn, snap):
    fx.flip_one_byte(snap, "title.akas.tsv.gz")
    _, failed, (ok, _) = run_all(ImdbParams(run_key="corrupt", snapshot_id=fx.SNAPSHOT_ID))
    assert not ok and set(failed) == {"check_files"} and not failed["check_files"].transient
    assert conn.execute("SELECT count(*) FROM imdb.snapshot").fetchone()[0] == 0


def test_missing_snapshot_id_is_a_config_error(snap):
    _, failed, (ok, _) = run_all(ImdbParams(run_key="noid"))
    assert not ok and isinstance(failed["check_files"].cause, ConfigError)


def test_fault_before_commit_is_deterministic_and_transient_once_is_retryable(conn, snap, tmp_path):
    p = ImdbParams(run_key="fault", snapshot_id=fx.SNAPSHOT_ID, fault_injection="before_commit")
    _, failed, (ok, _) = run_all(p)
    assert not ok and set(failed) == {"load_snapshot"} and not failed["load_snapshot"].transient
    assert conn.execute("SELECT count(*) FROM imdb.title").fetchone()[0] == 0
    marker = tmp_path / "marker"
    p = ImdbParams(run_key="transient", snapshot_id=fx.SNAPSHOT_ID, fault_injection=f"transient_once:{marker}")
    checked = pipeline.check_files_step(p)
    with pytest.raises(StepError) as e:
        pipeline.run_step("load_snapshot", p, lambda: pipeline.load_step(p, checked))
    assert e.value.transient
    assert pipeline.load_step(p, checked)["status"] == "loaded"  # 第 2 次成功


def test_fault_injection_rejected_outside_test(monkeypatch):
    monkeypatch.setenv("ETL_ENV", "prod")
    with pytest.raises(ConfigError):
        pipeline.settings_for(ImdbParams(run_key="x", fault_injection="before_commit"))


def test_lock_contention_and_db_errors_are_transient():
    assert pipeline.is_transient(ImdbBusy("busy"))
    assert pipeline.is_transient(psycopg.OperationalError("down"))


# ---------------------------------------------------------------- DAG amazon_movies_task4_match（AC11）

MATCH_ORDER = ["check_inputs", "normalize", "candidates_decide", "cluster_map", "verify", "finalize_report", "run_gate"]


@pytest.fixture(scope="module")
def match_dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_movies_task4_match"]


def test_match_dag_tasks_in_dependency_order(match_dag):
    assert set(match_dag.task_dict) == set(MATCH_ORDER)
    for a, b in zip(MATCH_ORDER, MATCH_ORDER[1:]):
        assert match_dag.task_dict[a].downstream_task_ids == {b}
    assert _rule(match_dag, "finalize_report") == "all_done" and _rule(match_dag, "run_gate") == "all_done"
    assert all(_rule(match_dag, t) == "all_success" for t in MATCH_ORDER[1:5])


def test_match_dag_manual_trigger_retries_and_params(match_dag):
    assert match_dag.schedule is None and match_dag.catchup is False
    assert match_dag.max_active_runs == 1 and match_dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in match_dag.tasks)
    assert set(match_dag.params) == {"extraction_id", "t3_rule_version", "imdb_snapshot_id", "norm_version",
                                     "rule_version", "batch_size", "limit", "fault_injection"}
    assert match_dag.params["limit"] is None and match_dag.params["batch_size"] == 500


def test_match_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_movies_task4_match.py").read_text().split("with DAG(")[0]
    assert "from etl_task4" not in top.replace("    from etl_task4", "")


@pytest.fixture
def world(conn, make_settings, tmp_path):
    fx_ = build(conn, make_settings, tmp_path, match_products())
    reset_task4(conn)
    return fx_, load_imdb(conn, tmp_path, fx.MATCH)


def run_match(p: MatchParams) -> tuple[dict, dict, tuple[bool, str]]:
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
    attempt("normalize", (lambda: pipeline.normalize_step(p, c)) if c else None)
    attempt("candidates_decide", (lambda: pipeline.candidates_decide_step(p, c)) if out["normalize"] else None)
    attempt("cluster_map", (lambda: pipeline.cluster_map_step(p, c)) if out["candidates_decide"] else None)
    attempt("verify", (lambda: pipeline.match_verify_step(p, out["cluster_map"])) if out["cluster_map"] else None)
    final = pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_step(
        p, {n: out.get(n) for n in pipeline.MATCH_STEP_NAMES}))
    return out, failed, pipeline.gate_decision(final)


def test_match_success_then_rerun_is_reused(conn, world, env):
    fx_, sid = world
    out, failed, (ok, reason) = run_match(MatchParams(run_key="m_ok", extraction_id=fx_.eid, imdb_snapshot_id=sid))
    assert not failed and ok, (failed, reason)
    assert out["normalize"]["status"] == out["candidates_decide"]["status"] == "done"
    version = out["cluster_map"]["mapping_version_id"]
    assert out["verify"]["product_edition_rows"] == out["check_inputs"]["manifest_rows"] == len(match_products())
    d = env / "task4" / fx_.eid / version / "m_ok"
    summary = json.loads((d / "summary.json").read_text())
    assert summary["final_status"] == "succeeded" and set(summary["tasks"].values()) == {"success"}
    assert (d / "report.md").exists() and (d / "examples.md").exists() and summary["stats"]["mapping_version_id"] == version
    rows_before = conn.execute("SELECT (SELECT count(*) FROM task4.merge_decision), (SELECT count(*) FROM "
                               "task4.field_assertion), (SELECT count(*) FROM task4.mapping_version)").fetchone()
    out, failed, (ok, _) = run_match(MatchParams(run_key="m_rerun", extraction_id=fx_.eid, imdb_snapshot_id=sid))
    assert ok and not failed
    assert {out[n]["status"] for n in ("normalize", "candidates_decide", "cluster_map")} == {"reused"}
    assert out["cluster_map"]["mapping_version_id"] == version
    assert conn.execute("SELECT (SELECT count(*) FROM task4.merge_decision), (SELECT count(*) FROM "
                        "task4.field_assertion), (SELECT count(*) FROM task4.mapping_version)").fetchone() == rows_before
    assert conn.execute("SELECT status FROM task4.run WHERE airflow_run_id = 'm_rerun'").fetchone() == ("reused",)
    assert json.loads((env / "task4" / fx_.eid / version / "m_rerun" / "summary.json").read_text())["final_status"] \
        == "succeeded"


def test_match_missing_imdb_snapshot_fails_check_without_retry(conn, world, env):
    fx_, _ = world
    out, failed, (ok, reason) = run_match(MatchParams(run_key="m_missing", extraction_id=fx_.eid,
                                                      imdb_snapshot_id="imdb_2099-01-01"))
    assert not ok and set(failed) == {"check_inputs"} and not failed["check_inputs"].transient
    summary = json.loads((env / "task4" / "_failures" / "m_missing" / "summary.json").read_text())
    assert summary["tasks"] == {"check_inputs": "failed", "normalize": "not_run", "candidates_decide": "not_run",
                                "cluster_map": "not_run", "verify": "not_run"}
    assert summary["final_status"] == "failed" and summary["failures"][0]["task"] == "check_inputs"
    assert (env / "task4" / "_failures" / "m_missing" / "check_inputs-try1.json").exists()
    assert conn.execute("SELECT count(*) FROM task4.run").fetchone()[0] == 0


def test_match_unknown_rule_version_and_extraction_are_deterministic_failures(conn, world):
    fx_, sid = world
    _, failed, (ok, _) = run_match(MatchParams(run_key="m_rule", extraction_id=fx_.eid, imdb_snapshot_id=sid,
                                               rule_version="t4r99"))
    assert not ok and set(failed) == {"check_inputs"} and not failed["check_inputs"].transient
    _, failed, (ok, _) = run_match(MatchParams(run_key="m_eid", extraction_id="0" * 64, imdb_snapshot_id=sid))
    assert not ok and set(failed) == {"check_inputs"} and not failed["check_inputs"].transient


def test_match_limited_run_stops_before_decisions_and_makes_no_mapping(conn, world):
    fx_, sid = world
    out, failed, (ok, _) = run_match(MatchParams(run_key="m_limit", extraction_id=fx_.eid, imdb_snapshot_id=sid,
                                                 limit=3))
    assert not ok and out["normalize"]["counts"]["normalize"]["remaining"] > 0
    assert set(failed) == {"candidates_decide"} and not failed["candidates_decide"].transient
    assert conn.execute("SELECT count(*) FROM task4.mapping_version").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM task4.run").fetchall() == [("failed",)]


def test_match_fault_injection_rejected_outside_test(monkeypatch):
    monkeypatch.setenv("ETL_ENV", "prod")
    with pytest.raises(ConfigError):
        pipeline.settings_for(MatchParams(run_key="x", fault_injection="before_commit:decide:1"))


def test_match_transient_once_is_retryable_and_the_second_try_succeeds(conn, world, tmp_path):
    fx_, sid = world
    marker = tmp_path / "marker"
    p = MatchParams(run_key="m_transient", extraction_id=fx_.eid, imdb_snapshot_id=sid,
                    fault_injection=f"transient_once:{marker}")
    checked = pipeline.check_inputs_step(p)
    with pytest.raises(StepError) as e:
        pipeline.run_step("normalize", p, lambda: pipeline.normalize_step(p, checked))
    assert e.value.transient
    assert conn.execute("SELECT count(*) FROM task4.run").fetchone()[0] == 0
    assert pipeline.normalize_step(p, checked)["status"] == "done"      # 第 2 次成功


def test_match_lock_contention_is_a_deterministic_failure(conn, world, base_settings):
    """AC10：另一个运行持有抽取锁时，新的运行拿不到锁，确定性失败（不重试），库里不留部分行。"""
    from etl_task1.db import connect

    fx_, sid = world
    p = MatchParams(run_key="m_busy", extraction_id=fx_.eid, imdb_snapshot_id=sid)
    checked = pipeline.check_inputs_step(p)
    with connect(base_settings) as other, extraction_lock(other, fx_.eid):
        with pytest.raises(StepError) as e:
            pipeline.run_step("normalize", p, lambda: pipeline.normalize_step(p, checked))
        assert isinstance(e.value.cause, RunBusy) and not e.value.transient
        assert conn.execute("SELECT count(*) FROM task4.run").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task4.field_assertion").fetchone()[0] == 0
    assert not pipeline.is_transient(RunBusy("busy"))
    assert pipeline.normalize_step(p, checked)["status"] == "done"      # 锁释放后可以运行


TABLES = ("product_edition", "field_assertion", "source_record", "imdb_title_key", "candidate", "merge_decision",
          "entity_stage", "movie_identity", "mapping_version", "movie", "product_movie", "product_match_status",
          "mapping_event", "cluster_check")


def table_counts(conn):
    return {t: conn.execute(f"SELECT count(*) FROM task4.{t}").fetchone()[0] for t in TABLES}


@pytest.mark.parametrize("stage,table", [("normalize", "field_assertion"), ("decide", "merge_decision"),
                                         ("decide_pair", "merge_decision")])
def test_match_resume_after_a_failed_batch_only_does_the_remaining_batches(conn, world, tmp_path, stage, table):
    """AC10：批大小 2，在某阶段第 2 批提交前注入故障。故障后只有第 1 批的行，第 2 批没有部分行；
    同一 DAG run 重试只处理剩余批次，最终行数与一次跑完相同且没有重复。"""
    fx_, sid = world
    p = MatchParams(run_key="m_resume", extraction_id=fx_.eid, imdb_snapshot_id=sid, batch_size=2,
                    fault_injection=f"before_commit:{stage}:2")
    out, failed, (ok, _) = run_match(p)
    broken = {"normalize": "normalize", "decide": "candidates_decide", "decide_pair": "candidates_decide"}[stage]
    assert not ok and set(failed) == {broken} and not failed[broken].transient
    assert conn.execute("SELECT status, failure_reason FROM task4.run").fetchone()[0] == "failed"
    done = {r[0] for r in conn.execute("SELECT entity_id FROM task4.entity_stage WHERE stage = %s", (stage,))}
    assert len(done) == 2                                              # 只有第 1 批（2 个实体）提交了
    assert conn.execute("SELECT count(*) FROM task4.run_batch WHERE stage = %s", (stage,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM task4.mapping_version").fetchone()[0] == 0
    if stage == "normalize":
        asins = {r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM task4.field_assertion")}
        assert {a.split("#")[0] for a in asins} <= done                # 第 2 批没有部分行
    elif stage == "decide":
        lefts = {r[0] for r in conn.execute("SELECT DISTINCT left_entity FROM task4.candidate WHERE right_kind = 'imdb'")}
        assert lefts <= done
    else:
        blocks = {f"block:{r[0]}" for r in conn.execute(
            "SELECT DISTINCT a.normalized_value FROM task4.candidate c JOIN task4.field_assertion a ON a.entity_id = "
            "c.left_entity AND a.field_name = 'title' WHERE c.right_kind = 'product'")}
        assert blocks <= done

    # 重试：同一 DAG run（同一 run_key），不再注入故障
    retry = MatchParams(run_key="m_resume", extraction_id=fx_.eid, imdb_snapshot_id=sid, batch_size=2, try_number=2)
    out, failed, (ok, reason) = run_match(retry)
    assert ok and not failed, (failed, reason)
    assert conn.execute("SELECT count(*) FROM task4.run").fetchone()[0] == 1       # 接着用同一条运行
    assert conn.execute("SELECT status FROM task4.run").fetchone()[0] == "succeeded"
    stage_counts = out["candidates_decide" if stage != "normalize" else "normalize"]["counts"]
    key = {"normalize": ("normalize", "entities"), "decide": ("decide", "entities"),
           "decide_pair": ("decide_pair", "blocks")}[stage]
    resumed_work = stage_counts[key[0]][key[1]]
    total = conn.execute("SELECT count(*) FROM task4.entity_stage WHERE stage = %s", (stage,)).fetchone()[0]
    assert resumed_work == total - 2                                    # 只做了剩余的，没有重做第 1 批
    resumed = table_counts(conn)
    assert conn.execute("SELECT count(*) FROM (SELECT 1 FROM task4.entity_stage GROUP BY extraction_id, stage, "
                        "stage_key, entity_id HAVING count(*) > 1) d").fetchone()[0] == 0
    movie_ids = conn.execute("SELECT asin, movie_id FROM task4.product_movie ORDER BY 1, 2").fetchall()

    # 与一次跑完的结果相同（行数与 asin → movie_id）
    sid2 = (reset_task4(conn), load_imdb(conn, tmp_path, fx.MATCH, date="2026-10-31"))[1]
    clean, failed, (ok, _) = run_match(MatchParams(run_key="m_clean", extraction_id=fx_.eid, imdb_snapshot_id=sid2,
                                                   batch_size=2))
    assert ok and not failed
    assert table_counts(conn) == resumed
    assert conn.execute("SELECT asin, movie_id FROM task4.product_movie ORDER BY 1, 2").fetchall() == movie_ids


def test_match_dag_default_rule_version_is_the_registered_current_one(match_dag):
    from etl_task4 import CURRENT_RULE_VERSION
    from etl_task4.match import RULESETS

    assert match_dag.params["rule_version"] == CURRENT_RULE_VERSION == "t4r2"
    assert {"t4r1", "t4r2"} <= set(RULESETS) and RULESETS["t4r2"].w_director_strict == 4
    changed = {k for k in RULESETS["t4r1"].thresholds() if RULESETS["t4r1"].thresholds()[k] != RULESETS["t4r2"].thresholds()[k]}
    assert changed == {"version", "w_director_strict", "max_score"}
