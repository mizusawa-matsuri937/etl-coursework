"""任务 4 聚类与映射（spec AC07 一致性与不连带合并、AC08 稳定 movie_id 与人工纠正、AC09 幂等与规则版本差异，AC06 的评论口径）。"""

from __future__ import annotations

import re

import pytest
from task4 import imdb_files as ifx

from _task4_helpers import P, build, inputs_for, load_imdb, match_products, reset_task4
from etl_task4 import review
from etl_task4.cluster import diff_versions, movie_id_for, record_scopes
from etl_task4.match import DEFAULT_RULES
from etl_task4.review import HumanRow, ReviewInputError, import_and_remap, import_decisions
from etl_task4.run import RunInputs
from etl_task4.runner import execute_match

pytestmark = pytest.mark.integration

A = "B0T4M00"
RULES2 = DEFAULT_RULES.with_version("t4r2", auto_threshold=9)


def make_world(conn, make_settings, tmp_path, products, data):
    fx = build(conn, make_settings, tmp_path, products)
    reset_task4(conn)
    return fx, inputs_for(fx, load_imdb(conn, tmp_path, data))


@pytest.fixture
def world(conn, make_settings, tmp_path):
    fx, inputs = make_world(conn, make_settings, tmp_path, match_products(), ifx.MATCH)
    return fx, inputs, execute_match(conn, inputs)


def movie_of(conn, version, asin, rel="edition"):
    rows = conn.execute("SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s AND "
                        "relation_type = %s ORDER BY member_ordinal", (version, asin, rel)).fetchall()
    return [r[0] for r in rows] if rel == "contains" else rows[0][0]


def status(conn, version, asin):
    return conn.execute("SELECT status, reason FROM task4.product_match_status WHERE mapping_version_id = %s AND "
                        "asin = %s", (version, asin)).fetchone()


# ---------------------------------------------------------------- AC07

DUO = [P("B0T4X00A01", "movie", "Duo Film", directors=("X Dir",), actors=("Lead Actor",), runtime="1 hour and 40 minutes"),
       P("B0T4X00B01", "movie", "Duo Film [DVD]", directors=("X Dir",), actors=("Lead Actor",),
         runtime="1 hour and 40 minutes"),
       P("B0T4X00C01", "movie", "Duo Film (Region 2)", directors=("Y Dir",), runtime="1 hour and 40 minutes")]
CHAIN = [P("B0T4X00D01", "movie", "Chain Film", directors=("Aa Aa",), runtime="1 hour and 30 minutes"),
         P("B0T4X00E01", "movie", "Chain Film [VHS]", runtime="1 hour and 30 minutes"),
         P("B0T4X00F01", "movie", "Chain Film (DVD)", directors=("Bb Bb",), runtime="1 hour and 30 minutes")]


def test_consistency_check_moves_the_conflicting_product_out_and_records_it(conn, make_settings, tmp_path):
    fx, inputs = make_world(conn, make_settings, tmp_path, DUO, ifx.CONFLICT)
    r = execute_match(conn, inputs)
    v = r.mapping_version_id
    # 三个商品都自动关联到同一部 IMDb 作品，但 C 与 A、B 的导演完全不重叠：C 被移出
    assert conn.execute("SELECT count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) "
                        "WHERE d.tier = 'auto' AND c.right_id = 'tt0000201'").fetchone()[0] == 3
    a, b, c = (movie_of(conn, v, f"B0T4X00{x}01") for x in "ABC")
    assert a == b == movie_id_for("imdb:tt0000201") and c != a
    assert status(conn, v, "B0T4X00A01") == ("merged", None)
    assert status(conn, v, "B0T4X00C01") == ("review_pending", "cluster_conflict")
    checks = conn.execute("SELECT asin_a, asin_b, conflict, action, resolved_by FROM task4.cluster_check "
                          "WHERE mapping_version_id = %s", (v,)).fetchall()
    assert checks == [("B0T4X00A01", "B0T4X00C01", "director_disjoint", "removed_b", "system")]
    assert conn.execute("SELECT anchor_key FROM task4.movie WHERE mapping_version_id = %s AND movie_id = %s",
                        (v, c)).fetchone()[0] == "asin:B0T4X00C01"


