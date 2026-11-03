"""任务 8 去重与目标层 review_dedup（spec AC01、AC03、AC05；AC06 的家族内部分；分区与恢复）。

上游表由任务 1 的装载器从合成 gzip 生成（tests/fixtures/task8/README.md）。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import _task8_helpers as h
from etl_task8 import dedup
from etl_task8.rules import DEFAULT_RULES
from etl_task8.run import RunInputs, extraction_lock, start_run
from task8.world import Rec, copies, unix

pytestmark = pytest.mark.integration

A, B = "B0000000A1", "B0000000B1"
P1, P2, P3 = "B0000000P1", "B0000000P2", "B0000000P3"
Q1, Q2 = "B0000000Q1", "B0000000Q2"
S1, S2 = "B0000000S1", "B0000000S2"
WORDS = "the film was a wonderful journey through history with great acting and beautiful sets".split()


def mapping(conn, eid: str) -> dict[int, tuple]:
    key = h.stage_key(conn, eid, "review_dedup")
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT record_no, asin, logical_review_id, mapping_status, match_level, diff_fields FROM task8.review_mapping "
        "WHERE dedup_stage_key = %s ORDER BY record_no", (key,))}


def logical(conn, eid: str) -> dict[str, tuple]:
    key = h.stage_key(conn, eid, "review_dedup")
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT logical_review_id, reviewer_id, representative_record_no, copy_count, asin_count, helpful_votes, "
        "total_votes, helpfulness_record_no, family_id, collision_variant FROM task8.logical_review "
        "WHERE dedup_stage_key = %s", (key,))}


def candidates(conn, eid: str) -> list[tuple]:
    key = h.stage_key(conn, eid, "review_dedup")
    return conn.execute("SELECT candidate_type, reviewer_id, logical_review_ids, similarity FROM task8.dup_candidate "
                        "WHERE stage_key = %s ORDER BY candidate_type, reviewer_id", (key,)).fetchall()


def digest(conn, key: str) -> dict[str, list]:
    """去重结果的内容（不含分区号与运行 ID）：分区数、写入批大小、中断重跑都不应改变它。"""
    q = lambda sql: conn.execute(sql, (key,)).fetchall()      # noqa: E731
    return {
        "mapping": q("SELECT record_no, source_review_id, asin, logical_review_id, mapping_status, match_level, diff_fields "
                     "FROM task8.review_mapping WHERE dedup_stage_key = %s ORDER BY record_no"),
        "logical": q("SELECT logical_review_id, reviewer_id, review_day, score, content_hash, text_md5, family_id, "
                     "collision_variant, representative_record_no, copy_count, asin_count, helpful_votes, total_votes, "
                     "helpfulness_record_no FROM task8.logical_review WHERE dedup_stage_key = %s ORDER BY 1"),
        "reviewer": q("SELECT reviewer_id, profile_names, profile_variant_count, record_count, logical_count, first_day, "
                      "last_day FROM task8.reviewer WHERE dedup_stage_key = %s ORDER BY 1"),
        "candidate": q("SELECT candidate_id, candidate_type, reviewer_id, logical_review_ids, similarity, basis "
                       "FROM task8.dup_candidate WHERE stage_key = %s ORDER BY 1"),
        "issue": q("SELECT issue_key, record_no, code, evidence FROM task8.review_issue WHERE stage_key = %s ORDER BY 1"),
    }


# ---------------------------------------------------------------- AC01

def test_ac01_every_record_mapped_once(conn, make_settings, tmp_path):
    recs = [Rec(A, "U1"), Rec(A, "U1"), Rec(A, "U2", raw_text=b"bad \xff bytes"), Rec(B, "U3", extra=(b"garbage line",)),
            Rec(B, "U4"), Rec(B, "U5", helpfulness=None, score=None, time=None, summary=None, text=None),
            Rec(None, "U6", extra=(b"5b4",))]
    eid = h.setup(conn, make_settings, tmp_path, recs)
    sample = h.load(conn, make_settings, tmp_path, recs, name="sample", sample_max=3)
    before = h.public_digest(conn)
    out = h.run(conn, eid, "review_dedup", partitions=2)
    assert h.public_digest(conn) == before
    m = mapping(conn, eid)
    assert sorted(m) == list(range(1, 8))                       # 每条原始记录恰好一行
    assert {n: (r[2], r[3]) for n, r in m.items()} == {
        1: ("mapped", "representative"), 2: ("mapped", "D1_exact_same_asin"), 3: ("quarantined", None),
        4: ("quarantined", None), 5: ("mapped", "representative"), 6: ("incomplete", None), 7: ("quarantined", None)}
    assert all(r[1] is None for r in m.values() if r[2] != "mapped")
    assert m[7][0] is None and m[3][0] == A                    # 无产品 ID 的隔离记录 asin 为空
    srids = set(h.record_nos(conn, eid).values())
    lids = {r[1] for r in m.values() if r[1]}
    assert len(lids) == 2 and all(l.startswith("lr_") and l not in srids for l in lids)
    assert (out["review_dedup"]["quarantined"], out["review_dedup"]["incomplete"],
            out["review_dedup"]["logical_reviews"]) == (3, 1, 2)
    assert conn.execute("SELECT count(*) FROM task8.stage_key WHERE extraction_id = %s", (sample,)).fetchone()[0] == 0
    issues = conn.execute("SELECT record_no, code FROM task8.review_issue ORDER BY 1, 2").fetchall()
    assert issues == [(6, "incomplete_review"), (6, "missing_helpfulness")]


# ---------------------------------------------------------------- AC03

def test_ac03_raw_values_only_collision_and_reviewer(conn, make_settings, tmp_path):
    recs = [Rec(A, "U1", text="Great movie"), Rec(A, "U1", text="great movie"),                     # K1
            Rec(A, "U2", text="Fine film"), Rec(A, "U2", text="Fine film "),                        # K2
            Rec(A, "U3", text="Same text", summary="KHAN!!!"), Rec(A, "U3", text="Same text", summary="Star Trek IV"),
            Rec(A, "U4", text="Collision one"), Rec(A, "U4", text="Collision two"),                 # K4
            Rec(B, "U5", profile="Ann", text="First"), Rec(B, "U5", profile="Ann", text="Second", time=unix(1)),
            Rec(B, "U5", profile='Ann "A"', text="Third", time=unix(2)), Rec(B, "U6", profile="Ann", text="First")]
    eid = h.setup(conn, make_settings, tmp_path, recs)
    texts = dict(conn.execute("SELECT r.record_no, s.review_text_raw FROM public.review_record r JOIN public.review_staging s "
                              "USING (extraction_id, source_review_id) WHERE r.extraction_id = %s", (eid,)).fetchall())
    assert texts[4] == "Fine film "                            # 任务 1 保留末尾空格
    h.run(conn, eid, "review_clean")
    ck = h.stage_key(conn, eid, "review_clean")                # K4：注入哈希碰撞（两条不同正文得到相同分组哈希）
    conn.execute("UPDATE task8.review_clean SET content_hash = (SELECT content_hash FROM task8.review_clean "
                 "WHERE clean_stage_key = %s AND record_no = 7) WHERE clean_stage_key = %s AND record_no = 8", (ck, ck))
    out = h.run(conn, eid, "review_dedup", partitions=2)
    m = mapping(conn, eid)
    assert all(r[3] == "representative" for r in m.values())   # K1–K4 都不合并
    assert len({r[1] for r in m.values()}) == 12
    lg = logical(conn, eid)
    assert sorted(v[8] for v in lg.values() if v[0] == "U4") == [0, 1]
    assert conn.execute("SELECT record_no, code FROM task8.review_issue WHERE code = 'hash_collision' ORDER BY 1"
                        ).fetchall() == [(7, "hash_collision"), (8, "hash_collision")]
    assert (out["review_dedup"]["collision_groups"], out["review_dedup"]["collision_records"]) == (1, 2)
    dk = h.stage_key(conn, eid, "review_dedup")
    rv = {r[0]: r[1:] for r in conn.execute("SELECT reviewer_id, profile_names, profile_variant_count, record_count, "
                                            "logical_count FROM task8.reviewer WHERE dedup_stage_key = %s", (dk,))}
    assert rv["U5"] == ({"Ann": 2, 'Ann "A"': 1}, 2, 3, 3)
    assert rv["U6"] == ({"Ann": 1}, 1, 1, 1)                  # 同名的另一用户单独一行


# ---------------------------------------------------------------- AC05

def ac05_world() -> list[Rec]:
    recs = [Rec(A, "U1", helpfulness="2/3"), Rec(A, "U1", helpfulness="2/3"),                       # D1
            Rec(A, "U2", helpfulness="1/2", text="Votes differ"), Rec(A, "U2", helpfulness="3/4", text="Votes differ")]
    for i in range(3):                                                                              # 同一家族 3 个 ASIN
        recs += copies([P1, P2, P3], f"U3{i}", text=f"Family copy {i}", time=unix(i))
    recs += [Rec(Q1, f"UQ1{i}", text=f"Q1 only {i}") for i in range(3)]
    recs += [Rec(Q2, f"UQ2{i}", text=f"Q2 only {i}") for i in range(3)]
    recs += copies([Q1, Q2], "UX", text="Order for a friend")                                        # 跨家族同内容
    return recs


def test_ac05_levels_representative_and_stable_ids(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac05_world())
    out = h.run(conn, eid, "review_dedup", partitions=3)
    m = mapping(conn, eid)
    assert [(n, m[n][3], m[n][4]) for n in (1, 2, 3, 4)] == [
        (1, "representative", []), (2, "D1_exact_same_asin", []), (3, "representative", []),
        (4, "D2_same_asin_variant", ["helpfulness"])]
    lg = logical(conn, eid)
    assert lg[m[3][1]][4:7] == (3, 4, 4)                         # D2 的逻辑评论 helpfulness 取 3/4（第 4 条）
    assert lg[m[1][1]][1:4] == (1, 2, 1)                         # 代表记录为最小 record_no；2 个副本、1 个 ASIN
    fam = [n for n in m if m[n][0] in (P1, P2, P3)]
    assert [m[n][3] for n in fam] == ["representative", "D3_family_copy", "D3_family_copy"] * 3
    assert {lg[m[n][1]][1:4] for n in fam} == {(fam[0], 3, 3), (fam[3], 3, 3), (fam[6], 3, 3)}
    ux = [n for n in m if m[n][1] and lg[m[n][1]][0] == "UX"]
    assert len({m[n][1] for n in ux}) == 2 and all(m[n][3] == "representative" for n in ux)   # 跨家族是两条逻辑评论
    levels = out["review_dedup"]["match_level"]
    dup = levels["D1_exact_same_asin"] + levels["D2_same_asin_variant"] + levels["D3_family_copy"]
    assert (levels["representative"], dup) == (13, 8) == (len(lg), len(m) - len(lg))
    first = {n: r[1] for n, r in m.items()}
    again = h.run(conn, eid, "review_dedup", partitions=3)
    assert again["review_dedup"].get("skipped") == "complete"
    h.reset(conn)
    h.run(conn, eid, "review_dedup", partitions=5)
    assert {n: r[1] for n, r in mapping(conn, eid).items()} == first      # 重跑（换分区数）logical_review_id 不变


# ---------------------------------------------------------------- AC06（家族内部分；统计组内的 X4 见 test_task8_attribution）

def ac06_world() -> list[Rec]:
    recs = [Rec(Q1, f"UQ1{i}", text=f"Q1 only {i}") for i in range(3)]
    recs += [Rec(Q2, f"UQ2{i}", text=f"Q2 only {i}") for i in range(3)]
    recs += copies([Q1, Q2], "X1", text="We love it!")                                            # X1
    recs += [Rec(S1, "X2", text=" ".join(WORDS)), Rec(S1, "X2", text=" ".join(WORDS[:-1]) + "!", time=unix(5))]  # X2 0.93
    recs += [Rec(S2, "X3", text=" ".join(WORDS[:6] + ["x1", "x2"])),
             Rec(S2, "X3", text=" ".join(WORDS[:6] + ["y1", "y2"]), time=unix(5))]                  # X3 0.6
    return recs


def test_ac06_candidates_do_not_change_mapping(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac06_world())
    h.run(conn, eid, "review_dedup", partitions=2)
    m = mapping(conn, eid)
    lg = logical(conn, eid)
    by_user = lambda u: sorted(l for l, v in lg.items() if v[0] == u)      # noqa: E731
    assert candidates(conn, eid) == [
        ("cross_family_same_content", "X1", by_user("X1"), None),
        ("near_duplicate", "X2", by_user("X2"), Decimal("0.9286"))]
    assert len(by_user("X1")) == len(by_user("X2")) == len(by_user("X3")) == 2
    # 候选不改变逻辑评论与映射：关掉近似阈值（新规则版本，新的 review_dedup 阶段键）后映射完全相同
    off = DEFAULT_RULES.with_version("t8x_off", near_dup_jaccard=1.01)
    h.run(conn, eid, "review_dedup", off, partitions=2)
    off_key = h.stage_key(conn, eid, "review_dedup")
    assert conn.execute("SELECT count(*) FROM task8.dup_candidate WHERE stage_key = %s AND candidate_type = 'near_duplicate'",
                        (off_key,)).fetchone()[0] == 0
    assert mapping(conn, eid) == m


# ---------------------------------------------------------------- 分区、写入批与恢复

def test_dedup_partitions_flush_and_resume(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac05_world() + ac06_world()[6:])
    h.run(conn, eid, "review_dedup", partitions=4)
    expected = digest(conn, h.stage_key(conn, eid, "review_dedup"))
    assert len(expected["candidate"]) == 3                      # UX、X1 两组跨家族同内容，X2 一对近似

    h.reset(conn)                                               # 1 个分区、每 2 行一次 COPY（流式游标打开期间写入）
    h.run(conn, eid, "review_family")
    inputs = RunInputs(extraction_id=eid)
    with extraction_lock(conn, eid):
        ctx = start_run(conn, inputs, DEFAULT_RULES, "stage")
        dedup.dedup_reviews(conn, ctx, partitions=1, flush_rows=2)
    assert digest(conn, h.stage_key(conn, eid, "review_dedup")) == expected

    h.reset(conn)

    def fail(stage, batch_no):
        if stage == "review_dedup" and batch_no == 4:            # unmapped、verify、p0 之后，p1 提交前
            raise RuntimeError("注入故障")

    with pytest.raises(RuntimeError):
        h.run(conn, eid, "review_dedup", partitions=4, before_commit=fail)
    dk = h.stage_key(conn, eid, "review_dedup")
    assert conn.execute("SELECT array_agg(entity_id ORDER BY entity_id) FROM task8.entity_stage WHERE stage_key = %s",
                        (dk,)).fetchone()[0] == ["p0/4", "unmapped", "verify"]
    assert {r[0] for r in conn.execute("SELECT DISTINCT partition_no FROM task8.review_mapping WHERE dedup_stage_key = %s",
                                       (dk,))} <= {-1, 0}           # 只有已提交单元的映射落库
    h.run(conn, eid, "review_dedup", partitions=4)
    assert digest(conn, dk) == expected
