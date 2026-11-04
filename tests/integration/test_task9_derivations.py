"""任务 9 集成层派生边 build_derivations（spec AC06、AC07；AC01 的派生边部分）。夹具见 tests/fixtures/task9/README.md；
预期值按 spec 手算或逐列对照上游行，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_derivations.py
"""

from __future__ import annotations

import pytest

import _task9_helpers as h
from etl_task9.derivations import check_graph
from etl_task9.rules import OPERATIONS, assertion_id, ol_of
from etl_task9.run import check_inputs, start_run
from task7 import world as w7


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    h.run(conn, t, through="derivations", batch_size=500)
    return t


def _keys(conn):
    return h.stage_key(conn, "sources"), h.stage_key(conn, "assertions"), h.stage_key(conn, "derivations")


def _parents(conn, dk, child):
    """child 断言的父边：[(parent_kind, parent_id, operation, rule_id, detail, role, relocated)]。"""
    return conn.execute("SELECT parent_kind, parent_id, operation, rule_id, detail, role, relocated FROM task9.derivation "
                        "WHERE stage_key = %s AND child_kind = 'assertion' AND child_id = %s ORDER BY 2, 3",
                        (dk, child)).fetchall()


def _upstream(conn, ak, aid):
    return conn.execute("SELECT upstream_table, upstream_key FROM task9.assertion WHERE stage_key = %s "
                        "AND assertion_id = %s", (ak, aid)).fetchone()


def _sources_of(conn, dk, ak, sk, aid):
    """沿派生边走到底：返回终点源记录的 (source_kind, upstream_table, upstream_key) 集合。"""
    rows = conn.execute(
        "WITH RECURSIVE up(id) AS (SELECT %s::text UNION SELECT d.parent_id FROM task9.derivation d JOIN up "
        "ON d.child_id = up.id WHERE d.stage_key = %s AND d.parent_kind = 'assertion') "
        "SELECT DISTINCT s.source_kind, s.upstream_table, s.upstream_key FROM up JOIN task9.derivation d "
        "ON d.stage_key = %s AND d.child_id = up.id AND d.parent_kind = 'source_record' "
        "JOIN task9.source_record s ON s.stage_key = %s AND s.source_record_id = d.parent_id",
        (aid, dk, dk, sk)).fetchall()
    return set(rows)


# ---------------------------------------------------------------- AC06：演职员成员（R16）

def test_ac06_aligned_director_links_page_credit_and_imdb_row(conn, t9):
    """Y2：页面导演 Sam Director（任务 5 未关联）与 IMDb nm6000001 对齐为同一人。成员断言同时连到对齐断言（页面）
    与 IMDb title.crew 行，沿边能走到 B0Y2 的页面快照与 title.crew.tsv.gz 的行。"""
    sk, ak, dk = _keys(conn)
    movie = t9.mid("Y2")
    member = conn.execute("SELECT assertion_id, member_key, rule_id FROM task9.assertion WHERE stage_key = %s "
                          "AND upstream_table = 'task7.movie_person_resolved' AND entity_id = %s AND field_name = 'director'",
                          (ak, movie)).fetchall()
    assert len(member) == 1
    aid, member_key, coverage = member[0]
    resolved = conn.execute("SELECT person_id, nconst, source_coverage FROM task7.movie_person_resolved WHERE stage_key = %s "
                            "AND movie_id = %s AND role = 'director'",
                            (check_inputs(conn, t9.inputs()).cv_stage_keys[3], movie)).fetchone()
    assert member_key == f"{resolved[0]}|director" and resolved[1] == "nm6000001" and coverage == resolved[2] == "both"
    ops = {(_upstream(conn, ak, p[1])[0], p[2]) for p in _parents(conn, dk, aid)}
    assert ("task7.credit_alignment", "align") in ops and ("imdb.title_director", "union") in ops
    align = next(p[1] for p in _parents(conn, dk, aid) if p[2] == "align")
    align_parents = {(_upstream(conn, ak, p[1])[0], p[2]) for p in _parents(conn, dk, align)}
    assert align_parents == {("task5.mention_person", "align"), ("imdb.title_director", "align")}
    found = _sources_of(conn, dk, ak, sk, aid)
    assert ("page_snapshot", "task2.product_manifest", f"{t9.eid}|B0Y2") in found
    tconst = conn.execute("SELECT effective_tconst FROM task7.movie_resolved WHERE conflict_version_id = %s "
                          "AND movie_id = %s", (t9.cv, movie)).fetchone()[0]
    assert ("imdb_row", "imdb.title_director", f"{h.SNAPSHOT}|{tconst}") in found


