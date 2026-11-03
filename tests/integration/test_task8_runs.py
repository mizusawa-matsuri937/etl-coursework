"""任务 8 完整运行：版本、阶段依赖与复用（spec AC09），分批恢复与并发锁（AC10）。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_runs.py
"""

from __future__ import annotations

import pytest

import _task8_helpers as h
from _task8_helpers import Product, mid
from etl_task1.db import connect
from etl_task8 import check
from etl_task8.rules import DEFAULT_RULES, stat_group_id
from etl_task8.run import RunBusy, extraction_lock
from etl_task8.versions import diff_versions
from task8.world import Rec, copies, unix

pytestmark = pytest.mark.integration

M1, M2, M3 = mid("M1"), mid("M2"), mid("M3")
A, B, C, D = "B00000A9A1", "B00000A9B1", "B00000A9C1", "B00000A9D1"
PRODUCTS = [Product(A, "movie", (M1,)), Product(B, "movie", (M1,)), Product(C, "movie", (M2,)),
            Product(D, "concert_other")]
# 内容表（不含运行、批次审计与版本元数据）：中断重跑、复用都不应改变它们
CONTENT = ("review_clean", "review_issue", "asin_signature", "signature_overlap", "review_family", "family_member",
           "logical_review", "review_mapping", "reviewer", "dup_candidate", "logical_review_work", "stat_group",
           "stat_group_member", "product_review_stats", "movie_review_stats", "stat_group_review_stats")


def world() -> list[Rec]:
    """A、B 评论集合相同（10 条）；C 是其中 6 条（Jaccard 0.6，阈值 0.5 同一家族、0.7 自成家族）；D 另有 2 条。"""
    recs = []
    for i in range(10):
        recs += copies([A, B, C] if i < 6 else [A, B], f"U{i}", text=f"review {i}", score=f"{1 + i % 5}.0",
                       time=unix(i))
    recs += [Rec(D, "U0", text="concert review"), Rec(D, "U9", text="another", helpfulness="0/0")]
    return recs


def content_digest(conn) -> dict[str, tuple]:
    """各内容表的行数与摘要（去掉本运行内编号的 batch_no）。"""
    return {t: conn.execute(f"SELECT count(*), md5(coalesce(string_agg((to_jsonb(x) - 'batch_no')::text, '|' "
                            f"ORDER BY (to_jsonb(x) - 'batch_no')::text), '')) FROM task8.{t} x").fetchone()
            for t in CONTENT}


def all_rows_digest(conn) -> dict[str, tuple]:
    """task8 全部表（task8.run 除外）的行数与摘要：复用运行只允许新增一条 run 审计。"""
    tables = [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'task8' "
                                         "AND table_name <> 'run' ORDER BY 1")]
    return {t: conn.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                            f"FROM task8.{t} x").fetchone() for t in tables}


# ---------------------------------------------------------------- AC09

def test_ac09_reuse_rule_change_and_cv_change(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, world())
    h.seed_upstream(conn, eid, PRODUCTS)
    h.seed_upstream(conn, eid, [Product(A, "movie", (M1,)), Product(B, "movie", (M1,)), Product(C, "movie", (M3,)),
                                Product(D, "concert_other")], cv="cv0002", base=False)
    r1 = h.execute(conn, eid, partitions=2)
    assert (r1.status, r1.review_dedup_version_id) == ("succeeded", "rd0001")
    before = all_rows_digest(conn)
    r2 = h.execute(conn, eid, partitions=2)                    # 同输入：reused，业务行不变
    assert (r2.status, r2.review_dedup_version_id) == ("reused", "rd0001")
    assert all_rows_digest(conn) == before
    assert [r[0] for r in conn.execute("SELECT status FROM task8.run ORDER BY started_at")] == ["succeeded", "reused"]
    v1 = h.version(conn, "rd0001")
    g = lambda rd: dict(conn.execute("SELECT movie_id, stat_group_id FROM task8.movie_review_stats "   # noqa: E731
                                     "WHERE review_dedup_version_id = %s", (rd,)).fetchall())
    assert g("rd0001") == {M1: stat_group_id(min(M1, M2)), M2: stat_group_id(min(M1, M2))}   # C 的 6 条与 A 共享

    # 改家族阈值（新规则版本）：review_clean 复用，review_family 及下游重算
    fam07 = DEFAULT_RULES.with_version("t8x_fam07", family_jaccard=0.7)
    r3 = h.execute(conn, eid, fam07, partitions=2)
    assert (r3.status, r3.review_dedup_version_id) == ("succeeded", "rd0002")
    assert r3.counts["review_clean"].get("skipped") == "complete"
    assert all(r3.counts[s].get("skipped") is None for s in ("review_family", "review_dedup", "review_attribution"))
    v2 = h.version(conn, "rd0002")
    assert v2["clean_stage_key"] == v1["clean_stage_key"]
    assert all(v2[k] != v1[k] for k in ("family_stage_key", "dedup_stage_key", "attribution_stage_key"))
    assert g("rd0002") == {M1: stat_group_id(M1), M2: stat_group_id(M2)}                     # C 自成家族：不再共享

    # 改 cv 版本：只重算 review_attribution
    r4 = h.execute(conn, eid, cv="cv0002", partitions=2)
    assert (r4.status, r4.review_dedup_version_id) == ("succeeded", "rd0003")
    assert all(r4.counts[s].get("skipped") == "complete" for s in ("review_clean", "review_family", "review_dedup"))
    assert r4.counts["review_attribution"].get("skipped") is None
    v3 = h.version(conn, "rd0003")
    assert all(v3[k] == v1[k] for k in ("clean_stage_key", "family_stage_key", "dedup_stage_key"))
    assert v3["attribution_stage_key"] != v1["attribution_stage_key"]

    # report diff：阶段键、计数与作品统计的变化；旧版本仍可查、仍通过核对
    d12 = diff_versions(conn, "rd0001", "rd0002")
    assert {(x[0], x[1]) for x in d12 if x[0] == "stage"} == {("stage", "family"), ("stage", "dedup"),
                                                              ("stage", "attribution")}
    hi = max(M1, M2)                                            # 组 ID 由组内最小 movie_id 生成，较大的那部换组
    assert ("movie", hi, "stat_group_id", stat_group_id(min(M1, M2)), stat_group_id(hi), "变化") in d12
    assert any(x[0] == "count" and x[2] == "logical_reviews" for x in d12)
    d13 = diff_versions(conn, "rd0001", "rd0003")
    assert [x[1] for x in d13 if x[0] == "stage"] == ["attribution"]
    assert ("movie", M2, "*", "PRESENT", "ABSENT", "移除") in d13 and ("movie", M3, "*", "ABSENT", "PRESENT", "新增") in d13
    assert not [x for x in d13 if x[0] == "count" and x[2] == "logical_reviews"]
    assert [r[0] for r in conn.execute("SELECT review_dedup_version_id FROM task8.dedup_version WHERE status = 'verified' "
                                       "ORDER BY 1")] == ["rd0001", "rd0002", "rd0003"]
    assert check.problems(check.verify(conn, "rd0001")) == []
    assert conn.execute("SELECT parent_version_id FROM task8.dedup_version WHERE review_dedup_version_id = 'rd0003'"
                        ).fetchone()[0] == "rd0002"


