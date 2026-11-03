"""任务 9 的只读查询 field、product、review、source、person（spec AC11–AC14）。
夹具见 tests/fixtures/task9/README.md；预期值逐列对照上游表，或在测试里沿派生边手工追踪，不由查询实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py
"""

from __future__ import annotations

from collections import defaultdict, deque

import pytest

import _task8_helpers as h8
import _task9_helpers as h
from etl_task9.query import query_field, query_person, query_product, query_review, query_source
from etl_task9.run import RunInputs
from etl_task9.versions import Keys, resolve
from task7 import world as w7


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    h.add_products(conn, t)
    h.run(conn, t, through="impact", batch_size=500)
    t.keys = resolve(conn, None, t.inputs(), need="impact")
    return t


def _srcs(tree_evidence, kind):
    return [e for e in tree_evidence if e["source_kind"] == kind]


# ---------------------------------------------------------------- AC11：field（R28）

def test_ac11_field_release_year_candidates_evidence_and_reason(conn, t9):
    """Legend 的 release_year：最终值与 task7 一致；采用候选是 IMDb 1985，证据为 title.basics 的行号、文件 SHA-256 与
    快照下载时间；两条未采用的页面标题年份 1986 保留 rejected 与原因，证据为各自的页面快照正文哈希与 fetched_at；
    路径带各步的操作、rule_id 与上游运行；另列推断上界。"""
    mid = t9.mid("Lg")
    out = query_field(conn, t9.keys, mid, "release_year")
    assert out["members"] == 1
    s = out["selections"][0]
    final, status, rule, reason = conn.execute(
        "SELECT r.release_year::text, s.status, s.selection_rule, s.reason FROM task7.movie_resolved r "
        "JOIN task7.movie_field_selection s ON s.conflict_version_id = r.conflict_version_id AND s.movie_id = r.movie_id "
        "AND s.field_name = 'release_year' WHERE r.conflict_version_id = %s AND r.movie_id = %s", (t9.cv, mid)).fetchone()
    assert (s["value_text"], s["status"], s["decision_rule"], s["reason"]) == (final, status, rule, reason) == \
        ("1985", "resolved", rule, reason)
    assert s["trace_status"] == "traced" and s["decision_version"] == t9.cv
    tconst, line = conn.execute("SELECT m.tconst, t.line_no FROM task4.movie m JOIN imdb.title t ON t.snapshot_id = %s "
                                "AND t.tconst = m.tconst WHERE m.mapping_version_id = %s AND m.movie_id = %s",
                                (h.SNAPSHOT, h.h7.T4, mid)).fetchone()
    imdb = _srcs(s["evidence"], "imdb_row")
    assert [(e["locator"]["file"], e["locator"]["tconst"], e["locator"]["line_no"]) for e in imdb] == \
        [("title.basics.tsv.gz", tconst, line)]
    assert imdb[0]["content_hash"] == h.file_sha256("title.basics.tsv.gz")
    assert imdb[0]["acquired_at_basis"] == "imdb.snapshot.downloaded_at" and imdb[0]["acquired_at"] is not None
    assert imdb[0]["depth"] >= 2 and imdb[0]["operations"][0].startswith("select:")
    edge = s["path"]["edges"][0]
    assert edge["operation"] == "select" and edge["upstream_task"] == "task7" and edge["upstream_run_id"]
    # 未采用：任务 7 的 rejected 候选，原因原样；证据为页面快照（ASIN、正文哈希、fetched_at）
    up = conn.execute("SELECT c.normalized_value, c.status, c.status_reason FROM task7.field_candidate c "
                      "JOIN task7.movie_field_selection s ON s.conflict_version_id = %s AND s.movie_id = %s "
                      "AND s.field_name = 'release_year' AND c.candidate_id = ANY(s.considered_candidate_ids) "
                      "AND NOT c.candidate_id = ANY(s.selected_candidate_ids) ORDER BY 1, 2", (t9.cv, mid)).fetchall()
    got = sorted((c["candidate"]["normalized_value"], c["candidate"]["upstream_status"], c["candidate"]["upstream_reason"])
                 for c in s["not_selected"])
    assert got == sorted(tuple(r) for r in up) and {r[1] for r in up} == {"rejected"}
    manifest = dict((a, (ch, f)) for a, ch, f in conn.execute(
        "SELECT asin, content_hash, fetched_at FROM task2.product_manifest WHERE extraction_id = %s", (t9.eid,)))
    for c in s["not_selected"]:
        pages = _srcs(c["evidence"], "page_snapshot")
        assert pages, c
        for e in pages:
            assert (e["content_hash"], e["acquired_at"]) == manifest[e["locator"]["asin"]]
            assert e["evidence_status"] == "ok"
    bound = out["related_inferred_upper_bound"]
    assert (bound["value_text"], bound["status"]) == ("1999-08-23", "inferred")


