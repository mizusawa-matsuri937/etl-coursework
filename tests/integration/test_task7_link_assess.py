"""任务 7 关联评估（link_assess）：四信号、错配熔断与共享 tconst 逐对分类（spec AC10、AC21）。
预期值按 spec 的 AC 表手算，夹具见 tests/fixtures/task7/README.md。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_link_assess.py
"""

from __future__ import annotations

import pytest
import _task7_helpers as h
from etl_task7 import link_assess
from etl_task7.run import SHARED_MARK, Task7InputError
from task7 import world as w

pytestmark = pytest.mark.integration

M, E, IMDB, REL = w.Movie, w.Ed, w.Imdb, w.Release


_case, _world = h.case, h.world_of


def _link(conn, ctx, s, movie: str) -> dict:
    cols = ("effective_tconst", "tconst_source", "base_reliability", "is_weak", "sig_year", "sig_runtime",
            "sig_director", "sig_title", "cross_source_mismatch_halt", "halt_reason", "signal_detail")
    row = conn.execute(f"SELECT {', '.join(cols)} FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s "
                       "AND movie_id = %s", (s.eid, ctx.inputs.stage_key("link_assess"), s.mid(movie))).fetchone()
    assert row is not None, f"作品 {movie} 没有关联评估行"
    d = dict(zip(cols, row))
    d["base_reliability"] = float(d["base_reliability"])
    return d


def _issues(conn, ctx, s, movie: str) -> dict[str, dict]:
    return {c: d for c, d in conn.execute(
        "SELECT issue_code, detail FROM task7.quality_issue WHERE extraction_id = %s AND stage_key = %s AND entity_id = %s",
        (s.eid, ctx.inputs.stage_key("link_assess"), s.mid(movie)))}


# ------------------------------------------------------------------ AC21 关联评估与错配熔断

def _ac21_world() -> w.World:
    return _world(
        # L1：弱匹配 Y ∧ RT（页面标题年份 2010 对 IMDb 1990；页面片长 150 对 100）
        _case("L1", "imdb_t6_cooccur", title_years=(2010,), page_runtime=150),
        # L2：弱匹配 Y ∧ D（导演毫无共同）
        _case("L2", "imdb_t5_hint", title_years=(2005,), imdb_dirs=(("nm0000001", "Alice Alpha"),),
              page_dirs=(("Bob Beta", None),)),
        # L3：弱匹配 T ∧ RT（页面标题与 IMDb 无关，片长差 40）
        _case("L3", "imdb_t6_cooccur", page_title="Wrong Trousers [VHS]", imdb_title="Curse of the Were-Rabbit",
              page_runtime=140),
        # L4：弱匹配，页面 John Cherry 与 IMDb John R. Cherry III 兼容，D 不亮
        _case("L4", "imdb_t5_hint", imdb_dirs=(("nm0000002", "John R. Cherry III"),), page_dirs=(("John Cherry", None),)),
        # L5：弱匹配只亮 RT
        _case("L5", "imdb_t6_cooccur", page_runtime=130, imdb_dirs=(("nm0000003", "Carl Gamma"),),
              page_dirs=(("Carl Gamma", None),)),
        # L6：强关联 Y ∧ RT：不熔断
        _case("L6", "imdb_t4_link", title_years=(2010,), page_runtime=150),
        # L7：页面人物没有 nconst，但姓名与 IMDb 严格相同
        _case("L7", "imdb_t6_cooccur", imdb_dirs=(("nm0000004", "Jane Roe"),), page_dirs=(("Jane Roe", None),)),
        # L8：页面标题带载体词 Emma VHS、尾置冠词 Departed, The，都不亮 T
        _case("L8a", "imdb_t5_hint", imdb_title="Emma", page_title="Emma VHS"),
        _case("L8b", "imdb_t5_hint", imdb_title="The Departed", page_title="Departed, The"),
        # 页面人物带 nconst 且与 IMDb 相同；评论证据比 IMDb 早 5 年（只亮 Y，弱匹配不熔断）
        _case("L9", "imdb_t6_cooccur", title_years=(), bounds=(("review", "1985-05-05"),),
              imdb_dirs=(("nm0000005", "Dora Delta"),), page_dirs=(("D. Delta", "nm0000005"),)),
        # 没有任何证据可比：全部 na
        _case("L10", "imdb_t4_link", title_years=(), page_runtime=0, imdb_runtime=None))