# ---------------------------------------------------------------- AC10

@pytest.mark.parametrize("stage, batch, committed", [
    ("review_clean", 2, ["r1-3"]),                     # 记录号区间每批 3 条：第 1 批落库，第 2 批回滚
    ("review_family", 2, ["sig0/2"]),                  # 签名第 1 个分区落库
    ("review_dedup", 2, ["unmapped"]),                 # 未映射单元落库，核验单元回滚
    ("review_family", 5, ["ovl0/2", "ovl1/2", "sig0/2", "sig1/2"]),   # 连通分量提交前：家族表没有半写入
])
def test_ac10_fault_before_commit_then_rerun_matches_one_shot(conn, make_settings, tmp_path, stage, batch, committed):
    eid = h.setup(conn, make_settings, tmp_path, world())
    h.seed_upstream(conn, eid, PRODUCTS)
    h.execute(conn, eid, partitions=2, batch_size=3)
    expected = content_digest(conn)
    h.reset(conn)

    def fail(st, batch_no):
        if (st, batch_no) == (stage, batch):
            raise RuntimeError("注入故障")

    with pytest.raises(RuntimeError, match="注入故障"):
        h.execute(conn, eid, partitions=2, batch_size=3, before_commit=fail)
    key = h.stage_key(conn, eid, stage)
    assert [r[0] for r in conn.execute("SELECT entity_id FROM task8.entity_stage WHERE stage_key = %s ORDER BY 1",
                                       (key,))] == committed
    assert conn.execute("SELECT status FROM task8.run").fetchone()[0] == "failed"
    assert conn.execute("SELECT count(*) FROM task8.dedup_version").fetchone()[0] == 0
    if stage == "review_family":
        assert conn.execute("SELECT count(*) FROM task8.review_family").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task8.family_member").fetchone()[0] == 0
    if stage == "review_clean":
        assert [r[0] for r in conn.execute("SELECT record_no FROM task8.review_clean ORDER BY 1")] == [1, 2, 3]
    r = h.execute(conn, eid, partitions=2, batch_size=3)        # 重跑跳过已提交单元
    assert r.status == "succeeded"
    assert content_digest(conn) == expected
    first = r.counts[stage]
    if stage == "review_clean":
        assert first["batches"] == len(world()) // 3 + (1 if len(world()) % 3 else 0) - 1
    elif stage == "review_dedup":
        assert first["units"] == 3                              # verify、p0/2、p1/2


def test_ac10_concurrent_run_fails_on_advisory_lock(conn, make_settings, tmp_path, base_settings):
    eid = h.setup(conn, make_settings, tmp_path, world())
    h.seed_upstream(conn, eid, PRODUCTS)
    with connect(base_settings) as other, extraction_lock(other, eid):
        with pytest.raises(RunBusy, match="另一个任务 8 运行"):
            h.execute(conn, eid, partitions=2)
        with pytest.raises(RunBusy):
            h.run(conn, eid, "review_clean")
    assert conn.execute("SELECT count(*) FROM task8.run").fetchone()[0] == 0
    assert h.execute(conn, eid, partitions=2).status == "succeeded"     # 锁释放后正常运行