def test_ac06_same_person_two_roles_two_members(conn, t9):
    """C1：Es One 同时是 starring 与 cast，成员断言两行（成员键 person_id|角色），各自连到页面提及；cast 同时连到
    IMDb title.principals 行（starring 只来自页面署名行，任务 7 的 starring 成员都是 page_only）。"""
    sk, ak, dk = _keys(conn)
    pid = w7.person_id("Es One", "nm7000010")
    rows = conn.execute("SELECT field_name, member_key, assertion_id FROM task9.assertion WHERE stage_key = %s "
                        "AND upstream_table = 'task7.movie_person_resolved' AND entity_id = %s AND raw_value IS NOT NULL "
                        "AND normalized_value = %s ORDER BY 1", (ak, t9.mid("C1"), pid)).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("cast", f"{pid}|cast"), ("starring", f"{pid}|starring")]
    assert rows[0][2] != rows[1][2]
    for field, _m, aid in rows:
        parents = {(_upstream(conn, ak, p[1])[0], p[2]) for p in _parents(conn, dk, aid)}
        assert ("task5.mention_person", "union") in parents
        assert (("imdb.title_principal", "union") in parents) == (field == "cast")
        asins = {k for kind, t, k in _sources_of(conn, dk, ak, sk, aid) if kind == "page_snapshot"}
        assert asins == {f"{t9.eid}|B0C1", f"{t9.eid}|B0C1B", f"{t9.eid}|B0C1C"}


def test_ac06_multi_person_string_keeps_raw_row(conn, t9):
    """C9：页面多人串 “Alpha Beta; Gamma Delta”。对齐断言原样保留整串（raw_value），每段一行并连到页面提及；
    对上 IMDb 的段同时连到 IMDb 行。"""
    sk, ak, dk = _keys(conn)
    rows = conn.execute("SELECT assertion_id, raw_value, normalized_value, upstream_status, field_locator FROM task9.assertion "
                        "WHERE stage_key = %s AND upstream_table = 'task7.credit_alignment' AND entity_id = %s "
                        "ORDER BY (field_locator->>'segment')::int", (ak, t9.mid("C9"))).fetchall()
    up = conn.execute("SELECT raw_name, segment_name, outcome FROM task7.credit_alignment WHERE stage_key = %s "
                      "AND movie_id = %s ORDER BY segment",
                      (check_inputs(conn, t9.inputs()).cv_stage_keys[3], t9.mid("C9"))).fetchall()
    assert [(r[1], r[2], r[3]) for r in rows] == [tuple(u) for u in up] and len(rows) >= 1
    assert {r[1] for r in rows} == {"Alpha Beta; Gamma Delta"}
    for aid, *_rest, loc in rows:
        parents = {(_upstream(conn, ak, p[1])[0], p[2]) for p in _parents(conn, dk, aid)}
        assert ("task5.mention_person", "align") in parents
        if loc["nconst"]:
            assert ("imdb.title_principal", "align") in parents
        assert ("page_snapshot", "task2.product_manifest", f"{t9.eid}|B0C9") in _sources_of(conn, dk, ak, sk, aid)


# ---------------------------------------------------------------- AC06：商品 → 作品、提及 → 人物（R20）