@pytest.fixture
def ac21(conn):
    s = h.seed(conn, _ac21_world())
    return s, h.run_stages(conn, s, upto="link_assess")


def test_ac21_weak_links_with_two_signals_are_halted(conn, ac21):
    """L1–L3：弱匹配 Y∧RT、Y∧D、T∧RT 熔断，写 task7.link_assessment，原因与证据完整。"""
    s, ctx = ac21
    l1, l2, l3 = (_link(conn, ctx, s, n) for n in ("L1", "L2", "L3"))
    assert (l1["sig_year"], l1["sig_runtime"], l1["cross_source_mismatch_halt"], l1["halt_reason"]) == ("bad", "bad", True, "Y+RT")
    assert (l2["sig_year"], l2["sig_director"], l2["cross_source_mismatch_halt"], l2["halt_reason"]) == ("bad", "bad", True, "Y+D")
    assert (l3["sig_title"], l3["sig_runtime"], l3["cross_source_mismatch_halt"], l3["halt_reason"]) == ("bad", "bad", True, "RT+T")
    assert l1["is_weak"] and (l1["tconst_source"], l1["base_reliability"]) == ("imdb_t6_cooccur", 0.75)
    assert (l2["tconst_source"], l2["base_reliability"]) == ("imdb_t5_hint", 0.80)
    # 证据完整持久化
    d1 = l1["signal_detail"]
    assert d1["year"]["lit_title_years"] == [2010] and d1["year"]["imdb_year"] == 1990
    assert d1["runtime"]["editions"] == [{"asin": "B0L1", "page": 150, "delta": 50, "cut_type": [], "discs": None, "lit": True}]
    assert d1["lit"] == ["Y", "RT"] and d1["task6_source"] == "imdb_t6_cooccur"
    assert d1["title"]["page_keys"] and d1["title"]["imdb_key_count"] == 1
    d2 = l2["signal_detail"]["director"]
    assert d2["page"][0]["names"] == ["Bob Beta"] and d2["imdb"] == [{"nconst": "nm0000001", "name": "Alice Alpha"}]
    assert d2["matches"] == []
    for movie in ("L1", "L2", "L3"):
        assert _issues(conn, ctx, s, movie).keys() == {"cross_source_mismatch_halt"}


def test_ac21_name_compatibility_does_not_light_director_signal(conn, ac21):
    """L4：John Cherry 与 John R. Cherry III 兼容；L7：页面人物没有 nconst 但姓名严格相同；L9：nconst 相同。"""
    s, ctx = ac21
    l4, l7, l9 = (_link(conn, ctx, s, n) for n in ("L4", "L7", "L9"))
    assert l4["sig_director"] == "ok" and not l4["cross_source_mismatch_halt"]
    assert l4["signal_detail"]["director"]["matches"] == [
        {"page_person_id": w.person_id("John Cherry"), "via": "name", "segment": "John Cherry", "nconst": "nm0000002", "level": "L3"}]
    assert l7["sig_director"] == "ok"
    assert l7["signal_detail"]["director"]["matches"][0]["via"] == "name" and \
        l7["signal_detail"]["director"]["matches"][0]["level"] == "L1"
    assert l9["sig_director"] == "ok"
    assert {m["via"] for m in l9["signal_detail"]["director"]["matches"]} == {"nconst", "name"}
    for movie in ("L4", "L7"):
        assert _issues(conn, ctx, s, movie) == {}


def _relax_world() -> w.World:
    return _world(
        # 真实例子：J.P.Dutta 与 J.P. Dutta 只差格式；Rakesh Chaturvedi 与 Rakesh Chaturvedi Om 是词序子集；
        # 三例都是弱匹配且 Y 亮（页面标题年份 2005 对 IMDb 1990），D 是否亮决定熔断与否
        _case("L11", "imdb_t5_hint", title_years=(2005,), imdb_dirs=(("nm0000011", "J.P. Dutta"),),
              page_dirs=(("J.P.Dutta", None),)),
        _case("L12", "imdb_t6_cooccur", title_years=(2005,), imdb_dirs=(("nm0000012", "Rakesh Chaturvedi Om"),),
              page_dirs=(("Rakesh Chaturvedi", None),)),
        # 单个词的名字不触发 LS：D 仍亮，仍熔断
        _case("L13", "imdb_t5_hint", title_years=(2005,), imdb_dirs=(("nm0000013", "Madonna Louise"),),
              page_dirs=(("Madonna", None),)),
        # 中间多出一个词（Michael Jordan 与 Michael B. Jordan）由 level 原有规则兼容（L3），t7r1 也不亮 D
        _case("L14", "imdb_t5_hint", title_years=(2005,), imdb_dirs=(("nm0000014", "Michael B. Jordan"),),
              page_dirs=(("Michael Jordan", None),)))


