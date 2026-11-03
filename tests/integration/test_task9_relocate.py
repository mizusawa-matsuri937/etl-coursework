"""任务 9 只读重放补定位与推断上界依赖评论（spec AC08、AC09）。夹具见 tests/fixtures/task9/README.md；预期值按
spec 手算或逐列对照上游行，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_relocate.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import _task9_helpers as h
from etl_task7.rules import classify_genre_term
from etl_task9.relocate import earliest_two
from etl_task9.rules import assertion_id
from etl_task9.run import check_inputs
from task8.world import Rec, unix


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    h.run(conn, t, through="derivations")
    return t


def _keys(conn):
    return h.stage_key(conn, "sources"), h.stage_key(conn, "assertions"), h.stage_key(conn, "derivations")


def _relocations(conn, dk, kind):
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
        "SELECT target_id, located, consistent, upstream_value, replay_value FROM task9.relocation WHERE stage_key = %s "
        "AND target_kind = %s", (dk, kind))}


def _relocate_parents(conn, dk, child):
    return conn.execute("SELECT parent_id, rule_id, relocated, detail FROM task9.derivation WHERE stage_key = %s "
                        "AND child_id = %s AND operation = 'relocate' ORDER BY parent_id", (dk, child)).fetchall()


def _rid(conn, eid, reviewer):
    return conn.execute("SELECT source_review_id FROM public.review_staging WHERE extraction_id = %s "
                        "AND reviewer_id = %s", (eid, reviewer)).fetchone()[0]


# ---------------------------------------------------------------- AC08：类型词面

def test_ac08_genre_terms_relocated_to_region_ordinal_and_assertion(conn, t9):
    """G1 的页面类型词面：任务 7 只记 ASIN 与区域；重放补出每个词面来自哪条任务 4 genre 断言（genre 区）或哪个任务 3
    面包屑字段（区域、序号），与 movie_genre_term 一致，标 relocated。"""
    _sk, ak, dk = _keys(conn)
    movie = t9.mid("G1")
    multi = check_inputs(conn, t9.inputs()).cv_stage_keys[3]
    up = conn.execute("SELECT section, term, disposition, asins FROM task7.movie_genre_term WHERE stage_key = %s "
                      "AND movie_id = %s ORDER BY 1, 2", (multi, movie)).fetchall()
    assert up
    rel = _relocations(conn, dk, "genre_term")
    for section, term, disposition, asins in up:
        located, ok, upstream, replay = rel[f"{movie}|{section}|{term}"]
        assert ok and upstream == replay and upstream["disposition"] == disposition
        assert {it["asin"] for it in located["items"]} == set(asins) and located["items"]
        child = assertion_id("task7.movie_genre_term", f"{t9.cv}|{movie}|{section}|{term}")
        parents = _relocate_parents(conn, dk, child)
        assert len(parents) == len(located["items"]) and all(p[2] for p in parents)
        for it in located["items"]:
            if section == "genre":
                raw = conn.execute("SELECT entity_id, field_name, normalized_value FROM task4.field_assertion "
                                   "WHERE assertion_id = %s", (it["task4_assertion_id"],)).fetchone()
                assert raw[0] == it["asin"] and raw[1] == "genre" and classify_genre_term(raw[2])[0] == term
                parent = assertion_id("task4.field_assertion", str(it["task4_assertion_id"]))
            else:
                raw = conn.execute("SELECT value_raw FROM task3.page_field WHERE extraction_id = %s AND asin = %s "
                                   "AND parser_version = 't3p1' AND section = %s AND ordinal = %s",
                                   (t9.eid, it["asin"], it["page_section"], it["ordinal"])).fetchone()
                assert classify_genre_term(raw[0])[0] == term
                parent = assertion_id("task3.page_field",
                                      f"{t9.eid}|{it['asin']}|t3p1|{it['page_section']}|{it['ordinal']}")
            assert parent in {p[0] for p in parents}
            assert conn.execute("SELECT count(*) FROM task9.assertion WHERE stage_key = %s AND assertion_id = %s",
                                (ak, parent)).fetchone()[0] == 1


def test_ac08_edition_features_relocated(conn, t9):
    """版本多值特征：任务 7 的 attributes_detail 只有“值 → 区域”；重放补出每个值来自哪条断言或字段。
    B0R1B 的 video_system PAL 来自详情要点区的 edition.video_system 断言。"""
    _sk, ak, dk = _keys(conn)
    rel = _relocations(conn, dk, "edition_feature")
    total = conn.execute("SELECT sum(cardinality(media_format) + cardinality(language) + cardinality(subtitles) + "
                         "cardinality(aspect_ratio) + cardinality(region) + cardinality(cut_type) + "
                         "cardinality(video_system)) FROM task7.edition_resolved WHERE conflict_version_id = %s",
                         (t9.cv,)).fetchone()[0]
    assert len(rel) == total > 0 and all(v[1] for v in rel.values())
    located, ok, upstream, replay = rel["B0R1B|video_system|PAL"]
    pal = conn.execute("SELECT assertion_id FROM task4.field_assertion WHERE entity_id = 'B0R1B' AND field_name = "
                       "'edition.video_system' AND norm_version = 't4n1' AND parser_version = 't3p1'").fetchone()[0]
    assert [it["task4_assertion_id"] for it in located["items"]] == [pal]
    assert upstream["sections"] == replay["sections"] == ["detail_bullets"]
    child = assertion_id("task7.edition_resolved", f"{t9.cv}|B0R1B|video_system|PAL")
    assert [(p[0], p[1], p[2]) for p in _relocate_parents(conn, dk, child)] == \
        [(assertion_id("task4.field_assertion", str(pal)), "replay:feature_values", True)]


def test_ac08_second_earliest_review_id_relocated(conn, t9):
    """Legend：任务 6 只保存第二早评论的时间；重放按任务 6 的同一排序补出它的源评论 ID（LU2），与上游时间、B3 一致。"""
    _sk, ak, dk = _keys(conn)
    lg = t9.mid("Lg")
    located, ok, upstream, replay = _relocations(conn, dk, "second_review")[lg]
    lu1, lu2 = _rid(conn, t9.eid, "LU1"), _rid(conn, t9.eid, "LU2")
    assert ok and located["earliest_source_review_id"] == lu1 and located["second_source_review_id"] == lu2
    assert located["second_asin"] == "B0Lg" and upstream["B3"] is True and replay["B3"] is True
    t6 = conn.execute("SELECT second_review_time FROM task6.movie_review_time WHERE movie_id = %s", (lg,)).fetchone()[0]
    assert located["second_review_time"] == t6.isoformat()
    bound = assertion_id("task6.inferred_release_bound", f"{h.h7.RV}|{lg}")
    edge = conn.execute("SELECT role, relocated, detail FROM task9.derivation WHERE stage_key = %s AND child_id = %s "
                        "AND parent_id = %s", (dk, bound, assertion_id("public.review_staging", f"{t9.eid}|{lu2}"))
                        ).fetchone()
    assert edge == ("direct_dependency", True, {"rank": 2, "relocated_by": "replay"})


def test_ac08_earliest_two_matches_task6_order():
    """与任务 6 movie_review_times 相同的作品层排序：同一时刻按 ASIN；商品内的第二条可以是作品的第二早。"""
    t = lambda d: datetime(2001, 1, d, tzinfo=timezone.utc)   # noqa: E731
    rows = [("B", "b1", t(1)), ("B", "b2", t(2)), ("B", "b3", t(1)), ("A", "a1", t(1)), ("A", "a2", t(5))]
    rows.sort(key=lambda r: (r[0], r[2], r[1]))
    first, second = earliest_two(rows)
    assert first == (t(1), "a1", "A") and second == (t(1), "b1", "B")
    assert earliest_two([("A", "a1", t(3))]) == ((t(3), "a1", "A"), None)


def test_ac08_replay_mismatch_kept_partial_and_upstream_unchanged(conn, make_settings, tmp_path):
    """上游值被改得与重放不一致（夹具里直接改任务 7、6 的结果行）：写 replay_mismatch，不写重放边，relocation 保留
    上游值与重放值；任务 9 运行前后上游各表不变。"""
    t = h.seed(conn, make_settings, tmp_path)
    multi = check_inputs(conn, t.inputs()).cv_stage_keys[3]
    g1, lg = t.mid("G1"), t.mid("Lg")
    section, term = conn.execute("SELECT section, term FROM task7.movie_genre_term WHERE stage_key = %s AND movie_id = %s "
                                 "ORDER BY 1, 2 LIMIT 1", (multi, g1)).fetchone()
    conn.execute("UPDATE task7.movie_genre_term SET asins = '{B0Other}' WHERE stage_key = %s AND movie_id = %s "
                 "AND section = %s AND term = %s", (multi, g1, section, term))
    conn.execute("UPDATE task7.edition_resolved SET attributes_detail = jsonb_set(attributes_detail, "
                 "'{video_system,PAL}', '[\"title\"]') WHERE conflict_version_id = %s AND asin = 'B0R1B'", (t.cv,))
    conn.execute("UPDATE task6.movie_review_time SET second_review_time = second_review_time + interval '1 day' "
                 "WHERE movie_id = %s", (lg,))
    before = h.upstream_digest(conn)
    h.run(conn, t, through="derivations")
    assert h.upstream_digest(conn) == before
    _sk, _ak, dk = _keys(conn)
    bad = {(r[0], r[1]) for r in conn.execute("SELECT target_kind, target_id FROM task9.relocation WHERE stage_key = %s "
                                              "AND NOT consistent", (dk,))}
    assert bad == {("genre_term", f"{g1}|{section}|{term}"), ("edition_feature", "B0R1B|video_system|PAL"),
                   ("second_review", lg)}
    issues = {r[0] for r in conn.execute("SELECT entity_id FROM task9.quality_issue WHERE stage_key = %s "
                                         "AND issue_code = 'replay_mismatch'", (dk,))}
    assert issues == {f"{k}:{i}" for k, i in bad}
    up, rep = conn.execute("SELECT upstream_value, replay_value FROM task9.relocation WHERE stage_key = %s AND "
                           "target_kind = 'genre_term'  AND NOT consistent", (dk,)).fetchone()
    assert up["asins"] == ["B0Other"] and rep["asins"] == ["B0G1"]
    for child in (assertion_id("task7.movie_genre_term", f"{t.cv}|{g1}|{section}|{term}"),
                  assertion_id("task7.edition_resolved", f"{t.cv}|B0R1B|video_system|PAL")):
        assert _relocate_parents(conn, dk, child) == []
    bound = assertion_id("task6.inferred_release_bound", f"{h.h7.RV}|{lg}")
    assert conn.execute("SELECT count(*) FILTER (WHERE relocated), count(*) FILTER (WHERE role IS NOT NULL) "
                        "FROM task9.derivation WHERE stage_key = %s AND child_id = %s", (dk, bound)).fetchone() == (0, 1)
    # 其余词面与特征仍一致
    assert conn.execute("SELECT count(*) FROM task9.relocation WHERE stage_key = %s AND consistent", (dk,)).fetchone()[0] > 0


# ---------------------------------------------------------------- AC09：推断上界的全部依赖评论（R22）

def _ac09_world():
    world = h.t9_world()
    mv1 = next(m for m in world.movies if m.name == "Mv1")
    mv1.release.inferred = ("2003-05-20", "day", "media_release")      # 只有发售日期证据
    return world


def test_ac09_review_bound_aggregates_all_edition_reviews(conn, make_settings, tmp_path):
    """Legend 三个 edition 商品共 7 条有效评论（B0Lg 3、B0LgB 2、B0LgD 2），合集 B0LgC 的 1 条不算；全部登记为
    aggregate 父节点，最早（LU1）与第二早（LU2）另标 direct_dependency；条件结果与算法版本在上界断言里。"""
    legend_day = (datetime(1999, 8, 23, tzinfo=timezone.utc) - datetime(2007, 6, 25, tzinfo=timezone.utc)).days
    reviews = h.t9_reviews() + [Rec("B0LgD", "LU8", time=unix(legend_day + 300), text="later unicorn review")]
    t = h.seed(conn, make_settings, tmp_path, world=_ac09_world(), reviews=reviews)
    h.run(conn, t, through="derivations")
    _sk, ak, dk = _keys(conn)
    lg = t.mid("Lg")
    bound = assertion_id("task6.inferred_release_bound", f"{h.h7.RV}|{lg}")
    edges = conn.execute("SELECT parent_id, operation, role, relocated, detail, ol_type, ol_subtype FROM task9.derivation "
                         "WHERE stage_key = %s AND child_id = %s", (dk, bound)).fetchall()
    rid = {name: _rid(conn, t.eid, name) for name in ("LU1", "LU2", "LU3", "LU4", "LU5", "LU6", "LU7", "LU8")}
    expected = {assertion_id("public.review_staging", f"{t.eid}|{rid[n]}"): n
                for n in ("LU1", "LU2", "LU3", "LU4", "LU5", "LU7", "LU8")}
    assert {e[0] for e in edges} == set(expected) and len(edges) == 7
    assert all(e[1] == "aggregate" and (e[5], e[6]) == ("DIRECT", "AGGREGATION") for e in edges)
    roles = {expected[e[0]]: (e[2], e[3], e[4]) for e in edges}
    assert roles["LU1"] == ("direct_dependency", False, {"rank": 1})
    assert roles["LU2"] == ("direct_dependency", True, {"rank": 2, "relocated_by": "replay"})
    assert all(roles[n] == (None, False, {}) for n in ("LU3", "LU4", "LU5", "LU7", "LU8"))
    assert assertion_id("public.review_staging", f"{t.eid}|{rid['LU6']}") not in {e[0] for e in edges}
    count = conn.execute("SELECT review_count FROM task6.movie_review_time WHERE movie_id = %s", (lg,)).fetchone()[0]
    assert count == 7
    value, loc, version = conn.execute("SELECT normalized_value, field_locator, extract_rule_version FROM task9.assertion "
                                       "WHERE stage_key = %s AND assertion_id = %s", (ak, bound)).fetchone()
    assert value == "1999-08-23" and version == "t6r1" and loc["algorithm"] == "etl_task6.bounds.build_bounds"
    assert loc["conditions"] == {"B1": True, "B2": True, "B3": True, "B4": True, "B5": True}
    review_ev = next(e for e in loc["evidence"] if e["evidence"] == "review")
    assert review_ev["checks"] == loc["conditions"] and review_ev["basis"]["source_review_id"] == rid["LU1"]


def test_ac09_media_release_bound_parent_is_used_assertion(conn, make_settings, tmp_path):
    """Mv1 只有商品发售日期证据：上界的父节点是所用 media_release 断言（B0E1 的 2003-05-20），边带 M1–M4。"""
    t = h.seed(conn, make_settings, tmp_path, world=_ac09_world())
    h.run(conn, t, through="derivations")
    sk, ak, dk = _keys(conn)
    mv1 = t.mid("Mv1")
    bound = assertion_id("task6.inferred_release_bound", f"{h.h7.RV}|{mv1}")
    used = conn.execute("SELECT assertion_id FROM task6.product_release_date WHERE asin = 'B0E1' AND kind = 'media_release'"
                        ).fetchone()[0]
    edges = conn.execute("SELECT parent_id, operation, rule_id, detail FROM task9.derivation WHERE stage_key = %s "
                         "AND child_id = %s", (dk, bound)).fetchall()
    assert [(e[0], e[1], e[2]) for e in edges] == [(assertion_id("task4.field_assertion", str(used)), "infer",
                                                    "media_release:M1-M4")]
    assert edges[0][3]["checks"] == {"M1": True, "M2": True, "M3": True, "M4": True}
    loc = conn.execute("SELECT field_locator FROM task9.assertion WHERE stage_key = %s AND assertion_id = %s",
                       (ak, bound)).fetchone()[0]
    assert loc["chosen_evidence"] == "media_release" and loc["algorithm"] == "etl_task6.bounds.build_bounds"
    # 发售日期断言再落到 B0E1 的页面快照
    parent = conn.execute("SELECT parent_kind, parent_id FROM task9.derivation WHERE stage_key = %s AND child_id = %s",
                          (dk, edges[0][0])).fetchone()
    assert parent[0] == "source_record" and conn.execute(
        "SELECT upstream_key FROM task9.source_record WHERE stage_key = %s AND source_record_id = %s",
        (sk, parent[1])).fetchone()[0] == f"{t.eid}|B0E1"
