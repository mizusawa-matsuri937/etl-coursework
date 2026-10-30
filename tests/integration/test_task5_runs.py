"""任务 5 运行语义（spec AC09：幂等、改规则与版本差异；AC10：分批恢复与并发锁）。预期值按 AC05 夹具手算。"""

from __future__ import annotations

import pytest
from task5 import resolve as fx

from _task5_helpers import World, clone_mapping, inputs_for, make_world, remake_world
from etl_task1.db import connect
from etl_task5 import cluster
from etl_task5.rules import DEFAULT_RULES, RULESETS
from etl_task5.run import RunBusy, extraction_lock
from etl_task5.runner import execute_resolve

pytestmark = pytest.mark.integration

R = "B0T5R00"
TABLES = ("person_mention", "entity_stage", "link_candidate", "link_decision", "person_group", "group_member",
          "person_candidate", "person_decision", "person_identity", "person_mapping_version", "person", "mention_person",
          "person_alias", "iteration_round", "movie_person", "movie_person_source")


def counts(conn):
    return {t: conn.execute(f"SELECT count(*) FROM task5.{t}").fetchone()[0] for t in TABLES}


def run(conn, world: World, rules=None, batch=3, **over):
    return execute_resolve(conn, inputs_for(world, **over), rules, batch_size=batch)


@pytest.fixture
def cases(conn, make_settings, tmp_path):
    return make_world(conn, make_settings, tmp_path, fx.case_products(), fx.CASES_IMDB)


@pytest.fixture
def alt_rules():
    RULESETS["t5r4"] = DEFAULT_RULES.with_version("t5r4", auto_w_min=3)       # 需要三位合作者才自动合并
    yield "t5r4"
    RULESETS.pop("t5r4")


def pid(conn, pmap, asin, raw):
    return conn.execute("SELECT mp.person_id FROM task5.mention_person mp JOIN task5.person_mention m USING (mention_key) "
                        "WHERE mp.person_mapping_version_id = %s AND m.asin = %s AND m.raw_value = %s "
                        "ORDER BY m.mention_key LIMIT 1", (pmap, asin, raw)).fetchone()[0]


# ---------------------------------------------------------------- AC09

def test_ac09_reused_second_run_adds_no_target_rows_and_only_an_audit_row(conn, cases):
    first = run(conn, cases)
    before = counts(conn)
    runs_before = conn.execute("SELECT count(*) FROM task5.run").fetchone()[0]
    second = run(conn, cases)
    assert first.status == "succeeded" and (second.status, second.person_mapping_version_id) == (
        "reused", first.person_mapping_version_id)
    assert counts(conn) == before                                              # 不新增也不修改目标行
    assert conn.execute("SELECT count(*) FROM task5.run").fetchone()[0] == runs_before + 1
    assert conn.execute("SELECT status FROM task5.run WHERE run_id = %s", (second.run_id,)).fetchone()[0] == "reused"