def test_ac11_field_director_members_page_and_imdb_evidence(conn, t9):
    """Y2 的 director：每个成员一条选择，与 task7.movie_person_resolved 一致；对齐的成员同时列出页面快照与
    IMDb title.crew 行的证据。"""
    mid = t9.mid("Y2")
    out = query_field(conn, t9.keys, mid, "director")
    up = conn.execute("SELECT person_id, nconst, source_coverage FROM task7.movie_person_resolved WHERE stage_key = %s "
                      "AND movie_id = %s AND role = 'director' ORDER BY person_id",
                      (t9.keys.inputs.cv_stage_keys[3], mid)).fetchall()
    assert [s["member_key"] for s in out["selections"]] == [r[0] for r in up] and out["members"] == len(up)
    for s, (pid, nconst, coverage) in zip(out["selections"], up):
        kinds = {e["source_kind"] for e in s["evidence"]}
        if coverage == "both":
            assert kinds == {"page_snapshot", "imdb_row"}
            crew = [e for e in s["evidence"] if e["source_kind"] == "imdb_row"]
            assert any(e["locator"]["file"] == "title.crew.tsv.gz" and nconst in e["locator"]["nconsts"] for e in crew)
            assert any(e["locator"].get("asin") == "B0Y2" for e in s["evidence"])
        assert s["not_selected"] == []


# ---------------------------------------------------------------- AC12：product 与 review（R29）

def test_ac12_product_edition_collection_and_not_collected(conn, t9):
    """电影单品 B0Lg、合集 B0LgC 与未采集商品 B0NC：采集状态、类别、版本属性、作品与关系类型、匹配状态与上游表
    一致；未采集商品返回 not_collected 而不报错。"""
    lg = query_product(conn, t9.keys, "B0Lg")
    assert (lg["collection"], lg["collection_status"]) == ("collected", "success")
    assert lg["category"]["category"] == "movie" and lg["category"]["category_rule_id"] == "MV1"
    assert lg["edition"]["edition_title"] == conn.execute(
        "SELECT edition_title FROM task7.edition_resolved WHERE conflict_version_id = %s AND asin = 'B0Lg'",
        (t9.cv,)).fetchone()[0]
    assert [(m["movie_id"], m["relation_type"], m["movie_basis"]) for m in lg["movies"]] == \
        [(t9.mid("Lg"), "edition", "auto_decision")]
    assert lg["match_status"] == "merged" and lg["source_record"]["source_kind"] == "page_snapshot"
    assert {(s["field_name"], s["status"]) for s in lg["selections"]} >= {("category", "resolved"),
                                                                         ("edition_title", "resolved")}
    coll = query_product(conn, t9.keys, "B0LgC")
    assert coll["product_kind"] == "collection" and coll["match_status"] == "collection_split"
    assert [(m["movie_id"], m["relation_type"]) for m in coll["movies"]] == [(t9.mid("Lg"), "contains")]
    nc = query_product(conn, t9.keys, h.NOT_COLLECTED)
    assert (nc["collection"], nc["collection_status"]) == ("not_collected", "not_collected")
    assert nc["category"]["category"] == "pending" and nc["movies"] == [] and nc["edition"] is None
    assert nc["source_record"] is None and nc["selections"] == []
    assert query_product(conn, t9.keys, "B0NOPE")["collection"] == "not_in_manifest"