def test_ac21_t7r2_relaxed_name_compatibility_removes_false_d_signals_and_t7r1_is_unchanged(conn):
    """修复前（t7r1）L11、L12 的 D 亮并与 Y 一起熔断；t7r2 预规范化与 LS 后 D 不亮，只剩 Y 单信号，不熔断。
    L13（单词名字）两版都熔断；L14 两版都不亮 D。"""
    s = h.seed(conn, _relax_world())
    old = h.run_stages(conn, s, upto="link_assess", rule_version="t7r1")
    new = h.run_stages(conn, s, upto="link_assess", rule_version="t7r2")
    assert old.inputs.stage_key("link_assess") != new.inputs.stage_key("link_assess")
    for movie in ("L11", "L12"):
        o = _link(conn, old, s, movie)
        assert (o["sig_year"], o["sig_director"], o["cross_source_mismatch_halt"], o["halt_reason"]) == (
            "bad", "bad", True, "Y+D")
        n = _link(conn, new, s, movie)
        assert (n["sig_year"], n["sig_director"], n["cross_source_mismatch_halt"], n["halt_reason"]) == (
            "bad", "ok", False, None)
        assert _issues(conn, new, s, movie).keys() == {"weak_link_single_signal"}
    assert _link(conn, new, s, "L11")["signal_detail"]["director"]["matches"][0]["level"] == "L1"
    assert _link(conn, new, s, "L12")["signal_detail"]["director"]["matches"][0]["level"] == "LS"
    assert _link(conn, new, s, "L12")["signal_detail"]["director"]["matches"][0]["segment"] == "Rakesh Chaturvedi"
    for ctx in (old, new):
        l13 = _link(conn, ctx, s, "L13")
        assert (l13["sig_director"], l13["cross_source_mismatch_halt"], l13["halt_reason"]) == ("bad", True, "Y+D")
        l14 = _link(conn, ctx, s, "L14")
        assert (l14["sig_director"], l14["cross_source_mismatch_halt"]) == ("ok", False)
    assert _link(conn, new, s, "L14")["signal_detail"]["director"]["matches"][0]["level"] == "L3"


def test_ac21_single_signal_is_not_halted_but_recorded(conn, ac21):
    """L5：弱匹配只亮 RT 不熔断，继续采用 IMDb，写质量问题 weak_link_single_signal；L9 只亮 Y（评论证据早于 IMDb 年）。"""
    s, ctx = ac21
    l5 = _link(conn, ctx, s, "L5")
    assert (l5["sig_runtime"], l5["sig_year"], l5["sig_director"], l5["sig_title"]) == ("bad", "ok", "ok", "ok")
    assert l5["cross_source_mismatch_halt"] is False and l5["halt_reason"] is None
    assert _issues(conn, ctx, s, "L5") == {"weak_link_single_signal": {"tconst": l5["effective_tconst"],
                                                                         "source": "imdb_t6_cooccur", "signal": "RT"}}
    l9 = _link(conn, ctx, s, "L9")
    assert (l9["sig_year"], l9["cross_source_mismatch_halt"]) == ("bad", False)
    assert l9["signal_detail"]["year"] == {"imdb_year": 1990, "title_years": [], "bound_years": [1985],
                                           "lit_title_years": [], "lit_bound_years": [1985]}
    assert _issues(conn, ctx, s, "L9").keys() == {"weak_link_single_signal"}


def test_ac21_strong_link_never_halts(conn, ac21):
    """L6：强关联 Y∧RT 同时亮起也不熔断，也不写单信号问题。"""
    s, ctx = ac21
    l6 = _link(conn, ctx, s, "L6")
    assert (l6["sig_year"], l6["sig_runtime"]) == ("bad", "bad")
    assert (l6["is_weak"], l6["base_reliability"], l6["cross_source_mismatch_halt"], l6["halt_reason"]) == (
        False, 0.95, False, None)
    assert _issues(conn, ctx, s, "L6") == {}