def test_no_chain_merging_review_edges_never_connect_clusters(conn, make_settings, tmp_path):
    fx, inputs = make_world(conn, make_settings, tmp_path, CHAIN, ifx.CONFLICT)
    v = execute_match(conn, inputs).mapping_version_id
    tiers = dict(conn.execute("SELECT c.left_entity || '-' || c.right_id, d.tier FROM task4.merge_decision d "
                              "JOIN task4.candidate c USING (candidate_id) WHERE c.right_kind = 'product'").fetchall())
    assert tiers == {"B0T4X00D01-B0T4X00E01": "review", "B0T4X00E01-B0T4X00F01": "review",
                     "B0T4X00D01-B0T4X00F01": "reject"}                  # D≈E、E≈F 复核，D 与 F 导演冲突
    movies = {movie_of(conn, v, f"B0T4X00{x}01") for x in "DEF"}
    assert len(movies) == 3                                              # 各自独立成作品，没有被连带合并
    assert all(status(conn, v, f"B0T4X00{x}01")[0] == "review_pending" for x in "DEF")
    assert conn.execute("SELECT count(*) FROM task4.cluster_check").fetchone()[0] == 0


# ---------------------------------------------------------------- AC08

def test_stable_id_two_runs_give_the_same_movie_ids_without_raw_ids(conn, world):
    fx, inputs, first = world
    second = execute_match(conn, inputs.__class__(**{**inputs.__dict__, "rule_version": "t4r1b"}),
                           DEFAULT_RULES.with_version("t4r1b"))
    assert second.status == "succeeded" and second.mapping_version_id != first.mapping_version_id
    ids = lambda v: dict(conn.execute("SELECT m.anchor_key, m.movie_id FROM task4.movie m WHERE mapping_version_id = %s",
                                      (v,)).fetchall())
    assert ids(first.mapping_version_id) == ids(second.mapping_version_id) and len(ids(first.mapping_version_id)) >= 15
    for anchor, mid in ids(first.mapping_version_id).items():
        assert re.fullmatch(r"mv_[0-9a-f]{16}", mid) and mid == movie_id_for(anchor)
        raw = anchor.split(":", 1)[1]
        assert raw.lower() not in mid
    pm1 = conn.execute("SELECT asin, member_ordinal, movie_id FROM task4.product_movie WHERE mapping_version_id = %s "
                       "ORDER BY 1, 2, 3", (first.mapping_version_id,)).fetchall()
    pm2 = conn.execute("SELECT asin, member_ordinal, movie_id FROM task4.product_movie WHERE mapping_version_id = %s "
                       "ORDER BY 1, 2, 3", (second.mapping_version_id,)).fetchall()
    assert pm1 == pm2


def test_stable_id_each_movie_product_has_exactly_one_edition_and_other_kinds_none(conn, world):
    fx, inputs, r = world
    v = r.mapping_version_id
    rows = conn.execute("SELECT asin, count(*) FROM task4.product_movie WHERE mapping_version_id = %s AND "
                        "relation_type = 'edition' GROUP BY 1", (v,)).fetchall()
    movies = [p.asin for p in fx.products if p.asin in {q.asin for q in match_products() if q.kind == "movie"}]
    assert sorted(a for a, _ in rows) == sorted(movies) and {n for _, n in rows} == {1}
    assert conn.execute("SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %s AND asin = ANY(%s) "
                        "AND relation_type = 'edition'", (v, [f"{A}K01", f"{A}K02", f"{A}K03"])).fetchone()[0] == 0


def test_stable_id_merged_and_page_only_works_follow_the_anchor_rules(conn, world):
    fx, inputs, r = world
    v = r.mapping_version_id
    casa = movie_id_for("imdb:tt0000101")
    assert {movie_of(conn, v, f"{A}{x}") for x in ("A01", "A02", "A03", "J01")} == {casa}
    row = conn.execute("SELECT status, tconst, display_title, edition_count FROM task4.movie WHERE "
                       "mapping_version_id = %s AND movie_id = %s", (v, casa)).fetchone()
    assert row == ("imdb_linked", "tt0000101", "Casablanca", 4)
    assert movie_of(conn, v, f"{A}F01") == movie_of(conn, v, f"{A}F02") == movie_id_for(f"asin:{A}F01")
    assert movie_of(conn, v, f"{A}E01") != movie_of(conn, v, f"{A}E02")          # 无年份：复核，不合并
    assert status(conn, v, f"{A}E01")[0] == "review_pending"
    assert status(conn, v, f"{A}G01") == ("review_pending", "non_movie_title_type")
    assert status(conn, v, f"{A}H01")[0] == "review_pending"
    assert status(conn, v, f"{A}B02") == ("singleton", None)                      # 关联 IMDb 但只有一个商品
    assert movie_of(conn, v, f"{A}B01") == movie_of(conn, v, f"{A}S01") == movie_id_for("imdb:tt0000102")   # 别名版并入
    assert movie_of(conn, v, f"{A}B01") != movie_of(conn, v, f"{A}B02")           # 1960 与 1983 年版分开