def test_ac12_review_matches_view_states(conn, make_settings, tmp_path):
    """任务 8 小世界（同 AC04）：有效、隔离、不完整评论的 query review 结果与视图一致，D3 复制返回逻辑评论、层次与
    家族与作品，只在合集上的评论没有作品。"""
    eid = h8.seed_small_world(conn, make_settings, tmp_path)
    h8.execute(conn, eid)
    from etl_task9.run import apply_task9_schema
    apply_task9_schema(conn)
    keys = Keys(RunInputs(extraction_id=eid, review_dedup_version_id="rd0001"), *(["none"] * 5))

    def review(user):
        sid = conn.execute("SELECT source_review_id FROM public.review_record WHERE extraction_id = %s "
                           "AND reviewer_id_observed = %s ORDER BY record_no LIMIT 1", (eid, user)).fetchone()[0]
        return query_review(conn, keys, sid)

    eu1 = review("EU1")
    assert (eu1["mapping"]["mapping_status"], eu1["mapping"]["match_level"]) == ("mapped", "representative")
    assert eu1["record"]["raw_sha256"] and eu1["record"]["start_offset"] is not None
    q = review("EU13")
    assert q["mapping"]["mapping_status"] == "quarantined" and q["mapping"]["logical_review_id"] is None
    assert review("EU12")["mapping"]["mapping_status"] == "incomplete"
    d3 = review("EL1")
    assert d3["mapping"]["logical_review_id"] and d3["mapping"]["family_id"]
    assert [m["movie_id"] for m in d3["movies"]] == [h8.mid("E1")]
    coll = review("EL2")
    assert coll["mapping"]["asin"] == "B0000E2EC1" and coll["movies"] == []


def test_ac12_review_bound_dependency_in_fixture(conn, t9):
    """Legend 最早评论 LU1：返回文件定位与源记录，并标出它是 Legend 推断上界的直接依赖。"""
    sid = conn.execute("SELECT source_review_id FROM public.review_record WHERE extraction_id = %s "
                       "AND reviewer_id_observed = 'LU1'", (t9.eid,)).fetchone()[0]
    out = query_review(conn, t9.keys, sid)
    assert out["source_record"]["source_kind"] == "review_record"
    assert [(d["movie_id"], d["operation"], d["role"]) for d in out["bound_dependencies"]] == \
        [(t9.mid("Lg"), "aggregate", "direct_dependency")]


# ---------------------------------------------------------------- AC13：source（R30）

def _manual_closure(conn, keys, sources):
    """手工沿派生边追踪：从源记录沿子边向下走到字段选择，记最短路径边数。"""
    children = defaultdict(list)
    for ck, cid, pk, pid in conn.execute("SELECT child_kind, child_id, parent_kind, parent_id FROM task9.derivation "
                                         "WHERE stage_key = ANY(%s)", ([keys.derivations, keys.selections],)):
        children[(pk, pid)].append((ck, cid))
    best = {}
    for s in sources:
        seen = {("source_record", s): 0}
        dq = deque([("source_record", s)])
        while dq:
            node = dq.popleft()
            for ch in children.get(node, ()):
                ck = "assertion" if ch[0] == "assertion" else "selection"
                if ck == "selection":
                    d = seen[node] + 1
                    best[ch[1]] = min(best.get(ch[1], d), d)
                    continue
                if ch not in seen:
                    seen[ch] = seen[node] + 1
                    dq.append(ch)
    return best


@pytest.mark.parametrize("ref", ["asin", "tconst", "nconst", "source_review_id"])
def test_ac13_source_affected_fields_match_manual_trace(conn, t9, ref):
    """页面快照（B0Lg）、IMDb tconst（Legend）、人物 nconst（Y2 的导演 nm6000001）与评论（LU1）：query source 列出的
    受影响选择与路径长度，等于测试里沿派生边手工追踪的结果。"""
    value = {"asin": "B0Lg", "nconst": "nm6000001",
             "tconst": conn.execute("SELECT tconst FROM task4.movie WHERE mapping_version_id = %s AND movie_id = %s",
                                    (h.h7.T4, t9.mid("Lg"))).fetchone()[0],
             "source_review_id": conn.execute("SELECT source_review_id FROM public.review_record WHERE extraction_id = %s "
                                              "AND reviewer_id_observed = 'LU1'", (t9.eid,)).fetchone()[0]}[ref]
    out = query_source(conn, t9.keys, limit=10_000, **{ref: value})
    assert out["source_records"], out
    expected = _manual_closure(conn, t9.keys, [s["source_record_id"] for s in out["source_records"]])
    got = {s["selection_id"]: s["depth"] for s in out["selections"]}
    assert got == expected and out["affected_selections"] == len(expected) > 0
    if ref == "asin":
        assert {"edition.edition_title", "edition.category", "product_movie.movie_id"} <= set(out["by_field"])
        assert [r["task"] for r in out["rerun_if_changed"]][:5] == ["task3", "task4", "task5", "task6", "task7"]
    if ref == "tconst":
        assert {"movie.release_year", "movie.title"} <= set(out["by_field"])
    if ref == "source_review_id":
        assert set(out["by_field"]) == {"movie.inferred_upper_bound"}