def test_ac21_title_signal_ignores_carrier_words_and_trailing_articles(conn, ac21):
    """L8：页面 Emma VHS、Departed, The 对 IMDb Emma、The Departed 不亮 T。"""
    s, ctx = ac21
    for movie, key in (("L8a", "emma vhs"), ("L8b", "departed the")):
        row = _link(conn, ctx, s, movie)
        assert row["sig_title"] == "ok" and not row["cross_source_mismatch_halt"]
        assert key in row["signal_detail"]["title"]["page_keys"]
    assert _link(conn, ctx, s, "L3")["sig_title"] == "bad"


def test_ac21_no_evidence_gives_na_and_unlinked_movies_have_no_row(conn):
    """没有可比证据时信号为 na；没有有效 IMDb 关联的作品没有评估行，但实体仍被标记为已处理。"""
    cases = [_case("L10", "imdb_t4_link", title_years=(), page_runtime=0, imdb_runtime=None)]
    world = _world(*cases)
    world.movies.append(M("Loner", REL("page_reported", title_years=(1980,))))
    world.editions.append(E("B0Loner", "Loner (1980)", movie="Loner", runtime=((90, "detail_bullets"),)))
    s = h.seed(conn, world)
    ctx = h.run_stages(conn, s, upto="link_assess")
    row = _link(conn, ctx, s, "L10")
    assert (row["sig_year"], row["sig_runtime"], row["sig_director"], row["sig_title"]) == ("na", "na", "na", "ok")
    assert conn.execute("SELECT count(*) FROM task7.link_assessment WHERE movie_id = %s", (s.mid("Loner"),)).fetchone()[0] == 0
    marked = dict(conn.execute("SELECT entity_id, rows_written FROM task7.entity_stage WHERE stage = 'link_assess'"))
    assert marked[s.mid("Loner")] == 0 and marked[s.mid("L10")] == 1 and SHARED_MARK in marked


REAL_TITLES = [   # (IMDb 标题, 页面原串)：正式库里被误熔断的真实形态，页面括号说明在任务 4 规范键里已被去掉
    ("300", "300 (Limited Collector's Edition + Digital Copy) [Blu-ray]"),
    ("Ajnabee", "Ajnabee (2-Disc Set)"),
    ("Amadeus", "Amadeus (Oscar O-Sleeve)"),
    ("Sickhouse", "Sickhouse, The (DVD) (FS/WS)")]


def test_ac21_title_signal_reads_task4_normalized_key_not_recleaned_title(conn):
    """T 信号的页面键取任务 4 断言的 normalized_value（R15）：页面括号说明不进键。T ok 时再同时亮 D 也不熔断。"""
    cases = [_case(f"T{n}", "imdb_t6_cooccur", imdb_title=imdb_title, page_title=raw,
                  imdb_dirs=(("nm0000099", "Real Director"),), page_dirs=(("Other Person", None),))
             for n, (imdb_title, raw) in enumerate(REAL_TITLES)]
    s = h.seed(conn, h.world_of(*cases))
    # 夹具与任务 4 一致：normalized_value 是去掉括号说明后的规范键，不含 limited、disc、oscar 等说明词
    keys = dict(conn.execute("SELECT entity_id, normalized_value FROM task4.field_assertion WHERE field_name = 'title'"))
    assert keys["B0T0"] == "300" and keys["B0T1"] == "ajnabee" and keys["B0T2"] == "amadeus"
    ctx = h.run_stages(conn, s, upto="link_assess")
    for n, (imdb_title, raw) in enumerate(REAL_TITLES):
        row = _link(conn, ctx, s, f"T{n}")
        assert row["sig_title"] == "ok", raw
        assert row["sig_director"] == "bad" and row["cross_source_mismatch_halt"] is False, raw   # 只亮 D，不熔断
        assert row["signal_detail"]["title"]["page_keys"] == [key_of(raw)], raw


def key_of(raw: str) -> str:
    from etl_task4.normalize import split_title
    return split_title(raw).key