def test_ac06_product_movie_edges_decision_and_singleton(conn, t9):
    """B0Lg（edition）与 B0LgC#1（合集成员）经任务 4 自动合并决策；B0LgB 为 singleton。"""
    sk, ak, dk = _keys(conn)
    lg = t9.mid("Lg")

    def pm(asin, ordinal):
        return assertion_id("task4.product_movie", f"{h.h7.T4}|{asin}|{ordinal}|{lg}")

    for asin, ordinal, entity in (("B0Lg", 0, "B0Lg"), ("B0LgC", 1, "B0LgC#1")):
        dec = conn.execute("SELECT decision_id, confidence FROM task4.product_movie JOIN task4.merge_decision "
                           "USING (decision_id) WHERE mapping_version_id = %s AND asin = %s AND member_ordinal = %s",
                           (h.h7.T4, asin, ordinal)).fetchone()
        parents = _parents(conn, dk, pm(asin, ordinal))
        assert [(p[1], p[2], p[3]) for p in parents] == [(assertion_id("task4.merge_decision", str(dec[0])), "link",
                                                          "auto_decision")]
        d_parents = {(_upstream(conn, ak, p[1]), p[2], p[4]["feature"]) for p in _parents(conn, dk, parents[0][1])}
        tables = {(x[0][0], x[2]) for x in d_parents}
        assert ("task4.field_assertion", "title") in tables and ("imdb.title", "title") in tables
        assert all(x[1] == "link" for x in d_parents)
        etype = conn.execute("SELECT entity_type, entity_id FROM task9.assertion WHERE stage_key = %s "
                             "AND assertion_id = %s", (ak, parents[0][1])).fetchone()
        assert etype == ("collection_member" if ordinal else "edition", entity)
    single = _parents(conn, dk, pm("B0LgB", 0))
    sr = conn.execute("SELECT source_record_id FROM task9.source_record WHERE stage_key = %s "
                      "AND upstream_key = %s", (sk, f"{t9.eid}|B0LgB")).fetchone()[0]
    assert [(p[0], p[1], p[2], p[3]) for p in single] == [("source_record", sr, "link", "singleton")]


def test_ac06_mention_person_merge_edges_carry_basis_and_round(conn, t9):
    """Es One 的三条提及：第一条经 credits（auto 外部 ID 判定 → IMDb 主要演职员行与人名行），其余经 co_credit 第 2 轮
    跨作品决策；归并边带依据、轮次与决策引用。"""
    sk, ak, dk = _keys(conn)
    pid = w7.person_id("Es One", "nm7000010")
    rows = conn.execute("SELECT mp.mention_key, mp.basis, mp.round FROM task5.mention_person mp "
                        "WHERE mp.person_mapping_version_id = %s AND mp.person_id = %s ORDER BY 1",
                        (h.h7.PMAP, pid)).fetchall()
    assert sorted(r[1] for r in rows) == ["co_credit", "co_credit", "credits"]
    for key, basis, rnd in rows:
        child = assertion_id("task5.mention_person", f"{h.h7.PMAP}|{key}")
        parents = _parents(conn, dk, child)
        merge = [p for p in parents if p[2] == "merge"]
        assert len(merge) == 1 and merge[0][3] == basis
        assert merge[0][1] == assertion_id("task5.person_mention", f"{t9.eid}|t3p1|t5n1|{key}")
        assert merge[0][4]["round"] == rnd and merge[0][4]["basis"] == basis
        if basis == "co_credit":
            assert rnd == 2 and [d["round"] for d in merge[0][4]["person_decision_ids"]] == [2]
        else:
            link = [p for p in parents if p[2] == "link"]
            assert len(link) == 1 and merge[0][4]["link_decision_ids"]
            lp = {_upstream(conn, ak, p[1])[0] for p in _parents(conn, dk, link[0][1])}
            assert lp == {"task5.person_mention", "imdb.title_principal", "imdb.name"}
        assert ("page_snapshot", "task2.product_manifest", f"{t9.eid}|{key.split(':')[0]}") in \
            _sources_of(conn, dk, ak, sk, child)