def test_human_cannot_link_and_must_link_create_a_new_version_with_history(conn, world):
    fx, inputs, first = world
    v1 = first.mapping_version_id
    casa, e2_old = movie_of(conn, v1, f"{A}A01"), movie_of(conn, v1, f"{A}E02")
    before = conn.execute("SELECT counts FROM task4.mapping_version WHERE mapping_version_id = %s", (v1,)).fetchone()[0]
    rows = [HumanRow("cannot_link", f"{A}A01", f"{A}A03", "加长版拆开"), HumanRow("must_link", f"{A}E01", f"{A}E02", "同一部")]
    imported, second = import_and_remap(conn, inputs, rows, annotator="tester", file_sha256="f" * 64)
    v2 = second.mapping_version_id
    assert imported.inserted == 2 and v2 != v1 and second.status == "succeeded"
    # 被拆出的商品得到新的 movie_id，原作品保留原 movie_id
    assert movie_of(conn, v2, f"{A}A03") != casa and movie_of(conn, v2, f"{A}A03") == movie_id_for(f"asin:{A}A03")
    assert {movie_of(conn, v2, f"{A}{x}") for x in ("A01", "A02", "J01")} == {casa}
    assert movie_of(conn, v2, f"{A}E01") == movie_of(conn, v2, f"{A}E02") == movie_of(conn, v1, f"{A}E01")
    ev = dict(conn.execute("SELECT event, count(*) FROM task4.mapping_event WHERE mapping_version_id = %s GROUP BY 1",
                           (v2,)).fetchall())
    assert ev == {"split": 1, "merge": 1, "retire": 1}
    split = conn.execute("SELECT asin, from_movie_id, to_movie_id, reason, human_decision_id FROM task4.mapping_event "
                         "WHERE mapping_version_id = %s AND event = 'split'", (v2,)).fetchone()
    assert split[:3] == (f"{A}A03", casa, movie_of(conn, v2, f"{A}A03")) and split[3] == "human_decision" and split[4]
    merge = conn.execute("SELECT asin, from_movie_id, to_movie_id FROM task4.mapping_event WHERE "
                         "mapping_version_id = %s AND event = 'merge'", (v2,)).fetchone()
    assert merge == (f"{A}E02", e2_old, movie_of(conn, v2, f"{A}E01"))
    retire = conn.execute("SELECT from_movie_id, to_movie_id FROM task4.mapping_event WHERE mapping_version_id = %s "
                          "AND event = 'retire'", (v2,)).fetchone()
    assert retire == (e2_old, movie_of(conn, v2, f"{A}E01"))
    # 前后数量各自正确（由表重新数一遍，对照记录的 counts）
    after = conn.execute("SELECT counts FROM task4.mapping_version WHERE mapping_version_id = %s", (v2,)).fetchone()[0]
    assert after["participating_products"] == before["participating_products"]
    assert after["movies_with_editions"] == before["movies_with_editions"]            # 拆出 1 个、并入后少 1 个
    assert after["multi_product_movies"] == before["multi_product_movies"] + 1
    assert after["review_pending_products"] == before["review_pending_products"] - 2
    for v, c in ((v1, before), (v2, after)):
        assert conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND edition_count > 0",
                            (v,)).fetchone()[0] == c["movies_with_editions"]
        assert conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND edition_count > 1",
                            (v,)).fetchone()[0] == c["multi_product_movies"]
        assert conn.execute("SELECT count(*) FROM task4.product_match_status WHERE mapping_version_id = %s AND "
                            "status = 'review_pending'", (v,)).fetchone()[0] == c["review_pending_products"]
    # 旧映射版本仍可查询，历史里能看到被拆开与并入的商品
    assert movie_of(conn, v1, f"{A}A03") == casa
    assert conn.execute("SELECT parent_version_id FROM task4.mapping_version WHERE mapping_version_id = %s",
                        (v2,)).fetchone()[0] == v1
    basis = dict(conn.execute("SELECT asin, basis FROM task4.product_movie WHERE mapping_version_id = %s AND asin = ANY(%s)",
                              (v2, [f"{A}A03", f"{A}E01", f"{A}E02", f"{A}A01"])).fetchall())
    assert basis == {f"{A}A03": "human", f"{A}E01": "human", f"{A}E02": "human", f"{A}A01": "auto_decision"}
    assert [(d["asin"], d["event"]) for d in diff_versions(conn, v1, v2)] == [(f"{A}A03", "split"), (f"{A}E02", "merge")]