# ---------------------------------------------------------------- AC14：person（R31）

def test_ac14_person_credits_merge_basis_external_id_and_works(conn, t9):
    """Es One（credits 一条、co_credit 第 2 轮两条提及，带外部 ID）：返回规范名来源、每条署名的页面定位与原值、
    外部 ID 证据（IMDb 人名行）、归并依据与轮次、参与作品与角色，均与任务 5、7 的表一致。"""
    pid = w7.person_id("Es One", "nm7000010")
    out = query_person(conn, t9.keys, pid)
    pmap = h.h7.PMAP
    canon, source = conn.execute("SELECT canonical_name, canonical_source FROM task5.person "
                                 "WHERE person_mapping_version_id = %s AND person_id = %s", (pmap, pid)).fetchone()
    assert out["canonical_name"]["value_text"] == canon and out["canonical_name"]["reason"] == source
    assert out["canonical_name"]["evidence"], out["canonical_name"]
    mentions = conn.execute("SELECT mp.mention_key, mp.basis, mp.round, m.raw_value FROM task5.mention_person mp "
                            "JOIN task5.person_mention m ON m.extraction_id = mp.extraction_id "
                            "AND m.parser_version = mp.parser_version AND m.norm_version = mp.norm_version "
                            "AND m.mention_key = mp.mention_key WHERE mp.person_mapping_version_id = %s "
                            "AND mp.person_id = %s ORDER BY 1", (pmap, pid)).fetchall()
    assert [(c["mention_key"], c["basis"], c["round"], c["raw_value"]) for c in out["credits"]] == \
        [tuple(r) for r in mentions]
    assert sorted(c["basis"] for c in out["credits"]) == ["co_credit", "co_credit", "credits"]
    for c in out["credits"]:
        assert c["page_locator"] and any(e["source_kind"] == "page_snapshot" for e in c["evidence"])
        merge = [e for e in c["merge_edges"] if e["operation"] == "merge"]
        assert merge and merge[0]["rule_id"] == c["basis"] and merge[0]["detail"]["round"] == c["round"]
    ext = conn.execute("SELECT system, external_id FROM task5.person_external_id WHERE person_mapping_version_id = %s "
                       "AND person_id = %s", (pmap, pid)).fetchall()
    assert [(s["member_key"], s["value_text"]) for s in out["external_ids"]] == [tuple(r) for r in ext] == \
        [("imdb", "nm7000010")]
    names = [e for e in out["external_ids"][0]["evidence"] if e["locator"].get("table") == "imdb.name"]
    assert names and names[0]["locator"]["nconst"] == "nm7000010"
    aliases = conn.execute("SELECT alias FROM task5.person_alias WHERE person_mapping_version_id = %s "
                           "AND person_id = %s ORDER BY alias", (pmap, pid)).fetchall()
    assert sorted(a["member_key"] for a in out["aliases"]) == [r[0] for r in aliases]
    works = conn.execute("SELECT movie_id, role FROM task7.movie_person_resolved WHERE stage_key = %s AND person_id = %s "
                         "ORDER BY 1, 2", (t9.keys.inputs.cv_stage_keys[3], pid)).fetchall()
    assert [(w["movie_id"], w["role"]) for w in out["works"]] == [tuple(r) for r in works]
    assert ("starring" in {w["role"] for w in out["works"]}) and all(w["trace_status"] == "traced" for w in out["works"])
