"""任务 4 报告导出、实例与只读核对（spec AC12、AC14 的程序部分）。"""

from __future__ import annotations

import csv
import hashlib
import json

import pytest
from task4 import imdb_files as ifx

from _task4_helpers import P, build, load_imdb, match_products, reset_task4
from etl_task4 import check, pipeline, report
from etl_task4.pipeline import MatchParams
from etl_task4.report import examples, run_dir

pytestmark = pytest.mark.integration

A = "B0T4M00"


def products():
    """AC05、AC06 的商品，再加一个剧集、一个无页面商品（不同评论数），让各口径都有数。"""
    return match_products() + [P("B0T4M00T01", "tv_series", "Some Series Season 1", reviews=6),
                               P("B0T4M00N01", "nopage", group="not_found", reviews=3)]


@pytest.fixture(autouse=True)
def out_dir(monkeypatch, tmp_path):
    d = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(d))
    return d


@pytest.fixture
def world(conn, make_settings, tmp_path, out_dir):
    fx = build(conn, make_settings, tmp_path, products())
    reset_task4(conn)
    sid = load_imdb(conn, tmp_path, ifx.MATCH)
    p = MatchParams(run_key="rep", extraction_id=fx.eid, imdb_snapshot_id=sid)
    checked = pipeline.check_inputs_step(p)
    up = {"check_inputs": checked, "normalize": pipeline.normalize_step(p, checked),
          "candidates_decide": pipeline.candidates_decide_step(p, checked),
          "cluster_map": pipeline.cluster_map_step(p, checked)}
    up["verify"] = pipeline.match_verify_step(p, up["cluster_map"])
    final = pipeline.finalize_step(p, up)
    assert final["final_status"] == "succeeded"
    version = up["cluster_map"]["mapping_version_id"]
    return fx, sid, version, run_dir(out_dir, fx.eid, version, "rep")


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