def test_ac21_title_signal_falls_back_to_cleaning_when_normalized_key_is_missing(conn):
    """断言的 normalized_value 为空时回退到清洁标题算法（原有行为）。"""
    s = h.seed(conn, h.world_of(_case("Fb", "imdb_t6_cooccur", imdb_title="Emma", page_title="Emma VHS")))
    conn.execute("UPDATE task4.field_assertion SET normalized_value = NULL WHERE field_name = 'title'")
    ctx = h.run_stages(conn, s, upto="link_assess")
    row = _link(conn, ctx, s, "Fb")
    assert row["sig_title"] == "ok" and row["signal_detail"]["title"]["page_keys"] == ["emma vhs"]


def test_ac21_movie_scalar_reads_the_same_assessment(conn, ac21):
    """movie_scalar 读取 link_assessment 的同一判定：熔断标记与有效 tconst 一致，已熔断作品不采用 IMDb 值。"""
    s, ctx = ac21
    ctx = h.run_stages(conn, s, upto="movie_scalar", ctx=ctx)
    for movie in ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8a", "L8b", "L9", "L10"):
        link = _link(conn, ctx, s, movie)
        row = conn.execute("SELECT cross_source_mismatch_halt, effective_tconst, tconst_source, release_year "
                           "FROM task7.movie_scalar_stage_result WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s",
                           (s.eid, ctx.inputs.stage_key("movie_scalar"), s.mid(movie))).fetchone()
        assert row[:3] == (link["cross_source_mismatch_halt"], link["effective_tconst"], link["tconst_source"]), movie
        # 已熔断的作品不采用 IMDb 年份；未熔断的采用 IMDb 年份 1990
        assert row[3] == (None if link["cross_source_mismatch_halt"] else 1990), movie


def test_ac21_requires_edition_stage_complete(conn):
    """link_assess 只读上游阶段结果：edition_resolve 没做完就确定性失败。"""
    s = h.seed(conn, _world(_case("L1", "imdb_t6_cooccur")))
    ctx = h.run_stages(conn, s, upto="edition_resolve")
    conn.execute("DELETE FROM task7.entity_stage WHERE stage = 'edition_resolve'")
    with pytest.raises(Task7InputError, match="edition_resolve"):
        link_assess.assess_links(conn, ctx, h.ruleset(ctx.inputs.rule_version))


# ------------------------------------------------------------------ AC10 共享 tconst 去重与分类

def _ac10_world() -> w.World:
    cases = [
        # P1：B 被熔断（弱匹配 Y ∧ RT）→ false_positive_link
        _case("P1a", "imdb_t4_link", tconst="tt3000001", imdb_title="Shared Picture One"),
        _case("P1b", "imdb_t6_cooccur", tconst="tt3000001", imdb_title="Shared Picture One", title_years=(2010,),
              page_runtime=150),
        # P2：标题相等、导演兼容 → likely_duplicate_movie；同一对作品有两条共享问题（候选共享 + 共现）
        _case("P2a", "imdb_t4_link", tconst="tt3000002", imdb_title="Shared Picture Two", imdb_dirs=(("nm0000011", "Eve Epsilon"),),
              page_dirs=(("Eve Epsilon", None),)),
        _case("P2b", "imdb_t5_hint", tconst="tt3000002", imdb_title="Shared Picture Two", imdb_dirs=(("nm0000011", "Eve Epsilon"),),
              page_dirs=(("E. Epsilon", None),)),
        # P3：标题相等但 B 的页面导演不兼容（只亮 D，弱匹配不熔断）→ undetermined
        _case("P3a", "imdb_t4_link", tconst="tt3000003", imdb_title="Shared Picture Three", imdb_dirs=(("nm0000012", "Fay Zeta"),),
              page_dirs=(("Fay Zeta", None),)),
        _case("P3b", "imdb_t6_cooccur", tconst="tt3000003", imdb_title="Shared Picture Three", imdb_dirs=(("nm0000012", "Fay Zeta"),),
              page_dirs=(("Gus Theta", None),)),
    ]
    imdb = {c[0].tconst: c[0] for c in cases}
    world = w.World(list(imdb.values()), [c[1] for c in cases], [c[2] for c in cases])
    world.shared = [("P1b", "tt3000001", "P1a", "candidate_shared"),
                    ("P2b", "tt3000002", "P2a", "candidate_shared"), ("P2b", "tt3000002", "P2a", "cooccur"),
                    ("P3b", "tt3000003", "P3a", "cooccur")]
    return world


