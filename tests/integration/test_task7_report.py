"""任务 7 报告、实例、三个只读查询与核对（spec AC14：report export 与 query；AC17：run-id 校验、阶段键防串读、check 检出异常）。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_report.py
"""

from __future__ import annotations

import hashlib
import json

import pytest

import _task7_helpers as h
from _task7_worlds import rich_world, small_world
from etl_task7 import check, query, report
from etl_task7.report import build_and_write_report, build_summary, examples
from etl_task7.runner import VerificationFailed, execute_conflict

pytestmark = pytest.mark.integration

R60_CATEGORIES = ("title_conflict", "year_disagreement", "mismatch_halt", "runtime_exact", "runtime_tolerance_1_2",
                  "runtime_pal_evidence", "runtime_variance_3_5", "runtime_cut_or_multidisc", "runtime_unexplained_6_15",
                  "runtime_unexplained_gt15", "credits_union", "credit_alignment", "multi_person", "billing_order",
                  "genre_mapping", "rating", "edition_publisher", "edition_release_date", "shared_tconst")


@pytest.fixture
def rich(conn):
    s = h.seed(conn, rich_world())
    r = execute_conflict(conn, s.inputs(), batch_size=4)
    assert r.status == "succeeded", r
    return conn, s, r.conflict_version_id


