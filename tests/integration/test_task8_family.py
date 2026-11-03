"""任务 8 评论家族 review_family（spec AC04；分区提交与连通分量单事务写入）。

上游表由任务 1 的装载器从合成 gzip 生成（tests/fixtures/task8/README.md）。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_family.py
"""

from __future__ import annotations

import hashlib

import pytest

import _task8_helpers as h
from etl_task8.rules import DEFAULT_RULES
from task8.world import Rec, copies, unix

pytestmark = pytest.mark.integration

A, B, C, D, E, F, G = (f"B0000000{x}1" for x in "ABCDEFG")


def ac04_world() -> list[Rec]:
    """A、B 评论集合相同（10 条）；C 是其中 6 条（Jaccard 0.6）；D 与 A 只共享 1 条（另有 2 条）；E 无共享（4 条）；
    F、G 各只有 1 条且相同。同一条评论 = 同一用户、同日、同评分、同摘要、同正文。"""
    recs: list[Rec] = []
    for i in range(10):
        on = [A, B] + ([C] if i < 6 else []) + ([D] if i == 0 else [])
        recs += copies(on, f"U{i}", text=f"Shared review number {i}.", time=unix(i))
    recs += [Rec(D, f"UD{i}", text=f"Only on D {i}") for i in range(2)]
    recs += [Rec(E, f"UE{i}", text=f"Only on E {i}") for i in range(4)]
    recs += copies([F, G], "UF", text="Order for a friend")
    return recs


def families(conn, eid: str) -> dict[tuple[str, ...], tuple]:
    key = h.stage_key(conn, eid, "review_family")
    out = {}
    for fid, asins, weak, biggest, sigs, basis in conn.execute(
            "SELECT f.family_id, array_agg(m.asin ORDER BY m.asin), f.weak_support, f.max_review_count, "
            "f.signature_count, f.basis FROM task8.review_family f JOIN task8.family_member m "
            "ON m.family_stage_key = f.family_stage_key AND m.family_id = f.family_id "
            "WHERE f.family_stage_key = %s GROUP BY f.family_stage_key, f.family_id", (key,)):
        out[tuple(asins)] = (fid, weak, biggest, sigs, basis)
    return out


def test_ac04_families_by_jaccard(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac04_world())
    out = h.run(conn, eid, "review_family", partitions=3)
    fams = families(conn, eid)
    assert set(fams) == {(A, B, C), (D,), (E,), (F, G)}
    fid = lambda asin: "rf_" + hashlib.sha256(asin.encode()).hexdigest()[:16]      # noqa: E731
    assert {k: v[0] for k, v in fams.items()} == {(A, B, C): fid(A), (D,): fid(D), (E,): fid(E), (F, G): fid(F)}
    assert {k: (v[1], v[2], v[3]) for k, v in fams.items()} == {
        (A, B, C): (False, 10, 2), (D,): (False, 3, 1), (E,): (False, 4, 1), (F, G): (True, 1, 1)}
    assert [e["jaccard"] for e in fams[(A, B, C)][4]] == [0.6] and fams[(A, B, C)][4][0]["shared"] == 6
    fam = out["review_family"]
    assert (fam["family_count"], fam["multi_asin_families"], fam["weak_support_families"], fam["weak_support_asins"],
            fam["signature_groups"]) == (4, 2, 1, 2, 5)
    key = h.stage_key(conn, eid, "review_family")
    sizes = dict(conn.execute("SELECT asin, review_count FROM task8.asin_signature WHERE family_stage_key = %s", (key,)))
    assert sizes == {A: 10, B: 10, C: 6, D: 3, E: 4, F: 1, G: 1}


def test_ac04_threshold_change_recomputes_family_only(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac04_world())
    h.run(conn, eid, "review_family")
    strict = DEFAULT_RULES.with_version("t8x07", family_jaccard=0.7)
    out = h.run(conn, eid, "review_family", strict)
    assert out["review_clean"].get("skipped") == "complete"            # 清洗阶段键不含家族阈值，原样复用
    assert out["review_family"]["family_count"] == 5
    assert set(families(conn, eid)) == {(A, B), (C,), (D,), (E,), (F, G)}
    keys = conn.execute("SELECT stage, count(*) FROM task8.stage_key WHERE extraction_id = %s GROUP BY 1 ORDER BY 1",
                        (eid,)).fetchall()
    assert keys == [("review_clean", 1), ("review_family", 2)]


def test_family_partitions_resume_and_components_atomic(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac04_world())
    h.run(conn, eid, "review_family", partitions=2)
    once = families(conn, eid)
    h.reset(conn)

    def fail(at: int):
        def hook(stage, batch_no):
            if stage == "review_family" and batch_no == at:
                raise RuntimeError("注入故障")
        return hook

    with pytest.raises(RuntimeError):                  # 签名第 2 个分区提交前失败
        h.run(conn, eid, "review_family", partitions=2, before_commit=fail(2))
    key = h.stage_key(conn, eid, "review_family")
    assert conn.execute("SELECT array_agg(entity_id ORDER BY entity_id) FROM task8.entity_stage WHERE stage_key = %s",
                        (key,)).fetchone()[0] == ["sig0/2"]
    with pytest.raises(RuntimeError):                  # 本次运行的第 4 批即连通分量，提交前失败：家族表没有半写入
        h.run(conn, eid, "review_family", partitions=2, before_commit=fail(4))
    assert conn.execute("SELECT count(*) FROM task8.review_family WHERE family_stage_key = %s", (key,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task8.family_member WHERE family_stage_key = %s", (key,)).fetchone()[0] == 0
    with pytest.raises(Exception, match="分区数"):     # 已按 2 个分区提交过部分单元，换分区数确定性失败
        h.run(conn, eid, "review_family", partitions=3)
    h.run(conn, eid, "review_family", partitions=2)
    assert families(conn, eid) == once
