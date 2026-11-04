"""任务 9 目标层字段选择 build_selections 与评论、商品视图（spec AC04、AC10；AC01、AC03、AC08 的选择部分）。
夹具见 tests/fixtures/task9/README.md；预期值逐行对照上游目标表，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_selections.py
"""

from __future__ import annotations

import gzip

import pytest

import _task8_helpers as h8
import _task9_helpers as h
from etl_task9.rules import assertion_id, selection_id
from etl_task9.run import apply_task9_schema, check_inputs
from etl_task9.selections import check_values, selection_params, trace_all
from etl_task9.run import RunContext


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    t.out = h.run(conn, t, through="selections", batch_size=500)["selections"]
    return t


def _lk(conn):
    return h.stage_key(conn, "selections")


def _sel(conn, entity_type, entity_id, field, member=""):
    row = conn.execute("SELECT * FROM task9.field_selection WHERE stage_key = %s AND entity_type = %s AND entity_id = %s "
                       "AND field_name = %s AND member_key = %s", (_lk(conn), entity_type, entity_id, field, member))
    cols = [d.name for d in row.description]
    r = row.fetchone()
    return dict(zip(cols, r)) if r else None


def _params(conn, t):
    inputs = check_inputs(conn, t.inputs())
    run_id = conn.execute("SELECT run_id FROM task9.run ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    ctx = RunContext(run_id, inputs)
    keys = [h.stage_key(conn, s) for s in ("selections", "derivations", "assertions", "sources")]
    return ctx, selection_params(conn, ctx, *keys)


# ---------------------------------------------------------------- AC10：字段选择、状态与目标层一致（R23–R27）

def test_ac10_scalar_status_and_missing_reason_match_task7(conn, t9):
    """作品与版本标量：每个 task7 字段选择恰好一行，状态相同（resolved、conflict_unresolved、missing），值等于
    movie_resolved / edition_resolved；NULL 值都有缺失原因；采用断言即任务 7 选中候选。"""
    up = conn.execute("SELECT 'movie', movie_id, field_name, status, selected_candidate_ids FROM task7.movie_field_selection "
                      "WHERE conflict_version_id = %s AND field_name IN ('title', 'original_title', 'release_year', "
                      "'runtime_minutes', 'mpaa_rating') UNION ALL SELECT 'edition', asin, field_name, status, "
                      "selected_candidate_ids FROM task7.edition_field_selection WHERE conflict_version_id = %s",
                      (t9.cv, t9.cv)).fetchall()
    assert {r[3] for r in up} == {"resolved", "conflict_unresolved", "missing"}
    for et, eid, field, status, sel in up:
        s = _sel(conn, et, eid, field)
        assert s["status"] == status and s["decision_version"] == t9.cv
        assert s["selected_assertion_ids"] == sorted(assertion_id("task7.field_candidate", str(c)) for c in sel)
        if s["value_text"] is None:
            assert s["missing_reason"]
    lg = t9.mid("Lg")
    title = conn.execute("SELECT title, release_year FROM task7.movie_resolved WHERE conflict_version_id = %s "
                         "AND movie_id = %s", (t9.cv, lg)).fetchone()
    assert _sel(conn, "movie", lg, "title")["value_text"] == title[0]
    year = _sel(conn, "movie", lg, "release_year")
    assert (year["value_text"], year["value_int"], year["trace_status"]) == (str(title[1]), title[1], "traced")


def test_ac10_multivalue_members_and_inferred_bound(conn, t9):
    """多值字段按成员一行（演职员 person_id、类型受控词），无成员的字段一行 missing；推断上界为 inferred，值等于
    movie_resolved.inferred_upper_bound，采用任务 6 上界断言；没有上界的作品一行 missing。"""
    multi = check_inputs(conn, t9.inputs()).cv_stage_keys[3]
    members = conn.execute("SELECT movie_id, role, person_id, status FROM task7.movie_person_resolved WHERE stage_key = %s",
                           (multi,)).fetchall()
    assert members and {r[3] for r in members} >= {"adopted"}
    for movie, role, person, status in members:
        s = _sel(conn, "movie", movie, role, person)
        assert s["status"] == ("resolved" if status == "adopted" else "conflict_unresolved")
        assert s["selected_assertion_ids"] == [assertion_id("task7.movie_person_resolved",
                                                            f"{t9.cv}|{movie}|{person}|{role}")]
    genres = conn.execute("SELECT movie_id, genre FROM task7.movie_genre WHERE stage_key = %s", (multi,)).fetchall()
    assert all(_sel(conn, "movie", m, "genres", g)["status"] == "resolved" for m, g in genres)
    empty = conn.execute("SELECT movie_id, field_name FROM task7.movie_field_selection WHERE conflict_version_id = %s "
                         "AND field_name = 'director' AND status = 'missing'", (t9.cv,)).fetchall()
    assert empty
    for m, f in empty:
        s = _sel(conn, "movie", m, f)
        assert s["status"] == "missing" and s["value_text"] is None and s["missing_reason"]
    lg = t9.mid("Lg")
    bound = _sel(conn, "movie", lg, "inferred_upper_bound")
    target = conn.execute("SELECT inferred_upper_bound FROM task7.movie_resolved WHERE conflict_version_id = %s "
                          "AND movie_id = %s", (t9.cv, lg)).fetchone()[0]
    assert (bound["status"], bound["value_date"], bound["decision_version"]) == ("inferred", target, h.h7.RV)
    assert bound["selected_assertion_ids"] == [assertion_id("task6.inferred_release_bound", f"{h.h7.RV}|{lg}")]
    assert bound["trace_status"] == "relocated"            # 第二早评论经重放补出
    nob = conn.execute("SELECT movie_id FROM task7.movie_resolved WHERE conflict_version_id = %s "
                       "AND inferred_upper_bound IS NULL LIMIT 1", (t9.cv,)).fetchone()[0]
    s = _sel(conn, "movie", nob, "inferred_upper_bound")
    assert s["status"] == "missing" and s["missing_reason"]


def test_ac10_injected_mismatch_detected(conn, t9):
    """核对通过后注入两处与目标层不一致：改一个最终值、删一个成员选择；check_values 都检出（R26）。"""
    ctx, p = _params(conn, t9)
    assert t9.out["values"]["mismatches"] == 0
    lg = t9.mid("Lg")
    sid = selection_id("movie", lg, "title")
    conn.execute("UPDATE task9.field_selection SET value_text = 'Wrong Title' WHERE stage_key = %s AND selection_id = %s",
                 (_lk(conn), sid))
    genre = conn.execute("SELECT selection_id, entity_id, member_key FROM task9.field_selection WHERE stage_key = %s "
                         "AND field_name = 'genres' AND member_key <> '' ORDER BY 1 LIMIT 1", (_lk(conn),)).fetchone()
    conn.execute("DELETE FROM task9.field_selection WHERE stage_key = %s AND selection_id = %s", (_lk(conn), genre[0]))
    out = check_values(conn, ctx, p)
    found = {r[0]: r[5] for r in out["samples"]}
    assert out["mismatches"] == 2
    assert found[sid] == "value_or_status"
    assert found[f"movie:{genre[1]}:genres:{genre[2]}"] == "missing_selection"


def test_ac10_manual_path_and_eval_labels_not_revisions(conn, t9):
    """人工修订只来自任务 4、5 的 human_decision：夹具的一条 must_link 登记为 manual_revision 源记录，但任务 4 的
    map 版本没有采用它（导入晚于映射），所以没有选择是 manual；在派生图里补一条“商品 → 人工修订”边后，该商品
    所属作品的选择判为 manual。任务 6–8 的评价标注不登记为源记录（R27）。"""
    sk, dk = h.stage_key(conn, "sources"), h.stage_key(conn, "derivations")
    manual = conn.execute("SELECT source_record_id, upstream_table FROM task9.source_record WHERE stage_key = %s "
                          "AND source_kind = 'manual_revision'", (sk,)).fetchall()
    assert [m[1] for m in manual] == ["task4.human_decision"]
    assert conn.execute("SELECT count(*) FROM task9.source_record WHERE stage_key = %s AND upstream_table LIKE "
                        "'%%eval%%'", (sk,)).fetchone()[0] == 0
    assert t9.out["by_trace_status"].get("manual", 0) == 0
    ctx, p = _params(conn, t9)
    child = assertion_id("task4.product_movie", f"map0002|B0R1B|0|{t9.mid('R1')}")
    assert conn.execute("SELECT count(*) FROM task9.assertion WHERE assertion_id = %s", (child,)).fetchone()[0] == 1
    conn.execute("INSERT INTO task9.derivation (stage_key, child_kind, child_id, parent_kind, parent_id, operation, "
                 "ol_type, ol_subtype, rule_id, rule_version, upstream_task, run_id) VALUES (%s, 'assertion', %s, "
                 "'source_record', %s, 'link', 'INDIRECT', 'JOIN', 'human', 't4r2', 'task4', %s)",
                 (dk, child, manual[0][0], ctx.run_id))
    trace_all(conn, ctx, p)
    s = _sel(conn, "product_movie", "B0R1B", "movie_id", f"0|{t9.mid('R1')}")
    assert s["trace_status"] == "manual"


def test_ac10_coverage_full_and_graph_closed(conn, t9):
    """夹具全部非空值都是 traced、relocated 或 manual；非空选择都有 select 边，图无悬空、无环。"""
    assert t9.out["coverage"] == 1.0 and t9.out["trace"]["gaps"] == 0
    g = t9.out["graph"]
    assert (g["dangling"], g["no_parent"], g["cycle_nodes"]) == (0, 0, 0)
    assert set(t9.out["by_status"]) == {"resolved", "conflict_unresolved", "missing", "inferred"}


# ---------------------------------------------------------------- AC01：范围（R04、R05）

def test_ac01_out_of_scope_and_no_rating_statistics(conn, t9):
    """范围外实体不生成选择：IMDb 作品与人名行、页面提及、上界引用的评论、合集成员都有断言，但不是最终字段，
    没有选择；旧映射版本 map0001 与另一个 cv 版本的结果不进入选择。评分统计（任务 8 统计表）既没有选择，
    也没有断言或逐条评论边（R05）。正式库的未映射商品见 checklist 的副本查询。"""
    lk, ak = _lk(conn), h.stage_key(conn, "assertions")
    asserted = {r[0] for r in conn.execute("SELECT DISTINCT entity_type FROM task9.assertion WHERE stage_key = %s", (ak,))}
    assert {"imdb_title", "imdb_name", "mention", "review", "collection_member"} <= asserted
    assert {r[0] for r in conn.execute("SELECT DISTINCT entity_type FROM task9.field_selection WHERE stage_key = %s",
                                       (lk,))} == {"movie", "edition", "product_movie", "person"}
    assert {r[0] for r in conn.execute("SELECT DISTINCT decision_version FROM task9.field_selection WHERE stage_key = %s",
                                       (lk,))} == {t9.cv, h.h7.RV, "map0002", "pmap0001", "t3r2"}
    assert conn.execute("SELECT count(*) FROM task9.field_selection WHERE stage_key = %s AND entity_type = 'edition' "
                        "AND entity_id NOT IN (SELECT asin FROM task7.edition_resolved WHERE conflict_version_id = %s)",
                        (lk, t9.cv)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task9.field_selection WHERE stage_key = %s AND (target_ref->>'table' LIKE "
                        "'task8.%%' OR field_name LIKE '%%rating%%stat%%')", (lk,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task9.assertion WHERE upstream_table LIKE 'task8.%%'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task9.derivation WHERE rule_id LIKE 'task8%%' OR upstream_task = 'task8'"
                        ).fetchone()[0] == 0


# ---------------------------------------------------------------- AC03：证据有问题的字段（R09、R25）

def test_ac03_bad_snapshot_marks_fields_evidence_unverified(conn, make_settings, tmp_path):
    """B0LgB 快照内容被改、B0LgD 快照缺失：经它们的页面断言得到的非空版本字段都判为 evidence_unverified，
    并写 untraced_value；Legend 本身的 B0Lg 版本字段不受影响。"""
    t = h.seed(conn, make_settings, tmp_path)
    (t.snapshot_root / "snapshots" / "B0LgB.html.gz").write_bytes(gzip.compress(b"tampered page body"))
    (t.snapshot_root / "snapshots" / "B0LgD.html.gz").unlink()
    h.run(conn, t, through="selections")
    lk = _lk(conn)
    rows = conn.execute("SELECT entity_id, trace_status, count(*) FROM task9.field_selection WHERE stage_key = %s "
                        "AND entity_type = 'edition' AND entity_id IN ('B0Lg', 'B0LgB', 'B0LgD') AND value_text IS NOT NULL "
                        "AND field_name IN ('edition_title', 'category') GROUP BY 1, 2 ORDER BY 1, 2", (lk,)).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("B0Lg", "traced"), ("B0LgB", "evidence_unverified"),
                                            ("B0LgD", "evidence_unverified")]
    issues = {r[0] for r in conn.execute("SELECT entity_id FROM task9.quality_issue WHERE stage_key = %s "
                                         "AND issue_code = 'untraced_value'", (lk,))}
    assert selection_id("edition", "B0LgB", "edition_title") in issues
    assert selection_id("edition", "B0LgD", "edition_title") in issues


# ---------------------------------------------------------------- AC08：重放不一致保持 partial（R21、R25）

def test_ac08_replay_mismatch_fields_partial(conn, make_settings, tmp_path):
    """同 test_task9_relocate 的不一致夹具：G1 的被改词面所映射的类型成员、B0R1B 的 PAL、Legend 的推断上界都判为
    partial（不是 relocated），并计入缺口；其余重放一致的成员仍为 relocated。"""
    t = h.seed(conn, make_settings, tmp_path)
    multi = check_inputs(conn, t.inputs()).cv_stage_keys[3]
    g1, lg = t.mid("G1"), t.mid("Lg")
    section, term, genres = conn.execute(
        "SELECT t.section, t.term, array_agg(g.genre) FROM task7.movie_genre_term t JOIN task7.movie_genre g "
        "ON g.stage_key = t.stage_key AND g.movie_id = t.movie_id AND t.term = ANY(g.raw_terms) "
        "WHERE t.stage_key = %s AND t.movie_id = %s GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 1", (multi, g1)).fetchone()
    conn.execute("UPDATE task7.movie_genre_term SET asins = '{B0Other}' WHERE stage_key = %s AND movie_id = %s "
                 "AND section = %s AND term = %s", (multi, g1, section, term))
    conn.execute("UPDATE task7.edition_resolved SET attributes_detail = jsonb_set(attributes_detail, "
                 "'{video_system,PAL}', '[\"title\"]') WHERE conflict_version_id = %s AND asin = 'B0R1B'", (t.cv,))
    conn.execute("UPDATE task6.movie_review_time SET second_review_time = second_review_time + interval '1 day' "
                 "WHERE movie_id = %s", (lg,))
    out = h.run(conn, t, through="selections")["selections"]
    assert all(_sel(conn, "movie", g1, "genres", g)["trace_status"] == "partial" for g in genres)
    assert _sel(conn, "edition", "B0R1B", "video_system", "PAL")["trace_status"] == "partial"
    assert _sel(conn, "movie", lg, "inferred_upper_bound")["trace_status"] == "partial"
    assert out["by_trace_status"]["partial"] == len(genres) + 2 == out["trace"]["gaps"]
    assert out["by_trace_status"]["relocated"] > 0


# ---------------------------------------------------------------- AC04：评论与商品视图（R10、R29）

def test_ac04_review_view_states_family_and_collection(conn, make_settings, tmp_path):
    """任务 8 小世界（经任务 1 装载与任务 8 自己的运行生成）：有效、隔离、不完整三条原始评论分别返回 mapped、
    quarantined、incomplete；D3 家族复制返回逻辑评论、层次与家族；只在合集上的逻辑评论只有商品没有作品；
    task9 里没有评论映射的表（只有视图）。"""
    eid = h8.seed_small_world(conn, make_settings, tmp_path)
    h8.execute(conn, eid)
    apply_task9_schema(conn)

    def view(user):
        return conn.execute(
            "SELECT v.mapping_status, v.match_level, v.logical_review_id, v.family_id, v.asin, v.movie_id, v.raw_sha256 "
            "FROM task9.v_review_lineage v JOIN public.review_record r ON r.extraction_id = v.extraction_id "
            "AND r.record_no = v.record_no WHERE v.extraction_id = %s AND v.review_dedup_version_id = 'rd0001' "
            "AND r.reviewer_id_observed = %s ORDER BY v.record_no, v.movie_id", (eid, user)).fetchall()

    assert view("EU1")[0][:2] == ("mapped", "representative")
    assert [r[0] for r in view("EU13")] == ["quarantined"] and view("EU13")[0][2] is None
    assert [r[0] for r in view("EU12")] == ["incomplete"]
    d3 = view("EL1")
    assert sorted(r[1] for r in d3) == ["D3_family_copy", "representative"]
    assert len({r[2] for r in d3}) == 1 and all(r[3] for r in d3) and all(r[5] == h8.mid("E1") for r in d3)
    coll = view("EL2")
    assert [(r[4], r[5]) for r in coll] == [("B0000E2EC1", None)]
    kinds = dict(conn.execute("SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'task9' "
                              "AND (table_name LIKE '%%review%%' OR table_name LIKE '%%product%%')").fetchall())
    assert kinds == {"v_review_lineage": "VIEW", "v_product_lineage": "VIEW"}


def test_ac04_product_view_collected_and_not_collected(conn, t9):
    """商品视图：Legend 的 edition 商品在 cv 版本下一行，返回采集状态、作品与关系类型、版本属性；类别与匹配状态
    取自任务 3、4 的表（夹具没有这两张表的行，列为空而不报错；正式库见 checklist 的副本查询）。"""
    rows = conn.execute("SELECT collection_status, category, movie_id, relation_type, match_status, edition_title "
                        "FROM task9.v_product_lineage WHERE extraction_id = %s AND asin = 'B0Lg' "
                        "AND conflict_version_id = %s", (t9.eid, t9.cv)).fetchall()
    assert len(rows) == 1, rows
    status, category, movie, relation, match, title = rows[0]
    assert (status, movie, relation, category, match) == ("success", t9.mid("Lg"), "edition", None, None)
    assert title == conn.execute("SELECT edition_title FROM task7.edition_resolved WHERE conflict_version_id = %s "
                                 "AND asin = 'B0Lg'", (t9.cv,)).fetchone()[0]
    # 每个映射版本与冲突版本各一行：三个版本（cv 与旧规则版本）× 两个映射版本中只有 cv 对应的映射版本有作品
    n = conn.execute("SELECT count(DISTINCT (mapping_version_id, conflict_version_id)) FROM task9.v_product_lineage "
                     "WHERE extraction_id = %s AND asin = 'B0Lg'", (t9.eid,)).fetchone()[0]
    assert n >= 2