def test_human_detach_moves_one_product_out(conn, world):
    fx, inputs, first = world
    _, second = import_and_remap(conn, inputs, [HumanRow("detach", f"{A}A02", None)], annotator="t", file_sha256="a" * 64)
    v2 = second.mapping_version_id
    assert movie_of(conn, v2, f"{A}A02") == movie_id_for(f"asin:{A}A02")
    assert movie_of(conn, v2, f"{A}A01") == movie_id_for("imdb:tt0000101")


def test_human_must_link_against_a_hard_conflict_is_executed_but_flagged(conn, world):
    fx, inputs, first = world
    _, second = import_and_remap(conn, inputs, [HumanRow("must_link", f"{A}D01", f"{A}D02")], annotator="t",
                                 file_sha256="b" * 64)       # Rocky 与 Rocky II：导演不同，系统不会合并
    v2 = second.mapping_version_id
    assert movie_of(conn, v2, f"{A}D01") == movie_of(conn, v2, f"{A}D02")
    assert conn.execute("SELECT conflict, action, resolved_by FROM task4.cluster_check WHERE mapping_version_id = %s",
                        (v2,)).fetchall() == [("director_disjoint", "kept_by_human", "human")]
    assert conn.execute("SELECT count(*) FROM task4.quality_issue WHERE issue_code = 'human_vs_veto'").fetchone()[0] == 1
    assert status(conn, v2, f"{A}D01") == ("merged", None)


def test_human_import_with_unknown_asin_or_bad_rows_fails_without_partial_rows(conn, world):
    fx, inputs, first = world
    for rows in ([HumanRow("must_link", f"{A}E01", f"{A}E02"), HumanRow("must_link", f"{A}E01", "NOPE000000")],
                 [HumanRow("must_link", f"{A}E01", f"{A}K01")],            # 合集不是电影类商品
                 [HumanRow("merge", f"{A}E01", f"{A}E02")],
                 [HumanRow("detach", f"{A}E01", f"{A}E02")],
                 [HumanRow("must_link", f"{A}E01", None)]):
        with pytest.raises(ReviewInputError):
            import_decisions(conn, inputs, rows, annotator="t", file_sha256="c" * 64)
    assert conn.execute("SELECT count(*) FROM task4.human_decision").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task4.run WHERE kind = 'human_import'").fetchone()[0] == 0


def test_human_csv_roundtrip_and_repeated_import_is_a_noop(conn, world, tmp_path):
    fx, inputs, first = world
    path = tmp_path / "human.csv"
    path.write_text("kind,left,right,note,annotator\n"
                    f"must_link,{A}E01,{A}E02,同一部,张三\ndetach,{A}A02,,拆出,张三\n", encoding="utf-8")
    rows, sha = review.read_csv(path)
    assert [r.kind for r in rows] == ["must_link", "detach"] and rows[1].right is None and len(sha) == 64
    imp1, r1 = import_and_remap(conn, inputs, rows, annotator="t", file_sha256=sha)
    imp2, r2 = import_and_remap(conn, inputs, rows, annotator="t", file_sha256=sha)
    assert (imp1.inserted, imp2.inserted, imp2.duplicates) == (2, 0, 2) and imp1.set_hash == imp2.set_hash
    assert r2.status == "reused" and r2.mapping_version_id == r1.mapping_version_id
    assert conn.execute("SELECT DISTINCT annotator FROM task4.human_decision").fetchall() == [("张三",)]


# ---------------------------------------------------------------- AC09

DATA_TABLES = ("field_assertion", "source_record", "imdb_title_key", "candidate", "merge_decision", "entity_stage",
               "movie_identity", "mapping_version", "movie", "product_movie", "product_match_status", "mapping_event",
               "cluster_check", "quality_issue", "run_batch", "product_edition")


def counts(conn):
    return {t: conn.execute(f"SELECT count(*) FROM task4.{t}").fetchone()[0] for t in DATA_TABLES}


