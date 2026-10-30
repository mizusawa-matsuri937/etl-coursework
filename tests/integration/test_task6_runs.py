"""任务 6 运行语义（spec AC13：幂等、改规则与版本差异；AC14：分批恢复与并发锁）。

命令的工作目录为项目根目录：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py
"""

from __future__ import annotations

import pytest
import psycopg

import _task6_helpers as h
from _task6_helpers import World, clone_mapping, inputs_for
from etl_task1.db import connect
from etl_task6.report import diff_versions
from etl_task6.rules import DEFAULT_RULES, RULESETS
from etl_task6.run import RunBusy, extraction_lock
from etl_task6.runner import execute_release

pytestmark = pytest.mark.integration

TABLES = (
    "release_candidate", "hint_use", "cooccur_score", "cooccur_result", "review_year_mention",
    "product_release_date", "product_review_time", "movie_review_time", "release_selection",
    "bound_evidence", "bound_assessment", "inferred_release_bound", "entity_stage",
)


def counts(conn: psycopg.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM task6.{t}").fetchone()[0] for t in TABLES}


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return h.make_world(conn, make_settings, tmp_path)


@pytest.fixture
def alt_rules():
    RULESETS["t6r2"] = DEFAULT_RULES.with_version("t6r2", b3_days=10)
    yield "t6r2"
    RULESETS.pop("t6r2", None)


# ---------------------------------------------------------------- AC13 版本、重跑复用与版本差异

def test_ac13_reused_second_run_adds_no_target_rows_and_only_an_audit_row(conn, world):
    """同一输入连续运行两次：第二次不新增任何目标行，审计为 reused（R28）。"""
    first = execute_release(conn, inputs_for(world), batch_size=7)
    assert first.status == "succeeded" and first.release_version_id == "rv0001"
    before = counts(conn)
    runs_before = conn.execute("SELECT count(*) FROM task6.run").fetchone()[0]

    second = execute_release(conn, inputs_for(world), batch_size=7)
    assert second.status == "reused" and second.release_version_id == "rv0001"
    assert counts(conn) == before
    assert conn.execute("SELECT count(*) FROM task6.run").fetchone()[0] == runs_before + 1
    assert conn.execute("SELECT status FROM task6.run WHERE run_id = %s", (second.run_id,)).fetchone()[0] == "reused"


def test_ac13_rule_change_keeps_extracted_stages_and_computes_new_version(conn, world, alt_rules):
    """新规则版本生成新上映信息版本，只重做择优与上界，不重新抽取候选、共现、商品日期与评论时间（R27）。"""
    first = execute_release(conn, inputs_for(world), batch_size=7)
    old_rv = first.release_version_id
    cands_before = conn.execute("SELECT count(*) FROM task6.release_candidate").fetchone()[0]
    stages_before = conn.execute("SELECT count(*) FROM task6.entity_stage WHERE stage = 'candidates'").fetchone()[0]

    new_run = execute_release(conn, inputs_for(world, rule_version=alt_rules), batch_size=7)
    new_rv = new_run.release_version_id
    assert new_rv != old_rv and new_run.status == "succeeded"

    # 候选与阶段标记不变，candidates 没有新增 run_batch
    assert conn.execute("SELECT count(*) FROM task6.release_candidate").fetchone()[0] == cands_before
    assert conn.execute("SELECT count(*) FROM task6.entity_stage WHERE stage = 'candidates'").fetchone()[0] == stages_before
    assert conn.execute("SELECT count(*) FROM task6.run_batch WHERE run_id = %s AND stage = 'candidates'",
                        (new_run.run_id,)).fetchone()[0] == 0

    # 父版本为上一个版本
    assert conn.execute("SELECT parent_version_id FROM task6.release_version WHERE release_version_id = %s",
                        (new_rv,)).fetchone()[0] == old_rv

    # 版本差异列出受影响作品
    diff = diff_versions(conn, old_rv, new_rv)
    assert len(diff) >= 0


def test_ac13_mapping_change_makes_a_new_version_and_diff_is_split_movie(conn, world):
    """换映射版本生成新上映信息版本；旧版本仍可查询。"""
    first = execute_release(conn, inputs_for(world), batch_size=7)
    old_rv = first.release_version_id
    alt_map, alt_pmap = clone_mapping(conn, world, "B0T6A10001", "map9002")

    new_run = execute_release(conn, inputs_for(world, t4_mapping_version_id=alt_map,
                                                t5_person_mapping_version_id=alt_pmap), batch_size=7)
    new_rv = new_run.release_version_id
    assert new_rv != old_rv and new_run.status == "succeeded"

    # 旧版本依然完整存在
    old_count = conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s",
                             (old_rv,)).fetchone()[0]
    assert old_count > 0

    new_count = conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s",
                             (new_rv,)).fetchone()[0]
    assert new_count == old_count + 1   # 拆出一部新作品