def _shared_rows(conn, ctx, s):
    return {(t, m): (o, c, list(ids)) for t, m, o, c, ids in conn.execute(
        "SELECT tconst, movie_id, other_movie_id, link_class, source_issue_ids FROM task7.shared_tconst_result "
        "WHERE extraction_id = %s AND stage_key = %s", (s.eid, ctx.inputs.stage_key("link_assess")))}


def test_ac10_duplicate_issues_make_one_pair_and_three_classes(conn):
    """同一对作品的两条共享问题只生成一对；已熔断、标题相等且导演兼容、标题相等但导演不兼容分别分类；任务 4 不变。"""
    s = h.seed(conn, _ac10_world())
    before = h.upstream_digest(conn)
    t4_movies = conn.execute("SELECT movie_id, tconst, status FROM task4.movie ORDER BY movie_id").fetchall()
    ctx = h.run_stages(conn, s, upto="link_assess")
    rows = _shared_rows(conn, ctx, s)
    assert len(rows) == 3                                                    # 4 条问题 → 3 对
    assert rows[("tt3000001", s.mid("P1b"))][:2] == (s.mid("P1a"), "false_positive_link")
    assert rows[("tt3000002", s.mid("P2b"))][:2] == (s.mid("P2a"), "likely_duplicate_movie")
    assert rows[("tt3000003", s.mid("P3b"))][:2] == (s.mid("P3a"), "undetermined")
    assert len(rows[("tt3000002", s.mid("P2b"))][2]) == 2                    # 两条来源问题都被记录
    assert len(rows[("tt3000001", s.mid("P1b"))][2]) == 1
    # 依据：双方的熔断与信号
    basis = conn.execute("SELECT basis FROM task7.shared_tconst_result WHERE tconst = 'tt3000001'").fetchone()[0]
    assert basis["movie"]["halt"] is True and basis["other"]["halt"] is False
    assert sorted(basis["owners"]) == [s.mid("P1a")]
    basis3 = conn.execute("SELECT basis FROM task7.shared_tconst_result WHERE tconst = 'tt3000003'").fetchone()[0]
    assert (basis3["movie"]["sig_title"], basis3["movie"]["sig_director"]) == ("ok", "bad")
    # 任务 4 与其他上游表没有被修改（只记录，不回写）
    assert h.upstream_digest(conn) == before
    assert conn.execute("SELECT movie_id, tconst, status FROM task4.movie ORDER BY movie_id").fetchall() == t4_movies
    # 分类步骤只做一次：重跑 link_assess 不再写（实体标记）
    counts = link_assess.assess_links(conn, ctx, h.ruleset(ctx.inputs.rule_version))
    assert counts["movies"] == 0 and counts["shared"] == {}
    assert len(_shared_rows(conn, ctx, s)) == 3


def test_ac10_pairs_use_current_task6_run_only(conn):
    """共享问题只取上映版本所属运行的 shared_tconst_with_movie；其他运行或问题类型的行不进入分类。"""
    world = _ac10_world()
    s = h.seed(conn, world)
    conn.execute("INSERT INTO task6.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                 "VALUES ('t6fix', %s, %s, 'year_disagreement', '{\"tconst\": \"tt3000002\"}')", (s.eid, s.mid("P2b")))
    conn.execute("INSERT INTO task6.run (run_id, kind, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                 "parser_version, rule_version, imdb_snapshot_id, input_key, status, finished_at) "
                 "VALUES ('t6other', 'release', %s, 'map0002', 'pmap0001', 't3p1', 't6r1', 'imdb_2026-10-29', 'o', 'succeeded', now())",
                 (s.eid,))
    conn.execute("INSERT INTO task6.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                 "VALUES ('t6other', %s, %s, 'shared_tconst_with_movie', %s)",
                 (s.eid, s.mid("P3a"), '{"tconst": "tt3000003", "owner_movie_id": "%s"}' % s.mid("P3b")))
    ctx = h.run_stages(conn, s, upto="link_assess")
    assert len(_shared_rows(conn, ctx, s)) == 3
    assert ("tt3000003", s.mid("P3a")) not in _shared_rows(conn, ctx, s)
