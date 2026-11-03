"""任务 9 完整运行：血缘版本、复用、分批恢复、并发锁与 verify 失败重跑（spec AC16；R36–R40）。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_runs.py
"""

from __future__ import annotations

import pytest

import _task9_helpers as h
from etl_task1.db import connect
from etl_task9 import check
from etl_task9.run import RunBusy, extraction_lock
from etl_task9.runner import VerificationFailed, execute_lineage, run_stage, STEPS

TABLES = ("source_record", "assertion", "derivation", "field_selection", "field_impact", "relocation")


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    return h.seed(conn, make_settings, tmp_path)


def _run(conn, t9, **kw):
    kw.setdefault("snapshot_root", t9.snapshot_root)
    kw.setdefault("snapshot_workers", 2)
    kw.setdefault("batch_size", 500)
    return execute_lineage(conn, t9.inputs(), **kw)


def _digest(conn):
    return {t: conn.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                            f"FROM (SELECT * FROM task9.{t}) x").fetchone() for t in TABLES}


def _content(conn):
    """不含运行 ID 与时间的内容摘要：故障恢复后与一次跑完比较。"""
    q = {"source_record": "source_record_id, locator_key, content_hash, evidence_status",
         "assertion": "assertion_id, level, entity_id, field_name, member_key, normalized_value, upstream_key",
         "derivation": "child_kind, child_id, parent_kind, parent_id, operation, rule_id, role",
         "field_selection": "selection_id, value_text, status, trace_status",
         "field_impact": "source_record_id, selection_id, depth"}
    return {t: conn.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                            f"FROM (SELECT {c} FROM task9.{t}) x").fetchone() for t, c in q.items()}


def test_ac16_lineage_version_verified_and_rerun_reused(conn, t9):
    """同输入运行两次：第一次生成 lv0001（verified，运行 succeeded，check 无问题）；第二次为 reused，只多一条运行审计，
    各表行与内容不变。"""
    r1 = _run(conn, t9)
    assert (r1.status, r1.lineage_version_id) == ("succeeded", "lv0001")
    row = conn.execute("SELECT v.status, r.status, v.counts ? 'upstream_counts' FROM task9.lineage_version v "
                       "JOIN task9.run r ON r.run_id = v.run_id WHERE v.lineage_version_id = 'lv0001'").fetchone()
    assert row == ("verified", "succeeded", True)
    assert check.problems(check.verify(conn, "lv0001")) == []
    before = _digest(conn)
    r2 = _run(conn, t9)
    assert (r2.status, r2.lineage_version_id) == ("reused", "lv0001")
    assert _digest(conn) == before
    assert conn.execute("SELECT count(*) FROM task9.lineage_version").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM task9.run WHERE run_id = %s", (r2.run_id,)).fetchone()[0] == "reused"


def test_ac16_fault_in_assertions_batch_resumes_to_same_result(conn, t9):
    """批大小 5，在 assertions 第 2 批提交前注入故障：只有已提交的批次落库，运行 failed；重跑结果与一次跑完相同。"""
    class Boom(RuntimeError):
        pass

    def hook(stage, batch_no):
        if stage == "assertions" and batch_no == 2:
            raise Boom("injected")

    with pytest.raises(Boom):
        _run(conn, t9, batch_size=5, before_commit=hook)
    ak = h.stage_key(conn, "assertions")
    batches = conn.execute("SELECT count(*) FROM task9.run_batch WHERE stage = 'assertions'").fetchone()[0]
    assert batches == 1
    assert conn.execute("SELECT count(*) FROM task9.entity_stage WHERE stage_key = %s AND entity_id = '__complete__'",
                        (ak,)).fetchone()[0] == 0
    assert conn.execute("SELECT status FROM task9.run ORDER BY started_at DESC LIMIT 1").fetchone()[0] == "failed"
    r = _run(conn, t9, batch_size=5)
    assert r.status == "succeeded"
    resumed = _content(conn)
    h.reset(conn)              # 一次跑完：清空 task9，在同一套上游上重跑
    assert _run(conn, t9).status == "succeeded"
    assert _content(conn) == resumed


def test_ac16_concurrent_run_fails_deterministically(conn, t9, base_settings):
    """另一个连接持有抽取锁时，运行立即以 RunBusy 失败，不写任何行。"""
    with connect(base_settings) as other, extraction_lock(other, t9.eid):
        with pytest.raises(RunBusy):
            _run(conn, t9)
    assert conn.execute("SELECT count(*) FROM task9.run").fetchone()[0] == 0


def test_ac16_verify_failure_keeps_unverified_then_rerun_verifies(conn, t9):
    """verify 注入失败：运行 failed、版本 lv0001 保持 unverified，不会被复用；同输入重跑在原版本上重写并 verified。"""
    with pytest.raises(VerificationFailed):
        _run(conn, t9, inject_verify_failure=True)
    assert conn.execute("SELECT status FROM task9.lineage_version WHERE lineage_version_id = 'lv0001'").fetchone()[0] \
        == "unverified"
    assert conn.execute("SELECT status FROM task9.run ORDER BY started_at DESC LIMIT 1").fetchone()[0] == "failed"
    r = _run(conn, t9)
    assert (r.status, r.lineage_version_id) == ("succeeded", "lv0001")
    assert conn.execute("SELECT status, run_id FROM task9.lineage_version").fetchall() == [("verified", r.run_id)]


def test_ac16_dag_steps_share_one_run_and_reuse(conn, t9):
    """DAG 的七个步骤按顺序执行共用一条运行，verify 后 succeeded；另一个 DAG run 的第一个步骤为 reused，其余步骤跳过。"""
    kw = dict(snapshot_root=t9.snapshot_root, snapshot_workers=2, batch_size=500)
    runs = {run_stage(conn, t9.inputs(), s, airflow_run_id="dag1", **kw).run_id for s in STEPS}
    assert len(runs) == 1
    assert conn.execute("SELECT r.status, v.status FROM task9.run r JOIN task9.lineage_version v "
                        "ON v.lineage_version_id = r.lineage_version_id WHERE r.run_id = %s", (runs.pop(),)).fetchone() \
        == ("succeeded", "verified")
    out = [run_stage(conn, t9.inputs(), s, airflow_run_id="dag2", **kw).status for s in STEPS]
    assert out == ["reused"] * len(STEPS)