def test_ac06_genre_members_link_imdb_and_page_terms(conn, t9):
    """G1：IMDb 类型 Action、Sci-Fi 与页面词面映射的并集，每个受控词一条成员断言；IMDb 侧连 title.basics 的类型成员，
    页面侧连映射到它的词面断言（词面再经重放连到页面断言，见 test_task9_relocate）。"""
    sk, ak, dk = _keys(conn)
    movie = t9.mid("G1")
    members = conn.execute("SELECT member_key, upstream_status, assertion_id FROM task9.assertion WHERE stage_key = %s "
                           "AND upstream_table = 'task7.movie_genre' AND entity_id = %s ORDER BY 1",
                           (ak, movie)).fetchall()
    up = conn.execute("SELECT genre, source_coverage FROM task7.movie_genre WHERE stage_key = %s AND movie_id = %s "
                      "ORDER BY 1", (check_inputs(conn, t9.inputs()).cv_stage_keys[3], movie)).fetchall()
    assert [(m[0], m[1]) for m in members] == [tuple(u) for u in up]
    assert {m[1] for m in members} >= {"imdb_only", "page_only"} or {m[1] for m in members} >= {"both"}
    for genre, coverage, aid in members:
        parents = _parents(conn, dk, aid)
        kinds = {(_upstream(conn, ak, p[1])[0], p[2]) for p in parents}
        assert (("imdb.title", "union") in kinds) == (coverage in ("both", "imdb_only"))
        assert (("task7.movie_genre_term", "vocab_map") in kinds) == (coverage in ("both", "page_only"))


# ---------------------------------------------------------------- AC07：闭集、标注、无环与终止（R17–R19）

def test_ac07_operations_closed_and_annotated(conn, t9):
    _sk, _ak, dk = _keys(conn)
    rows = conn.execute("SELECT operation, ol_type, ol_subtype, count(*), count(*) FILTER (WHERE rule_id = '' OR "
                        "rule_version = '' OR upstream_task = '' OR upstream_run_id IS NULL OR occurred_at IS NULL) "
                        "FROM task9.derivation WHERE stage_key = %s GROUP BY 1, 2, 3", (dk,)).fetchall()
    assert rows
    for op, t, s, n, missing in rows:
        assert op in OPERATIONS and (t, s) == OPERATIONS[op] and missing == 0, op
    ops = {r[0] for r in rows}
    assert ops >= {"extract", "normalize", "split", "align", "link", "merge", "select", "union", "vocab_map",
                   "aggregate", "relocate"}
    with pytest.raises(ValueError, match="闭集"):
        ol_of("guess")


def test_ac07_graph_acyclic_and_every_path_reaches_source(conn, t9):
    sk, ak, dk = _keys(conn)
    g = check_graph(conn, None, edge_keys=[dk], ak=ak, sk=sk)
    assert (g["dangling"], g["no_parent"], g["cycle_nodes"]) == (0, 0, 0)
    # 每条断言沿派生边都能走到至少一条源记录
    unreached = conn.execute(
        "WITH RECURSIVE walk(start, id) AS (SELECT a.assertion_id, a.assertion_id FROM task9.assertion a "
        "WHERE a.stage_key = %(ak)s UNION SELECT w.start, d.parent_id FROM walk w JOIN task9.derivation d "
        "ON d.stage_key = %(dk)s AND d.child_id = w.id AND d.parent_kind = 'assertion') "
        "SELECT count(*) FROM task9.assertion a WHERE a.stage_key = %(ak)s AND NOT EXISTS (SELECT 1 FROM walk w "
        "JOIN task9.derivation d ON d.stage_key = %(dk)s AND d.child_id = w.id AND d.parent_kind = 'source_record' "
        "WHERE w.start = a.assertion_id)", {"ak": ak, "dk": dk}).fetchone()[0]
    assert unreached == 0
    assert conn.execute("SELECT count(*) FROM task9.quality_issue WHERE stage_key = %s", (dk,)).fetchone()[0] == 0