def test_summary_json_agrees_with_the_database(conn, world):
    fx, sid, v, d = world
    s = json.loads((d / "summary.json").read_text())["stats"]
    pe = s["product_editions"]
    assert pe["total"] == one(conn, "SELECT count(*) FROM task4.product_edition") == len(products())
    assert pe["match_scope"] == dict(conn.execute("SELECT match_scope, count(*) FROM task4.product_edition "
                                                  "GROUP BY 1 ORDER BY 1").fetchall())
    assert pe["match_scope"] == {"matched": 21, "no_page": 1, "not_applicable": 1}
    ba = s["before_after"]
    movies_in_db = one(conn, "SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND edition_count > 0", v)
    assert ba["movie_products_before"] == 18 and ba["movies_after"] == movies_in_db
    assert ba["merged_away"] == 18 - movies_in_db
    assert ba["multi_product_movies"] == one(conn, "SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s "
                                                   "AND edition_count > 1", v)
    assert ba["products_in_multi_product_movies"] == one(
        conn, "SELECT coalesce(sum(edition_count), 0) FROM task4.movie WHERE mapping_version_id = %s AND "
              "edition_count > 1", v)
    assert ba["review_pending_products"] == one(
        conn, "SELECT count(*) FROM task4.product_match_status WHERE mapping_version_id = %s AND "
              "status = 'review_pending'", v)
    assert ba["multi_product_size_distribution"] == {"2": 2, "4": 1}       # B01+S01、F01+F02；Casablanca 四个版本
    # 第三方来源与页面来源分开
    so = s["sources"]
    assert so["imdb_linked_movies"] == one(conn, "SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                                 "status = 'imdb_linked'", v)
    assert so["page_only_movies"] == one(conn, "SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                               "status = 'page_only'", v)
    assert so["contains_only_movies"] == 2                                 # Alien、Aliens 只经合集出现
    assert so["products_linked_to_imdb"] + so["products_page_only"] == 18
    assert so["source_kind"]["third_party_imdb"] == so["products_linked_to_imdb"]
    # 判定档
    de = s["decisions"]
    assert de["by_tier"] == dict(conn.execute(
        "SELECT d.tier, count(*) FROM task4.merge_decision d GROUP BY 1 ORDER BY 1").fetchall())
    assert sum(de["by_tier"].values()) == de["candidates"] == one(conn, "SELECT count(*) FROM task4.candidate")
    assert de["hard_conflicts"].get("director_disjoint", 0) >= 1
    assert de["candidate_overflow_entities"] == 0 and de["cluster_conflicts_removed"] == 0
    # 合集成员与剧集
    c, tv = s["collections"], s["tv_series"]
    assert (c["collections"], c["split"], c["unsplit"], c["members"], c["member_links"]) == (3, 2, 1, 6, 4)
    assert c["members_unmatched"] == 2
    assert tv["tv_series_products"] == 1 and tv["tv_series_relations"] == 0
    # 评论口径之和等于清单记录总数，再加无产品标识的记录等于任务 1 读取数
    r = s["record_scopes"]
    reviews = {p.asin: p.reviews for p in products()}
    assert r["edition"] == sum(p.reviews for p in products() if p.kind == "movie")
    assert r["collection"] == 11 and r["other_video"] == 6 and r["no_page"] == 3
    assert r["manifest_total"] == sum(reviews.values()) == r["edition"] + r["collection"] + r["other_video"] + r["no_page"]
    assert r["consistent"] and r["records_without_asin"] == 1
    assert s["unknown_descriptors_top"] is not None and s["imdb_snapshot_id"] == sid


def test_examples_list_cross_edition_merges_and_confusable_non_merges_with_sources(conn, world):
    fx, sid, v, d = world
    ex = json.loads((d / "summary.json").read_text())["examples"]
    merged = {g["display_title"]: g for g in ex["cross_edition_merges"]}
    assert {g["edition_count"] for g in merged.values()} <= {2, 4}
    casa = next(g for g in ex["cross_edition_merges"] if g["tconst"] == "tt0000101")
    assert [p["asin"] for p in casa["products"]] == [f"{A}A01", f"{A}A02", f"{A}A03", f"{A}J01"]
    assert casa["imdb_source"] and casa["imdb_source"]["file"] == "title.basics.tsv.gz" \
        and casa["imdb_source"]["tconst"] == "tt0000101"
    for g in ex["cross_edition_merges"]:
        for p in g["products"]:                                              # 每个商品都有可定位的页面来源
            assert p["title"] and p["final_url"] and p["snapshot_path"] and p["content_hash"]
            assert p["tier"] or p["basis"] == "singleton"
    page_cluster = next(g for g in ex["cross_edition_merges"] if g["status"] == "page_only")
    assert {p["asin"] for p in page_cluster["products"]} == {f"{A}F01", f"{A}F02"}
    pairs = {(g["left"]["asin"], g["right"]["asin"]): g for g in ex["confusable_not_merged"]}
    assert len(pairs) >= 3
    b = pairs.get((f"{A}B01", f"{A}B02"))
    assert b and b["tier"] == "reject" and "director_disjoint" in b["veto"] and b["features"]["director"]["value"] \
        == "disjoint"
    for g in ex["confusable_not_merged"]:
        assert g["left"]["final_url"] and g["right"]["snapshot_path"] and g["left"]["movie_id"] != g["right"]["movie_id"]
    assert all(g["annotation"] is None for g in ex["cross_edition_merges"] + ex["confusable_not_merged"])
    text = (d / "examples.md").read_text()
    assert "Casablanca" in text and "Breathless" in text and "未标注" in text


def test_examples_state_the_users_annotation_when_there_is_one(conn, world):
    fx, sid, v, d = world
    conn.execute("INSERT INTO task4.eval_set (eval_set_id, extraction_id, mapping_version_id, rule_version, strata) "
                 "VALUES ('es1', %s, %s, 't4r1', '{}')", (fx.eid, v))
    for pid, a, b, label in (("p1", f"{A}A01", f"{A}A02", "same"), ("p2", f"{A}B01", f"{A}B02", "different")):
        conn.execute("INSERT INTO task4.eval_pair (eval_set_id, pair_id, stratum, left_entity, right_entity, "
                     "predicted_same) VALUES ('es1', %s, 'S1', %s, %s, false)", (pid, a, b))
        conn.execute("INSERT INTO task4.eval_label (eval_set_id, pair_id, label, annotator, import_file_sha256) "
                     "VALUES ('es1', %s, %s, '张三', %s)", (pid, label, "0" * 64))
    ex = examples(conn, v)
    casa = next(g for g in ex["cross_edition_merges"] if g["tconst"] == "tt0000101")
    assert casa["annotation"] == {"label": "same", "annotator": "张三", "note": None}
    assert ex["cross_edition_merges"][0]["movie_id"] == casa["movie_id"]       # 已标注的排在最前
    b = next(g for g in ex["confusable_not_merged"] if g["left"]["asin"] == f"{A}B01")
    assert b["annotation"]["label"] == "different" and ex["confusable_not_merged"][0] is b


def test_export_files_match_recorded_hashes_and_hold_no_review_text_or_html(conn, world):
    fx, sid, v, d = world
    summary = json.loads((d / "summary.json").read_text())
    names = {f["path"] for f in summary["exports"]}
    assert names == {"movies.csv", "product_movie.csv", "decisions_sample.csv", "examples.md"}
    for f in summary["exports"]:
        raw = (d / f["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == f["sha256"] and len(raw) == f["bytes"]
        assert b"<html" not in raw.lower() and b"review_text" not in raw
    rows = list(csv.DictReader((d / "movies.csv").open(encoding="utf-8")))
    assert len(rows) == one(conn, "SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", v)
    rel = list(csv.DictReader((d / "product_movie.csv").open(encoding="utf-8")))
    assert len(rel) == one(conn, "SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %s", v)
    sample = list(csv.DictReader((d / "decisions_sample.csv").open(encoding="utf-8")))
    assert {r["tier"] for r in sample} == {"auto", "review", "reject"} and len(sample) <= 600
    md = (d / "report.md").read_text()
    assert "合并前后" in md and "第三方与页面分开" in md and "评论记录口径" in md


def test_check_passes_on_the_fixture_and_reports_the_expected_numbers(conn, world):
    fx, sid, v, d = world
    out = check.verify(conn, v)
    assert check.problems(out) == [], check.problems(out)
    assert out["product_edition_rows"] == out["manifest_rows"] == len(products())
    assert out["movie_edition_relations"] == out["movie_products"] == 18 and out["tv_series_relations"] == 0
    assert out["unresolved_cluster_conflicts"] == 0 and out["migrations"][-2:] == ["301", "302"]
    assert out["counts_recorded"] == out["counts_recounted"]


def test_check_finds_a_missing_edition_relation_a_forged_auto_decision_and_a_hard_conflict(conn, world):
    fx, sid, v, d = world
    conn.execute("DELETE FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s", (v, f"{A}I01"))
    found = check.problems(check.verify(conn, v))
    assert any("电影类商品" in x for x in found)
    conn.execute("UPDATE task4.merge_decision SET tier = 'auto' WHERE decision_id = (SELECT d.decision_id FROM "
                 "task4.merge_decision d JOIN task4.candidate c USING (candidate_id) WHERE c.left_entity = %s AND "
                 "c.right_kind = 'imdb' LIMIT 1)", (f"{A}I01",))
    found = check.problems(check.verify(conn, v))
    assert any("R22" in x for x in found) or any("不满足" in x for x in found)
    # 把冲突的两个商品塞进同一部作品：未处理的簇内硬冲突
    mid = one(conn, "SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s "
                    "AND relation_type = 'edition'", v, f"{A}B01")
    conn.execute("UPDATE task4.product_movie SET movie_id = %s WHERE mapping_version_id = %s AND asin = %s AND "
                 "relation_type = 'edition'", (mid, v, f"{A}B02"))
    conn.execute("UPDATE task4.movie SET edition_count = edition_count + 1 WHERE mapping_version_id = %s AND "
                 "movie_id = %s", (v, mid))
    found = check.problems(check.verify(conn, v))
    assert any("簇内硬冲突" in x for x in found)


def test_check_is_read_only(conn, world):
    fx, sid, v, d = world
    before = {t: one(conn, f"SELECT count(*) FROM task4.{t}") for t in ("run", "merge_decision", "product_movie")}
    check.verify(conn, v)
    assert before == {t: one(conn, f"SELECT count(*) FROM task4.{t}") for t in before}
    with pytest.raises(RuntimeError):
        check.verify(conn, "map9999")