def test_ac09_rule_change_keeps_mentions_and_recomputes_decisions_onward(conn, cases, alt_rules):
    old = run(conn, cases).person_mapping_version_id
    mentions_before = conn.execute("SELECT count(*), min(run_id) FROM task5.person_mention").fetchone()
    stages_before = conn.execute("SELECT count(*) FROM task5.entity_stage WHERE stage = 'mentions'").fetchone()[0]
    new_run = run(conn, cases, rule_version=alt_rules)
    new = new_run.person_mapping_version_id
    assert new != old and new_run.status == "succeeded"
    assert conn.execute("SELECT count(*), min(run_id) FROM task5.person_mention").fetchone() == mentions_before   # 不重新抽取提及
    assert conn.execute("SELECT count(*) FROM task5.entity_stage WHERE stage = 'mentions'").fetchone()[0] == stages_before
    assert conn.execute("SELECT count(*) FROM task5.run_batch WHERE run_id = %s AND stage = 'mentions'",
                        (new_run.run_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(DISTINCT rule_version) FROM task5.person_decision").fetchone()[0] == 2
    assert conn.execute("SELECT parent_version_id FROM task5.person_mapping_version WHERE person_mapping_version_id = %s",
                        (new,)).fetchone()[0] == old
    # 差异恰好是受影响的提及：D（两位同名合作者）的三位演员不再自动合并；A、B、C、E、F、H 不受影响
    changed = cluster.diff_versions(conn, old, new)
    asins = {k.split(":")[0] for k, *_ in changed}
    assert {f"{R}D02"} <= asins and not asins & {f"{R}{c}{n}" for c in "ABCEFH" for n in ("01", "02")}
    assert all(reason for *_, reason in changed)                                # 每个变化都带原因
    assert conn.execute("SELECT count(*) FROM task5.mapping_event WHERE person_mapping_version_id = %s", (new,)).fetchone()[0] \
        == len(changed) + conn.execute("SELECT count(*) FROM task5.mapping_event WHERE person_mapping_version_id = %s "
                                       "AND event = 'retire'", (new,)).fetchone()[0]
    assert pid(conn, old, f"{R}A01", "Dana Same") == pid(conn, new, f"{R}A01", "Dana Same")    # 锚点未变的 person_id 不变


def test_ac09_mapping_change_makes_a_new_version_and_the_diff_is_exactly_the_split_mention(conn, cases):
    old_run = run(conn, cases)
    old = old_run.person_mapping_version_id
    alt = clone_mapping(conn, cases, f"{R}A02")
    new_run = run(conn, cases, t4_mapping_version_id=alt)
    new = new_run.person_mapping_version_id
    assert new != old and new_run.status == "succeeded"
    assert conn.execute("SELECT t4_mapping_version_id FROM task5.person_mapping_version WHERE person_mapping_version_id = %s",
                        (new,)).fetchone()[0] == alt
    assert conn.execute("SELECT count(*) FROM task5.run_batch WHERE run_id = %s AND stage = 'mentions'",
                        (new_run.run_id,)).fetchone()[0] == 0                   # 提及只取解析与规范化版本，不重新抽取
    assert pid(conn, old, f"{R}A01", "Dana Same") == pid(conn, old, f"{R}A02", "Dana Same")
    changed = cluster.diff_versions(conn, old, new)
    assert [c[0].split(":")[0] for c in changed] == [f"{R}A02"] and changed[0][3] == "singleton"
    assert pid(conn, old, f"{R}A01", "Dana Same") == pid(conn, new, f"{R}A01", "Dana Same")   # 最小提及所在的部分保持原 person_id
    assert pid(conn, new, f"{R}A02", "Dana Same") != pid(conn, new, f"{R}A01", "Dana Same")


# ---------------------------------------------------------------- AC10

@pytest.mark.parametrize("stage", ["mentions", "credits", "filmography", "decide_pair"])
def test_ac10_fault_before_the_second_batch_commit_keeps_only_batch_one_and_the_rerun_finishes_the_rest(
        conn, make_settings, tmp_path, stage):
    """批大小 2，在某阶段第 2 批提交前注入故障：故障后只有第 1 批的行，重跑只处理剩余批次，最终行数与一次跑完相同。"""
    w = make_world(conn, make_settings, tmp_path, fx.case_products(), fx.CASES_IMDB)

    def boom(st, n):
        if (st, n) == (stage, 2):
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        execute_resolve(conn, inputs_for(w), batch_size=2, before_commit=boom)
    assert conn.execute("SELECT status FROM task5.run").fetchone()[0] == "failed"
    done = {r[0] for r in conn.execute("SELECT entity_id FROM task5.entity_stage WHERE stage = %s", (stage,))}
    assert len(done) == 2                                                     # 只有第 1 批提交
    assert conn.execute("SELECT count(*) FROM task5.run_batch WHERE stage = %s", (stage,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM task5.person_mapping_version").fetchone()[0] == 0
    if stage == "mentions":                                                   # 第 2 批没有部分行
        assert {r[0] for r in conn.execute("SELECT DISTINCT asin FROM task5.person_mention")} <= done
    elif stage in ("credits", "filmography"):
        assert {r[0] for r in conn.execute("SELECT DISTINCT split_part(mention_key, ':', 1) FROM task5.link_candidate "
                                           "WHERE route = %s", (stage,))} <= done
    resumed = execute_resolve(conn, inputs_for(w), batch_size=2)
    assert resumed.status == "succeeded"
    assert conn.execute("SELECT count(*) FROM (SELECT 1 FROM task5.entity_stage GROUP BY extraction_id, stage, stage_key, "
                        "entity_id HAVING count(*) > 1) d").fetchone()[0] == 0
    after = counts(conn)
    w2 = remake_world(conn, w, tmp_path, fx.CASES_IMDB)                                        # 一次跑完
    clean = execute_resolve(conn, inputs_for(w2), batch_size=2)
    assert clean.status == "succeeded"
    got = counts(conn)
    for t in ("person_mention", "link_candidate", "link_decision", "person_group", "person_candidate", "person_decision",
              "person", "mention_person", "movie_person"):
        assert got[t] == after[t], t


@pytest.mark.parametrize("stage", ["cluster", "movie_person"])
def test_ac10_fault_in_a_global_step_leaves_no_partial_rows_and_the_rerun_completes(conn, cases, stage):
    def boom(st, n):
        if st == stage:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        execute_resolve(conn, inputs_for(cases), batch_size=3, before_commit=boom)
    c = counts(conn)
    assert c["movie_person"] == 0 and c["movie_person_source"] == 0
    if stage == "cluster":
        assert c["person_mapping_version"] == 0 and c["person"] == 0 \
            and c["mention_person"] == 0 and c["person_alias"] == 0 and c["person_identity"] == 0
    else:
        assert c["person_mapping_version"] == 1 and c["person"] > 0          # 版本已提交，MoviePerson 回滚
    r = execute_resolve(conn, inputs_for(cases), batch_size=3)
    assert r.status == "succeeded" and counts(conn)["movie_person"] > 0
    assert counts(conn)["person_mapping_version"] == 1                        # 沿用已提交的版本，不重复生成


def test_ac10_second_run_cannot_take_the_extraction_lock_and_fails_deterministically(conn, cases, base_settings):
    with connect(base_settings) as other, extraction_lock(other, cases.fx.eid):
        with pytest.raises(RunBusy):
            execute_resolve(conn, inputs_for(cases), batch_size=3)
        assert conn.execute("SELECT count(*) FROM task5.run").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task5.person_mention").fetchone()[0] == 0
    assert execute_resolve(conn, inputs_for(cases), batch_size=3).status == "succeeded"      # 锁释放后可以运行