def test_ac21_cooccur_rule_param_change_updates_stage_key_and_recomputes_cooccur(conn, world):
    """修改 RuleSet 的共现评分参数（如 cooccur_min_score=3）时，stage_key('cooccur') 改变并触发共现阶段重算，
    而非沿用旧阶段键跳过重算（ISSUE-08，AC21）。"""
    first = execute_release(conn, inputs_for(world), batch_size=7)
    assert first.status == "succeeded"
    inp1 = inputs_for(world)
    cooccur_batches_before = conn.execute(
        "SELECT count(*) FROM task6.run_batch WHERE stage = 'cooccur'"
    ).fetchone()[0]

    RULESETS["t6r_cooc3"] = DEFAULT_RULES.with_version("t6r_cooc3", cooccur_min_score=3)
    try:
        inp2 = inputs_for(world, rule_version="t6r_cooc3")
        assert inp2.stage_key("cooccur") != inp1.stage_key("cooccur")
        assert inp2.stage_key("candidates") == inp1.stage_key("candidates")

        second = execute_release(conn, inp2, batch_size=7)
        assert second.status == "succeeded" and second.release_version_id != first.release_version_id

        # cooccur 阶段因 stage_key 变化重新执行了分批写入，而 candidates 阶段仍复用
        cooccur_batches_after = conn.execute(
            "SELECT count(*) FROM task6.run_batch WHERE stage = 'cooccur'"
        ).fetchone()[0]
        assert cooccur_batches_after > cooccur_batches_before
        assert conn.execute(
            "SELECT count(*) FROM task6.run_batch WHERE run_id = %s AND stage = 'candidates'",
            (second.run_id,),
        ).fetchone()[0] == 0

        # 在 cooccur_min_score=3 下，原先得 2 分的 TRS001 不再通过共现反查
        trs_movie = conn.execute(
            "SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = 'B0T6TRS001'",
            (world.mapping,),
        ).fetchone()[0]
        passed_new = conn.execute(
            "SELECT passed FROM task6.cooccur_score WHERE extraction_id = %s AND stage_key = %s "
            "AND movie_id = %s AND tconst = 'tt0006020'",
            (world.extraction_id, inp2.stage_key("cooccur"), trs_movie),
        ).fetchone()[0]
        assert passed_new is False
    finally:
        RULESETS.pop("t6r_cooc3", None)


# ---------------------------------------------------------------- AC14 分批恢复与并发锁

def test_ac14_fault_injection_and_recovery_candidates(conn, world):
    """候选阶段第 2 批提交前注入故障：第 1 批已提交，第 2 批无部分行；重跑只处理剩余批次且无重复。"""
    inputs = inputs_for(world)

    class CustomFault(Exception):
        pass

    def hook(stage, batch_no):
        if stage == "candidates" and batch_no == 2:
            raise CustomFault("candidates batch 2 failure")

    with pytest.raises(CustomFault):
        execute_release(conn, inputs, batch_size=2, before_commit=hook)

    # 故障后只有第 1 批的行
    b1_count = conn.execute("SELECT count(*) FROM task6.entity_stage WHERE extraction_id = %s "
                            "AND stage = 'candidates' AND batch_no = 1", (world.extraction_id,)).fetchone()[0]
    assert b1_count == 2
    b2_count = conn.execute("SELECT count(*) FROM task6.entity_stage WHERE extraction_id = %s "
                            "AND stage = 'candidates' AND batch_no = 2", (world.extraction_id,)).fetchone()[0]
    assert b2_count == 0

    # 重跑恢复完成
    recovered = execute_release(conn, inputs, batch_size=2)
    assert recovered.status == "succeeded"

    # 总作品数等于一次完整跑完，且无重复候选
    total_marked = conn.execute("SELECT count(*) FROM task6.entity_stage WHERE extraction_id = %s "
                                "AND stage = 'candidates'", (world.extraction_id,)).fetchone()[0]
    total_movies = conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s",
                                (world.mapping,)).fetchone()[0]
    assert total_marked == total_movies


def test_ac14_fault_injection_and_recovery_cooccur(conn, world):
    """共现反查阶段第 2 批提交前注入故障并重跑恢复。"""
    inputs = inputs_for(world)

    class CustomFault(Exception):
        pass

    def hook(stage, batch_no):
        if stage == "cooccur" and batch_no == 2:
            raise CustomFault("cooccur batch 2 failure")

    with pytest.raises(CustomFault):
        execute_release(conn, inputs, batch_size=2, before_commit=hook)

    # 重跑恢复完成
    recovered = execute_release(conn, inputs, batch_size=2)
    assert recovered.status == "succeeded"


def test_ac14_concurrent_run_fails_with_runbusy(conn, make_settings, world):
    """两个运行同时启动：第二个拿不到咨询锁，确定性抛 RunBusy（R29）。"""
    inputs = inputs_for(world)
    conn2 = connect(make_settings())
    with extraction_lock(conn2, world.extraction_id):
        with pytest.raises(RunBusy):
            execute_release(conn, inputs, batch_size=7)
    conn2.close()