def test_ac07_injected_cycle_and_dangling_edge_detected(conn, t9):
    sk, ak, dk = _keys(conn)
    a, b = conn.execute("SELECT child_id, parent_id FROM task9.derivation WHERE stage_key = %s AND operation = 'split' "
                        "LIMIT 1", (dk,)).fetchone()
    run = conn.execute("SELECT run_id FROM task9.run ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    ins = ("INSERT INTO task9.derivation (stage_key, child_kind, child_id, parent_kind, parent_id, operation, ol_type, "
           "ol_subtype, rule_id, rule_version, upstream_task, run_id) VALUES (%s, 'assertion', %s, %s, %s, 'normalize', "
           "'DIRECT', 'TRANSFORMATION', 'injected', 'test', 'task9', %s)")
    conn.execute(ins, (dk, b, "assertion", a, run))                       # 环：a ← b ← a
    conn.execute(ins, (dk, a, "assertion", "as_0000000000000000", run))   # 悬空父断言
    conn.execute(ins, (dk, a, "source_record", "sr_0000000000000000", run))   # 悬空父源记录
    g = check_graph(conn, None, edge_keys=[dk], ak=ak, sk=sk)
    assert g["dangling"] == 2 and g["cycle_nodes"] >= 2
    assert {a, b} <= set(_cycle(conn, dk))
    # 没有任何父边的断言
    conn.execute("DELETE FROM task9.derivation WHERE stage_key = %s AND child_id = %s AND operation = 'extract'",
                 (dk, conn.execute("SELECT child_id FROM task9.derivation WHERE stage_key = %s AND operation = 'extract' "
                                   "AND child_id NOT IN (%s, %s) LIMIT 1", (dk, a, b)).fetchone()[0]))
    assert check_graph(conn, None, edge_keys=[dk], ak=ak, sk=sk)["no_parent"] == 1
    # 操作类型不在闭集：表约束拒绝
    with pytest.raises(Exception, match="operation"):
        conn.execute("INSERT INTO task9.derivation (stage_key, child_kind, child_id, parent_kind, parent_id, operation, "
                     "ol_type, ol_subtype, rule_id, rule_version, upstream_task, run_id) VALUES (%s, 'assertion', 'x', "
                     "'assertion', 'y', 'guess', 'DIRECT', 'IDENTITY', 'r', 'v', 'task9', %s)", (dk, run))


def _cycle(conn, dk):
    from etl_task9.derivations import _cycle_nodes
    return _cycle_nodes(conn, [dk])


def test_ac07_resume_after_failure_matches_single_run(conn, make_settings, tmp_path):
    """第 3 批提交前注入故障：已提交批次保留，续跑后与一次跑完的派生边、重放结果完全相同（R38）。"""
    t = h.seed(conn, make_settings, tmp_path)
    h.run(conn, t, through="assertions")

    def boom(stage, batch_no):
        if stage == "derivations" and batch_no == 3:
            raise RuntimeError("注入故障")

    with pytest.raises(RuntimeError, match="注入故障"):
        h.run(conn, t, through="derivations", batch_size=1500, before_commit=boom)
    dk = h.stage_key(conn, "derivations")
    partial = conn.execute("SELECT count(*) FROM task9.derivation WHERE stage_key = %s", (dk,)).fetchone()[0]
    assert partial > 0
    out = h.run(conn, t, through="derivations", batch_size=1500)
    assert not out["derivations"].get("skipped")
    resumed = sorted(conn.execute("SELECT child_id, parent_id, operation, rule_id, role, relocated FROM task9.derivation "
                                  "WHERE stage_key = %s", (dk,)).fetchall())
    reloc = sorted(conn.execute("SELECT target_kind, target_id, consistent, located FROM task9.relocation "
                                "WHERE stage_key = %s", (dk,)).fetchall(), key=str)
    conn.execute("DELETE FROM task9.derivation WHERE stage_key = %s", (dk,))
    conn.execute("DELETE FROM task9.relocation WHERE stage_key = %s", (dk,))
    conn.execute("DELETE FROM task9.quality_issue WHERE stage_key = %s", (dk,))
    conn.execute("DELETE FROM task9.entity_stage WHERE stage_key = %s", (dk,))
    h.run(conn, t, through="derivations")
    assert sorted(conn.execute("SELECT child_id, parent_id, operation, rule_id, role, relocated FROM task9.derivation "
                               "WHERE stage_key = %s", (dk,)).fetchall()) == resumed
    assert sorted(conn.execute("SELECT target_kind, target_id, consistent, located FROM task9.relocation "
                               "WHERE stage_key = %s", (dk,)).fetchall(), key=str) == reloc


def test_ac01_derivations_upstream_unchanged_rerun_skips(conn, make_settings, tmp_path):
    """同输入重跑：派生阶段跳过、行不变；运行前后上游各 schema 行数与摘要不变（AC01 的派生边部分）。"""
    t = h.seed(conn, make_settings, tmp_path)
    h.run(conn, t, through="assertions")
    before = h.upstream_digest(conn)
    h.run(conn, t, through="derivations")
    assert h.upstream_digest(conn) == before
    dk = h.stage_key(conn, "derivations")
    snap = conn.execute("SELECT count(*), md5(string_agg(child_id || parent_id || operation, ',' ORDER BY child_id, "
                        "parent_id, operation)) FROM task9.derivation WHERE stage_key = %s", (dk,)).fetchone()
    out = h.run(conn, t, through="derivations")
    assert out["derivations"]["skipped"]
    assert conn.execute("SELECT count(*), md5(string_agg(child_id || parent_id || operation, ',' ORDER BY child_id, "
                        "parent_id, operation)) FROM task9.derivation WHERE stage_key = %s", (dk,)).fetchone() == snap
    # 评分统计不逐条物化依赖评论（R05）：派生边里没有任务 8 统计表
    assert conn.execute("SELECT count(*) FROM task9.assertion WHERE upstream_table LIKE 'task8.%%'").fetchone()[0] == 0


def test_ac01_derivations_require_assertions_stage(conn, make_settings, tmp_path):
    from etl_task9.derivations import build_derivations
    from etl_task9.run import Task9InputError
    t = h.seed(conn, make_settings, tmp_path)
    h.run(conn, t, through="sources")
    ctx = start_run(conn, check_inputs(conn, t.inputs()), "stage")
    with pytest.raises(Task9InputError, match="assertions"):
        build_derivations(conn, ctx)


def test_ac06_starring_member_reuses_cast_alignment(conn, make_settings, tmp_path):
    """页面主演 Garey Starling（任务 5 未关联）在任务 7 里按 cast 角色对齐到 IMDb 的 Gary Starling；starring 成员沿用
    这次对齐（正式库 502 例）。成员断言连到本角色的页面提及，并经对齐断言连到 IMDb 行。"""
    world = h.t9_world()
    imdb, movie, ed = h.h7.case("St", "imdb_t4_link")
    imdb.principals = (("nm7100099", "Gary Starling", "actor"),)
    movie.credits = (w7.Credit("Garey Starling", "starring", None, (("B0St", 1),)),)
    world.imdb.append(imdb)
    world.movies.append(movie)
    world.editions.append(ed)
    t = h.seed(conn, make_settings, tmp_path, world=world)
    h.run(conn, t, through="derivations")
    sk, ak, dk = _keys(conn)
    rows = conn.execute("SELECT field_name, assertion_id, field_locator FROM task9.assertion WHERE stage_key = %s "
                        "AND upstream_table = 'task7.movie_person_resolved' AND entity_id = %s ORDER BY 1",
                        (ak, t.mid("St"))).fetchall()
    page_pid = w7.person_id("Garey Starling")
    starring = [r for r in rows if r[0] == "starring"]
    assert len(starring) == 1 and starring[0][2]["resolution_note"]["page_person_ids"] == [page_pid]
    assert starring[0][2]["person_id"] != page_pid                       # 成员是对齐后的 IMDb 人物
    parents = {(_upstream(conn, ak, p[1])[0], p[2]) for p in _parents(conn, dk, starring[0][1])}
    assert ("task5.mention_person", "union") in parents and ("task7.credit_alignment", "align") in parents
    found = _sources_of(conn, dk, ak, sk, starring[0][1])
    assert ("page_snapshot", "task2.product_manifest", f"{t.eid}|B0St") in found
    assert any(x[1] == "imdb.title_principal" for x in found)
    g = check_graph(conn, None, edge_keys=[dk], ak=ak, sk=sk)
    assert (g["dangling"], g["no_parent"], g["cycle_nodes"]) == (0, 0, 0)
