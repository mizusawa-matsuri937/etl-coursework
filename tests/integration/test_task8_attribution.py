"""任务 8 归属与统计 review_attribution 与版本物化（spec AC07、AC08；AC01 的只读指定 cv、AC06 的 X4）。

上游表：任务 1 由装载器从合成 gzip 生成；任务 2 清单、任务 3 分类与任务 7 版本由 _task8_helpers.seed_upstream 按正式库
同形写入（tests/fixtures/task8/README.md）。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_attribution.py
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import _task8_helpers as h
from _task8_helpers import Product, mid
from etl_task8 import check
from etl_task8.rules import stat_group_id
from task8.world import Rec, copies, unix

pytestmark = pytest.mark.integration

M1, M2, M3, M4, M5, M9 = (mid(f"M{i}") for i in (1, 2, 3, 4, 5, 9))
W20 = ("this long film was one wonderful journey through history with great acting beautiful sets and moving music "
       "plus clever writing").split()      # 20 个不同的词：去掉最后一个后词集 Jaccard = 19/20
assert len(set(W20)) == 20
UPSTREAM = ("task4.mapping_version", "task7.conflict_version", "task7.edition_resolved", "task7.shared_tconst_result",
            "task3.product_classification", "task2.product_manifest")


def upstream_digest(conn) -> dict[str, tuple]:
    return {t: conn.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {t} x"
                            ).fetchone() for t in UPSTREAM}


def lids(conn, rd: str, user: str) -> list[str]:
    dk = h.version(conn, rd)["dedup_stage_key"]
    return [r[0] for r in conn.execute("SELECT logical_review_id FROM task8.logical_review WHERE dedup_stage_key = %s "
                                       "AND reviewer_id = %s ORDER BY 1", (dk, user))]


def work(conn, rd: str) -> dict[tuple[str, str], tuple]:
    ak = h.version(conn, rd)["attribution_stage_key"]
    return {(r[0], r[1]): (list(r[2]), r[3]) for r in conn.execute(
        "SELECT logical_review_id, movie_id, via_asins, also_on_collection FROM task8.logical_review_work "
        "WHERE attribution_stage_key = %s", (ak,))}


def movie_stats(conn, rd: str) -> dict[str, tuple]:
    return {r[0]: (r[1], r[2], r[3], _f(r[4]), r[5], r[6], _f(r[7]), r[8], r[9]) for r in conn.execute(
        "SELECT movie_id, stat_group_id, raw_records, raw_rated, raw_avg_score, logical_reviews, logical_rated, "
        "logical_avg_score, shared_with_group, near_dup_candidates FROM task8.movie_review_stats "
        "WHERE review_dedup_version_id = %s", (rd,))}


def group_stats(conn, rd: str) -> dict[str, tuple]:
    return {r[0]: (r[1], r[2], r[3], _f(r[4]), r[5], r[6], _f(r[7]), r[8], r[9], _f(r[10])) for r in conn.execute(
        "SELECT stat_group_id, movie_count, raw_records, raw_rated, raw_avg_score, logical_reviews, logical_rated, "
        "logical_avg_score, near_dup_pairs, sensitivity_rated, sensitivity_avg_score FROM task8.stat_group_review_stats "
        "WHERE review_dedup_version_id = %s", (rd,))}


def _f(x: Decimal | None) -> float | None:
    return None if x is None else round(float(x), 4)


# ---------------------------------------------------------------- AC07

A1, A2, C1, T1, N1 = "B00000A7A1", "B00000A7A2", "B00000A7C1", "B00000A7T1", "B00000A7N1"
AC07_PRODUCTS = [Product(A1, "movie", (M1,)), Product(A2, "movie", (M1,)), Product(C1, "collection", (M1, M2)),
                 Product(T1, "tv_series"), Product(N1, "not_collected")]


def ac07_world() -> list[Rec]:
    recs = []
    for i in range(4):          # 三个商品共有的评论：使 A1、A2、C1 在同一评论家族（Jaccard 5/6、5/7、4/7）
        recs += copies([A1, A2, C1], f"F{i}", text=f"filler {i}")
    recs += copies([A1, A2], "L1", text="L1 on both singles")
    recs += [Rec(C1, "L2", text="L2 only on the collection")]
    recs += copies([C1, A1], "L3", text="L3 on collection and single")
    recs += [Rec(T1, "L4", text="L4 on a tv series"), Rec(N1, "L5", text="L5 on a not collected product")]
    return recs


def test_ac07_movie_attribution_only_via_single_editions(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac07_world())
    h.seed_upstream(conn, eid, AC07_PRODUCTS)
    r = h.execute(conn, eid, partitions=2)
    assert r.status == "succeeded" and r.review_dedup_version_id == "rd0001"
    rd = r.review_dedup_version_id
    (l1,), (l2,), (l3,), (l4,), (l5,) = (lids(conn, rd, u) for u in ("L1", "L2", "L3", "L4", "L5"))
    w = work(conn, rd)
    assert w[(l1, M1)] == ([A1, A2], False)                     # L1 归属 M1 一次（两个单品）
    assert w[(l3, M1)] == ([A1], True)                          # L3 在合集与单品：按单品归属，标 also_on_collection
    assert not [k for k in w if k[0] in (l2, l4, l5)]           # L2 只在合集；L4 剧集；L5 未采集：都不归属作品
    assert not [k for k in w if k[1] == M2]                     # M2 只在合集里：没有归属
    assert len(w) == 6 and {k[1] for k in w} == {M1}           # 4 条填充评论 + L1 + L3
    ms = movie_stats(conn, rd)
    assert ms[M1][4:6] == (6, 6) and ms[M2][4:6] == (0, 0)
    prod = {a: row for a, *row in conn.execute(
        "SELECT asin, product_category, has_movie, raw_valid, logical_reviews FROM task8.product_review_stats "
        "WHERE review_dedup_version_id = %s", (rd,))}
    assert prod == {A1: ["movie", True, 6, 6], A2: ["movie", True, 5, 5], C1: ["collection_with_movies", True, 6, 6],
                    T1: ["tv_series", False, 1, 1], N1: ["not_collected", False, 1, 1]}
    dk = h.version(conn, rd)["dedup_stage_key"]                 # 商品层：L1 在 A1、A2 各算一条
    assert conn.execute("SELECT array_agg(DISTINCT asin ORDER BY asin) FROM task8.review_mapping WHERE dedup_stage_key = %s "
                        "AND logical_review_id = %s", (dk, l1)).fetchone()[0] == [A1, A2]
    info = check.verify(conn, rd)
    assert check.problems(info) == [] and info["work_rows"] == 6


# ---------------------------------------------------------------- AC08 与 AC06 的 X4

S1, S2, S3, S4, S5 = (f"B00000A8S{i}" for i in range(1, 6))
AC08_PRODUCTS = [Product(S1, "movie", (M1,)), Product(S2, "movie", (M2,)), Product(S3, "movie", (M3,)),
                 Product(S4, "movie", (M4,)), Product(S5, "movie", (M5,))]


def ac08_world() -> list[Rec]:
    return [Rec(S1, "U1", score="5.0", text="u1 on s1"), Rec(S1, "U2", score="3.0", text="u2 on s1"),
            Rec(S1, "U7", score="5.0", text=" ".join(W20)),                                   # X4：U7 在 M1
            Rec(S2, "U3", score="4.0", text="u3 on s2"),
            Rec(S2, "U7", score="3.0", text=" ".join(W20[:-1]), time=unix(3)),                # X4：U7 在 M2，Jaccard 0.95
            *copies([S3, S4], "UX", score="5.0", text="shared by M3 and M4"),                # M3、M4 共享一条逻辑评论
            Rec(S3, "U6", score="1.0", text="only on s3"),
            Rec(S5, "U8", score="4.0", text="b"), Rec(S5, "U8", score="4.0", text="b"),      # D1：去重前 3 条、去重后 2 条
            Rec(S5, "U9", score="2.0", text="c")]


def test_ac08_stat_groups_and_rating_rules(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac08_world())
    h.seed_upstream(conn, eid, AC08_PRODUCTS, [(M1, M2)])
    before = upstream_digest(conn)
    rd = h.execute(conn, eid, partitions=2).review_dedup_version_id
    assert upstream_digest(conn) == before                      # 任务 2–7 的表未改
    g12, g34, g5 = stat_group_id(min(M1, M2)), stat_group_id(min(M3, M4)), stat_group_id(M5)
    ak = h.version(conn, rd)["attribution_stage_key"]
    members = dict(conn.execute("SELECT movie_id, stat_group_id FROM task8.stat_group_member "
                                "WHERE attribution_stage_key = %s", (ak,)).fetchall())
    assert members == {M1: g12, M2: g12, M3: g34, M4: g34, M5: g5}
    basis = dict(conn.execute("SELECT stat_group_id, basis FROM task8.stat_group WHERE attribution_stage_key = %s",
                              (ak,)).fetchall())
    assert [e["type"] for e in basis[g12]] == ["likely_duplicate_movie"]
    assert [(e["type"], e["shared"]) for e in basis[g34]] == [("shared_logical_review", 1)]
    # 作品：(组, 去重前记录, 有评分, 平均, 逻辑评论, 有评分, 平均, 共享数, 两端都在本作品的近似对)
    assert movie_stats(conn, rd) == {
        M1: (g12, 3, 3, 4.3333, 3, 3, 4.3333, 0, 0), M2: (g12, 2, 2, 3.5, 2, 2, 3.5, 0, 0),
        M3: (g34, 2, 2, 3.0, 2, 2, 3.0, 1, 0), M4: (g34, 1, 1, 5.0, 1, 1, 5.0, 1, 0),
        M5: (g5, 3, 3, 3.3333, 2, 2, 3.0, 0, 0)}
    # 组：组内共享评论只计一次；{M1,M2} 有一对近似评论，敏感性去掉较晚的一条（评分 3）：(5+3+5+4)/4
    assert group_stats(conn, rd) == {
        g12: (2, 5, 5, 4.0, 5, 5, 4.0, 1, 4, 4.25), g34: (2, 3, 3, 3.6667, 2, 2, 3.0, 0, 2, 3.0),
        g5: (1, 3, 3, 3.3333, 2, 2, 3.0, 0, 2, 3.0)}
    counts = h.version(conn, rd)["counts"]
    assert (counts["stat_groups"], counts["multi_movie_groups"], counts["attributed_logical_reviews"],
            counts["work_rows"]) == (3, 2, 9, 10)               # 9 条逻辑评论，UX 归属 M3 与 M4 两行


def test_ac06_x4_near_duplicate_within_stat_group(conn, make_settings, tmp_path):
    eid = h.setup(conn, make_settings, tmp_path, ac08_world())
    h.seed_upstream(conn, eid, AC08_PRODUCTS, [(M1, M2)])
    rd = h.execute(conn, eid, partitions=2).review_dedup_version_id
    v = h.version(conn, rd)
    u7 = lids(conn, rd, "U7")
    rows = conn.execute("SELECT stage_key, stage, candidate_type, reviewer_id, logical_review_ids, similarity, "
                        "basis->>'scope' FROM task8.dup_candidate WHERE candidate_type = 'near_duplicate'").fetchall()
    assert rows == [(v["attribution_stage_key"], "review_attribution", "near_duplicate", "U7", u7, Decimal("0.9500"),
                     "stat_group")]
    assert len(u7) == 2                                         # 候选不合并：两条逻辑评论、各自映射
    dk = v["dedup_stage_key"]
    assert conn.execute("SELECT count(*) FROM task8.review_mapping WHERE dedup_stage_key = %s AND logical_review_id = ANY(%s) "
                        "AND match_level = 'representative'", (dk, u7)).fetchone()[0] == 2


# ---------------------------------------------------------------- AC01：只读指定 cv 版本

def test_ac01_reads_only_requested_conflict_version(conn, make_settings, tmp_path):
    recs = ac08_world()
    eid = h.setup(conn, make_settings, tmp_path, recs)
    sample = h.load(conn, make_settings, tmp_path, recs, name="sample", sample_max=4)
    h.seed_upstream(conn, eid, AC08_PRODUCTS, [(M1, M2)])
    other = [Product(S1, "movie", (M9,)), Product(S2, "collection", (M2,))] + AC08_PRODUCTS[2:]
    link2 = h.seed_upstream(conn, eid, other, [(M3, M5)], cv="cv0002", base=False)
    rd = h.execute(conn, eid, partitions=2).review_dedup_version_id
    v = conn.execute("SELECT conflict_version_id, t3_rule_version, attribution_stage_key FROM task8.dedup_version "
                     "WHERE review_dedup_version_id = %s", (rd,)).fetchone()
    assert v[:2] == ("cv0003", "t3r2")
    full = conn.execute("SELECT full_key FROM task8.stage_key WHERE stage_key = %s", (v[2],)).fetchone()[0]
    assert "|cv0003|" in full and link2 not in full
    movies = {k[1] for k in work(conn, rd)}
    assert movies == {M1, M2, M3, M4, M5} and M9 not in movies  # cv0002 的映射（S1 → M9）不被读到
    ak = v[2]
    assert conn.execute("SELECT count(*) FROM task8.stat_group_member WHERE attribution_stage_key = %s AND movie_id LIKE "
                        "'mv_decoy%%'", (ak,)).fetchone()[0] == 0  # 其他 stage_key 与其他类别的作品对不被读到
    assert conn.execute("SELECT count(*) FROM task8.stat_group WHERE attribution_stage_key = %s AND movie_count > 1",
                        (ak,)).fetchone()[0] == 2               # {M1,M2}、{M3,M4}，不是 cv0002 的 {M3,M5}
    assert conn.execute("SELECT count(*) FROM task8.stage_key WHERE extraction_id = %s", (sample,)).fetchone()[0] == 0