def q1(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


# ------------------------------------------------------------------ AC14 summary.json 与数据库一致

def test_ac14_summary_counts_match_the_database(rich):
    conn, s, cv = rich
    sm = build_summary(conn, cv)
    movies = q1(conn, "SELECT count(*) FROM task4.movie WHERE mapping_version_id = 'map0002'")
    editions = q1(conn, "SELECT count(*) FROM task4.product_edition WHERE match_scope = 'matched'")
    assert (sm["totals"]["movies"], sm["totals"]["editions"], sm["totals"]["all_products"]) == (movies, editions, 253059)
    for field, c in sm["coverage"]["movie_fields"].items():
        got = {st: q1(conn, "SELECT count(*) FROM task7.movie_field_selection WHERE conflict_version_id = %s AND field_name = %s "
                            "AND status = %s", cv, field, st) for st in ("resolved", "conflict_unresolved", "missing")}
        assert {k: c[k] for k in got} == got and sum(got.values()) == movies == c["denominator"]
    for field, c in sm["coverage"]["edition_fields"].items():
        got = {st: q1(conn, "SELECT count(*) FROM task7.edition_field_selection WHERE conflict_version_id = %s AND field_name = %s "
                            "AND status = %s", cv, field, st) for st in ("resolved", "conflict_unresolved", "missing")}
        assert {k: c[k] for k in got} == got and sum(got.values()) == editions == c["denominator"]
        assert c["all_products_denominator"] == 253059
    sk = sm["stage_keys"]
    assert sm["link_assessment"]["halted"] == q1(conn, "SELECT count(*) FROM task7.link_assessment WHERE stage_key = %s "
                                                  "AND cross_source_mismatch_halt", sk["link"]) >= 1
    assert sm["link_assessment"]["assessed"] == q1(conn, "SELECT count(*) FROM task7.link_assessment WHERE stage_key = %s", sk["link"])
    assert sum(sm["runtime_diff"]["by_class"].values()) == q1(conn, "SELECT count(*) FROM task7.runtime_diff WHERE stage_key = %s", sk["scalar"])
    assert set(sm["runtime_diff"]["by_class"]) == {"exact", "tolerance_1_2", "pal_evidence", "variance_3_5", "cut_or_multidisc",
                                                   "unexplained_6_15", "unexplained_gt15"}
    assert sum(sm["candidate_status"]["movie"].values()) == q1(
        conn, "SELECT count(*) FROM task7.field_candidate WHERE stage_key = %s AND entity_type = 'movie'", sk["scalar"])
    assert sum(sm["candidate_status"]["edition"].values()) == q1(
        conn, "SELECT count(*) FROM task7.field_candidate WHERE stage_key = %s AND entity_type = 'edition'", sk["edition"])
    ct = sm["conflict_types"]["movie"]
    assert sum(n for by in ct.values() for n in by.values()) == q1(
        conn, "SELECT count(*) FROM task7.movie_field_selection WHERE conflict_version_id = %s", cv)
    assert sm["shared_tconst"]["pairs"] == q1(conn, "SELECT count(*) FROM task7.shared_tconst_conflict WHERE conflict_version_id = %s", cv) == 2
    assert sum(sm["credit_alignment"]["by_role_outcome_level"].values()) == q1(
        conn, "SELECT count(*) FROM task7.credit_alignment WHERE stage_key = %s", sk["multivalue"])
    assert sm["people"]["imdb_person_ext"] == q1(conn, "SELECT count(*) FROM task7.imdb_person_ext")
    assert sum(sm["cast_coverage"]["by_value"].values()) == movies and "不是完整演员表" in sm["cast_coverage"]["note"]
    assert sum(v["terms"] for v in sm["genres"]["term_dispositions"].values()) == q1(
        conn, "SELECT count(*) FROM task7.movie_genre_term WHERE stage_key = %s", sk["multivalue"])
    ry = sm["coverage"]["movie_fields"]["release_year"]
    assert ry["inferred"] == q1(conn, "SELECT count(*) FROM task7.movie_resolved r JOIN task7.movie_field_selection f ON "
                                       "f.conflict_version_id = r.conflict_version_id AND f.movie_id = r.movie_id AND "
                                       "f.field_name = 'release_year' WHERE r.conflict_version_id = %s AND f.status = 'missing' "
                                       "AND r.inferred_upper_bound IS NOT NULL", cv)


def test_ac14_examples_cover_every_r60_category_with_locators(rich):
    conn, s, cv = rich
    ex = examples(conn, cv)
    assert set(R60_CATEGORIES) <= set(ex)
    for cat in R60_CATEGORIES:
        assert 1 <= len(ex[cat]) <= 3, cat
        for item in ex[cat]:
            assert item.get("movie_id") or item.get("asin") or item.get("tconst"), (cat, item)    # 可定位
    t = ex["title_conflict"][0]
    assert any(c["status"] == "superseded_by_specific" and c["source_ref"].get("asin") for c in t["candidates"])
    assert all("candidate_id" in c and "source" in c for c in t["candidates"])
    assert ex["mismatch_halt"][0]["link_assessment"]["halt"] is True
    assert ex["multi_person"] and any(r["outcome"] == "unsplit_multi_person" for it in ex["multi_person"] for r in it["alignment"]) \
        or any(r["outcome"] == "aligned" for it in ex["multi_person"] for r in it["alignment"])
    md = report.render_examples_markdown(ex)
    for cat in R60_CATEGORIES:
        assert f"## {cat}（" in md


def test_ac14_export_writes_files_whose_recorded_hashes_match(rich, tmp_path):
    conn, s, cv = rich
    summary, directory, digests = build_and_write_report(conn, cv, tmp_path, extraction_id=s.eid)
    assert directory == tmp_path / "task7" / s.eid / cv / summary["run_id"]
    assert {"summary.json", "report.md", "examples.md", "movie_field_selection.csv", "edition_field_selection.csv"} <= set(digests)
    for name, dg in digests.items():
        assert dg["sha256"] == hashlib.sha256((directory / name).read_bytes()).hexdigest()
    on_disk = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert on_disk["totals"] == summary["totals"] and on_disk["coverage"] == json.loads(json.dumps(summary["coverage"]))
    assert (directory / "movie_field_selection.csv").read_text(encoding="utf-8").count("\n") == \
        q1(conn, "SELECT count(*) FROM task7.movie_field_selection WHERE conflict_version_id = %s", cv) + 1
    for p in directory.iterdir():                                       # 导出不含评论正文与整页 HTML
        text = p.read_text(encoding="utf-8").lower()
        assert "<html" not in text and "review_text" not in text and "reviewtext" not in text


# ------------------------------------------------------------------ AC14 三个只读查询

def test_ac14_query_movie_returns_values_candidates_reasons_and_link_assessment(rich):
    conn, s, cv = rich
    r = query.query_movie(conn, cv, s.mid("Sam"))
    assert r["found"] and r["movie"]["title"] == "Seven Samurai"
    t = r["fields"]["title"]
    assert t["status"] == "resolved" and t["winning_value"] == "Seven Samurai" and t["conflict_type"] == "formatting_or_alias"
    assert t["reason"] and t["selection_rule"] == "T1" and t["selected_candidate_ids"]
    assert {c["status"] for c in t["candidates"]} == {"adopted", "superseded_by_specific"}      # 未采用候选也在
    assert all(c["source_ref"] for c in t["candidates"])
    assert r["link_assessment"]["tconst"] and r["link_assessment"]["halt"] is False
    assert r["aliases"]["aka"] >= 1 and r["editions"][0]["asin"] == "B0Sam"
    halted = query.query_movie(conn, cv, s.mid("Y4"))
    assert halted["link_assessment"]["halt"] is True and halted["fields"]["release_year"]["status"] == "conflict_unresolved"
    assert {c["status"] for c in halted["fields"]["release_year"]["candidates"]} == {"conflict_pending"}
    assert query.query_movie(conn, cv, "mv_missing")["found"] is False
    people = query.query_movie(conn, cv, s.mid("C1"))["people"]
    assert {(p["role"], p["source_coverage"]) for p in people} >= {("starring", "page_only"), ("cast", "both"), ("director", "imdb_only")}


def test_ac14_query_edition_returns_attributes_internal_conflicts_movies_and_review_stats(rich):
    conn, s, cv = rich
    r = query.query_edition(conn, cv, "B0E1")
    assert r["found"] and r["edition"]["publisher"] == "Warner Home Video"
    assert set(r["internal_conflicts"]) == {"publisher", "product_release_date"}
    assert r["internal_conflicts"]["publisher"]["conflict_type"] == "formatting_or_alias"
    assert r["fields"]["publisher"]["candidates"] and r["movies"][0]["movie_id"] == s.mid("Mv1")
    assert r["review_stats"] == {"by_classification": {}} and r["edition"]["media_release_date"] == "2003-05-20"
    assert query.query_edition(conn, cv, "B0NONE")["found"] is False


def test_ac14_query_conflicts_filters_by_type_and_field(rich):
    conn, s, cv = rich
    allc = query.query_conflicts(conn, cv, limit=500)
    assert allc["returned"] == q1(conn, "SELECT (SELECT count(*) FROM task7.movie_field_selection WHERE conflict_version_id = %s "
                                       "AND conflict_type <> 'none') + (SELECT count(*) FROM task7.edition_field_selection "
                                       "WHERE conflict_version_id = %s AND conflict_type <> 'none')", cv, cv)
    halt = query.query_conflicts(conn, cv, conflict_type="cross_source_mismatch_halt")
    assert halt["returned"] > 0 and {c["conflict_type"] for c in halt["conflicts"]} == {"cross_source_mismatch_halt"}
    assert all(c["reason"] and c["rule"] for c in halt["conflicts"])
    pub = query.query_conflicts(conn, cv, field="publisher", layer="edition")
    assert {c["field"] for c in pub["conflicts"]} == {"publisher"} and {c["layer"] for c in pub["conflicts"]} == {"edition"}
    assert {c["conflict_type"] for c in pub["conflicts"]} == {"formatting_or_alias", "intra_asin_publisher_conflict"}
    assert query.query_conflicts(conn, cv, limit=2)["returned"] == 2


def test_ac14_cli_entry_points_print_json(rich, capsys):
    conn, s, cv = rich
    assert query.main(["--cv", cv, "--extraction-id", s.eid, "movie", s.mid("Sam")]) == 0
    assert json.loads(capsys.readouterr().out)["movie"]["title"] == "Seven Samurai"
    assert query.main(["--cv", cv, "--extraction-id", s.eid, "conflicts", "--type", "unrated_vs_rated"]) == 0
    assert json.loads(capsys.readouterr().out)["returned"] == 1
    assert report.main(["summary", "--cv", cv, "--extraction-id", s.eid]) == 0
    assert json.loads(capsys.readouterr().out)["conflict_version_id"] == cv
    assert check.main(["--cv", cv, "--extraction-id", s.eid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["problems"] == [] and out["passed"] is True


# ------------------------------------------------------------------ AC17 多运行共存下的报告与核对

def test_ac17_unknown_or_foreign_run_id_is_rejected(conn):
    s = h.seed(conn, small_world())
    r1 = execute_conflict(conn, s.inputs(), batch_size=2)
    with pytest.raises(ValueError, match="不存在"):
        build_summary(conn, r1.conflict_version_id, run_id="no-such-run")
    r2 = execute_conflict(conn, s.inputs(rule_version=_register("t7r1x")), batch_size=2)
    assert r2.conflict_version_id == "cv0002"
    with pytest.raises(ValueError, match="属于冲突消解版本"):
        build_summary(conn, "cv0001", run_id=r2.run_id)
    assert build_summary(conn, "cv0001", run_id=r1.run_id)["run_id"] == r1.run_id


def _register(version: str) -> str:
    from etl_task7 import CURRENT_RULE_VERSION
    from etl_task7.rules import RULESETS
    RULESETS[version] = RULESETS[CURRENT_RULE_VERSION].with_version(version, runtime_tol_small=3)
    return version


@pytest.fixture(autouse=True)
def _cleanup_rules():
    yield
    from etl_task7.rules import RULESETS
    RULESETS.pop("t7r1x", None)


def test_ac17_reused_run_still_reads_succeeded_counts(conn):
    s = h.seed(conn, small_world())
    first = execute_conflict(conn, s.inputs(), batch_size=2)
    reused = execute_conflict(conn, s.inputs(), batch_size=2)
    assert reused.status == "reused"
    base = build_summary(conn, "cv0001")
    assert base["run_id"] == first.run_id                                  # 默认不选 reused 运行
    via_reused = build_summary(conn, "cv0001", run_id=reused.run_id)
    assert via_reused["run_id"] == reused.run_id and via_reused["stage_counts"] == base["stage_counts"]
    assert base["stage_counts"]["edition_resolve"]["editions"] == 5       # 计数来自 succeeded 的那次运行


def test_ac17_check_detects_orphan_candidates_inconsistent_state_and_unverified_version(rich):
    conn, s, cv = rich
    assert check.problems(check.verify(conn, cv)) == []
    row = conn.execute("SELECT scalar_stage_key FROM task7.conflict_version WHERE conflict_version_id = %s", (cv,)).fetchone()
    # 1) 孤儿候选：阶段键下多出一条没有任何选择记录考虑的候选
    conn.execute("INSERT INTO task7.field_candidate (extraction_id, stage_key, entity_type, entity_id, field_name, source, "
                 "source_key, raw_value, normalized_value, precision, source_ref, match_reliability, status, rule_version, run_id) "
                 "SELECT extraction_id, stage_key, entity_type, entity_id, field_name, 'injected', 'x', '1', '1', precision, "
                 "'{}', 0.5, 'rejected', rule_version, run_id FROM task7.field_candidate WHERE stage_key = %s LIMIT 1", (row[0],))
    found = check.problems(check.verify(conn, cv))
    assert any("孤儿候选" in p for p in found), found
    conn.execute("DELETE FROM task7.field_candidate WHERE source = 'injected'")
    assert check.problems(check.verify(conn, cv)) == []
    # 2) 状态不一致：conflict_unresolved 字段的候选被改成 adopted
    conn.execute("UPDATE task7.field_candidate SET status = 'adopted' WHERE candidate_id = (SELECT considered_candidate_ids[1] "
                 "FROM task7.movie_field_selection WHERE conflict_version_id = %s AND status = 'conflict_unresolved' "
                 "AND cardinality(considered_candidate_ids) > 0 LIMIT 1)", (cv,))
    found = check.problems(check.verify(conn, cv))
    assert any("状态不一致" in p for p in found), found


def test_ac17_check_detects_materialization_mismatch_and_unverified_status(conn):
    s = h.seed(conn, small_world())
    with pytest.raises(VerificationFailed):
        execute_conflict(conn, s.inputs(), batch_size=2, inject_verify_failure=True)
    info = check.verify(conn, "cv0001")
    assert any("unverified" in p for p in check.problems(info))                         # 命令行要求 verified
    assert check.problems(info, expect_verified=False) == []                            # verify 步骤在标记前调用
    assert execute_conflict(conn, s.inputs(), batch_size=2).status == "succeeded"
    conn.execute("UPDATE task7.movie_resolved SET title = 'tampered' WHERE movie_id = %s", (s.mid("S1"),))
    assert any("不一致" in p for p in check.problems(check.verify(conn, "cv0001")))
    assert check.main(["--cv", "cv0001", "--extraction-id", s.eid]) == 1