def test_reused_second_run_adds_nothing_but_an_audit_row(conn, world):
    fx, inputs, first = world
    before = counts(conn)
    second = execute_match(conn, inputs)
    assert second.status == "reused" and second.mapping_version_id == first.mapping_version_id
    assert counts(conn) == before
    assert conn.execute("SELECT status, mapping_version_id FROM task4.run WHERE run_id = %s",
                        (second.run_id,)).fetchone() == ("reused", first.mapping_version_id)
    assert conn.execute("SELECT count(*) FROM task4.run WHERE kind = 'match'").fetchone()[0] == 2


def test_rule_version_diff_lists_exactly_the_products_the_threshold_affects(conn, world):
    fx, inputs, first = world
    before = counts(conn)
    second = execute_match(conn, RunInputs(**{**inputs.__dict__, "rule_version": "t4r2"}), RULES2)
    v1, v2 = first.mapping_version_id, second.mapping_version_id
    after = counts(conn)
    # 不重新标准化，也不重新生成候选；只新增判定以后的行
    for t in ("field_assertion", "source_record", "imdb_title_key", "candidate", "product_edition"):
        assert after[t] == before[t], t
    assert after["merge_decision"] == 2 * before["merge_decision"] and after["mapping_version"] == 2
    # 手算受影响的商品：t4r1 下自动关联、得分低于新阈值 9 的商品（R25 的页面簇边不用自动阈值，F 对不受影响）
    lost = {r[0] for r in conn.execute(
        "SELECT c.left_entity FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) "
        "WHERE d.rule_version = 't4r1' AND d.tier = 'auto' AND c.right_kind = 'imdb' AND d.score < 9 "
        "AND c.left_entity NOT LIKE '%#%'").fetchall()}
    assert {f"{A}A03", f"{A}J01", f"{A}D01", f"{A}D02"} <= lost and f"{A}A01" not in lost
    changed = diff_versions(conn, v1, v2)
    assert {d["asin"] for d in changed} == lost
    assert all(d["reason"] == "auto_decision_changed" and d["event"] for d in changed)
    lost_row = next(d for d in changed if d["asin"] == f"{A}A03")
    assert (lost_row["old_tier"], lost_row["new_tier"]) == ("auto", "review") and lost_row["old_score"] == 8
    # 锚点没变的作品 movie_id 不变；没有受影响的商品保持原 movie_id
    assert movie_of(conn, v2, f"{A}A01") == movie_of(conn, v1, f"{A}A01") == movie_id_for("imdb:tt0000101")
    unchanged = [p.asin for p in match_products() if p.kind == "movie"
                 and p.asin not in {d["asin"] for d in changed}]
    assert unchanged and all(movie_of(conn, v1, a) == movie_of(conn, v2, a) for a in unchanged)
    assert conn.execute("SELECT parent_version_id FROM task4.mapping_version WHERE mapping_version_id = %s",
                        (v2,)).fetchone()[0] == v1
    assert conn.execute("SELECT count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) "
                        "WHERE d.rule_version = 't4r2' AND d.tier = 'auto' AND c.right_kind = 'imdb' AND d.score < 9").fetchone()[0] == 0


def test_run_resumes_after_a_failed_batch_and_matches_a_clean_run(conn, make_settings, tmp_path):
    fx, inputs = make_world(conn, make_settings, tmp_path, match_products(), ifx.MATCH)

    def boom(stage, batch_no):
        if stage == "decide" and batch_no == 2:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        execute_match(conn, inputs, batch_size=5, before_commit=boom)
    assert conn.execute("SELECT status FROM task4.run ORDER BY started_at").fetchall() == [("failed",)]
    assert conn.execute("SELECT count(*) FROM task4.mapping_version").fetchone()[0] == 0
    ok = execute_match(conn, inputs, batch_size=5)
    assert ok.status == "succeeded"
    resumed = counts(conn)
    assert conn.execute("SELECT count(*) FROM task4.merge_decision").fetchone()[0] == resumed["candidate"]
    from etl_task4 import candidates
    from etl_task4.run import RunContext

    assert candidates.count_pending_entities(conn, RunContext(ok.run_id, ok.inputs)) == 0
    assert conn.execute("SELECT count(*) FROM task4.candidate c WHERE c.right_kind = 'imdb' AND NOT EXISTS "
                        "(SELECT 1 FROM task4.entity_stage s WHERE s.stage = 'decide' AND s.entity_id = c.left_entity)"
                        ).fetchone()[0] == 0


# ---------------------------------------------------------------- AC06：合集关系与评论口径

def test_collection_contains_relations_many_to_many_and_status(conn, world):
    fx, inputs, r = world
    v = r.mapping_version_id
    alien, aliens = movie_id_for("imdb:tt0000110"), movie_id_for("imdb:tt0000111")
    assert movie_of(conn, v, f"{A}K01", "contains") == sorted([alien, aliens], key=lambda m: m) or \
        set(movie_of(conn, v, f"{A}K01", "contains")) == {alien, aliens}
    assert set(movie_of(conn, v, f"{A}K03", "contains")) == {alien, aliens}
    assert conn.execute("SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s",
                        (v, f"{A}K02")).fetchone()[0] == 0
    assert status(conn, v, f"{A}K01") == ("collection_split", "2/2 members linked")
    assert status(conn, v, f"{A}K03") == ("collection_split", "2/4 members linked")
    assert status(conn, v, f"{A}K02") == ("collection_unsplit", "no_members")
    # 同一部电影出现在多个合集里；没有 edition 商品的作品 edition_count 为 0
    row = conn.execute("SELECT status, edition_count, contains_count, display_title FROM task4.movie WHERE "
                       "mapping_version_id = %s AND movie_id = %s", (v, alien)).fetchone()
    assert row == ("imdb_linked", 0, 2, "Alien")
    ordinals = conn.execute("SELECT asin, member_ordinal FROM task4.product_movie WHERE mapping_version_id = %s AND "
                            "relation_type = 'contains' ORDER BY 1, 2", (v,)).fetchall()
    assert ordinals == [(f"{A}K01", 1), (f"{A}K01", 2), (f"{A}K03", 1), (f"{A}K03", 2)]
    assert conn.execute("SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %s AND "
                        "relation_type = 'contains' AND decision_id IS NULL", (v,)).fetchone()[0] == 0
    unmatched = {r[0] for r in conn.execute("SELECT entity_id FROM task4.quality_issue WHERE issue_code = "
                                            "'collection_member_unmatched'").fetchall()}
    assert unmatched == {f"{A}K03#3", f"{A}K03#4"}


def test_collection_review_counts_come_only_from_edition_relations_and_add_up(conn, world):
    fx, inputs, r = world
    products = match_products()
    s = record_scopes(conn, r.mapping_version_id)
    assert s["edition"] == sum(p.reviews for p in products if p.kind == "movie")
    assert s["collection"] == sum(p.reviews for p in products if p.kind == "collection") == 11
    assert s["other_video"] == s["no_page"] == 0
    assert s["manifest_total"] == s["edition"] + s["collection"] + s["other_video"] + s["no_page"]
    # 评论记录总数 = 清单记录合计 + 没有产品标识的记录
    total = conn.execute("SELECT count(*) FROM public.review_record WHERE extraction_id = %s", (fx.eid,)).fetchone()[0]
    assert total == s["manifest_total"] + conn.execute(
        "SELECT count(*) FROM public.review_record WHERE extraction_id = %s AND product_id_observed IS NULL",
        (fx.eid,)).fetchone()[0]
    # 按作品统计只经 edition 关系：合集评论不出现在任何作品的评论数里
    per_movie = conn.execute(
        "SELECT sum(m.records_total) FROM task4.product_movie pm JOIN task2.product_manifest m ON m.asin = pm.asin "
        "AND m.extraction_id = %s WHERE pm.mapping_version_id = %s AND pm.relation_type = 'edition'",
        (fx.eid, r.mapping_version_id)).fetchone()[0]
    assert int(per_movie) == s["edition"]


def test_collection_scopes_include_tv_series_and_pages_without_content(conn, make_settings, tmp_path):
    from _task4_helpers import standard_products

    fx = build(conn, make_settings, tmp_path, standard_products())
    reset_task4(conn)
    inputs = inputs_for(fx, load_imdb(conn, tmp_path, ifx.BASIC))
    r = execute_match(conn, inputs)
    s = record_scopes(conn, r.mapping_version_id)
    reviews = {p.asin: p.reviews for p in standard_products()}
    assert s["edition"] == 3 + 2 + 1 + 1 and s["collection"] == 4 + 5
    assert s["other_video"] == 6 + 1 + 1 + 1 and s["no_page"] == 7 + 1
    assert s["manifest_total"] == sum(reviews.values())
    assert conn.execute("SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %s AND asin = ANY(%s)",
                        (r.mapping_version_id, ["B0T4000007", "B0T4000011"])).fetchone()[0] == 0
