"""任务 7 作品层标量字段（movie_scalar）：范围与输入版本、候选与状态语义、标题与别名、上映年份决策表、片长差异、
内容分级（spec AC01–AC06 的作品层部分）。预期值按 spec 的 AC 表手算，夹具见 tests/fixtures/task7/README.md。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py
"""

from __future__ import annotations

from datetime import date

import pytest
import _task7_helpers as h
from etl_task7.rules import CANDIDATE_STATUSES, CONFLICT_TYPES, SELECTION_STATUSES
from task7 import world as w

pytestmark = pytest.mark.integration

M, E, IMDB, REL = w.Movie, w.Ed, w.Imdb, w.Release
case, world_of = h.case, h.world_of


def _run(conn, world):
    s = h.seed(conn, world)
    return s, h.run_stages(conn, s, upto="movie_scalar")


def _result(conn, ctx, s, movie: str) -> dict:
    cols = ("title", "original_title", "release_year", "release_precision", "runtime_minutes", "mpaa_rating",
            "effective_tconst", "tconst_source", "cross_source_mismatch_halt", "inferred_upper_bound",
            "inferred_bound_precision", "inferred_bound_evidence")
    row = conn.execute(f"SELECT {', '.join(cols)} FROM task7.movie_scalar_stage_result WHERE extraction_id = %s "
                       "AND stage_key = %s AND movie_id = %s",
                       (s.eid, ctx.inputs.stage_key("movie_scalar"), s.mid(movie))).fetchone()
    assert row is not None, movie
    return dict(zip(cols, row))


def _cands(conn, ctx, s, movie: str, field: str) -> list[dict]:
    return h.candidates(conn, ctx, "movie_scalar", s.mid(movie), field)


def _issue_codes(conn, ctx, s, movie: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT issue_code FROM task7.quality_issue WHERE extraction_id = %s AND entity_id = %s AND stage_key = %s",
        (s.eid, s.mid(movie), ctx.inputs.stage_key("movie_scalar")))}


def _diffs(conn, ctx, s, movie: str) -> dict[str, tuple]:
    return {asin: (cls, formula, pal, delta) for asin, cls, formula, pal, delta in conn.execute(
        "SELECT asin, diff_class, pal_formula_match, has_pal_evidence, delta FROM task7.runtime_diff "
        "WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s",
        (s.eid, ctx.inputs.stage_key("movie_scalar"), s.mid(movie)))}


# ------------------------------------------------------------------ AC01 输入版本、实体边界与处理范围（作品层）

def _scope_world(decoys: bool = True) -> w.World:
    imdb = [IMDB("tt1000001", "Verified Film", year=1990, runtime=100),
            IMDB("tt1000002", "Hinted Film", year=1991, runtime=95),
            IMDB("tt1000003", "Cooccur Film", year=1992, runtime=90),
            IMDB("tt1000004", "Member One", year=1995)]
    movies = [M("Verified", REL("imdb_t4_link", "tt1000001")), M("Hinted", REL("imdb_t5_hint", "tt1000002")),
              M("Cooccur", REL("imdb_t6_cooccur", "tt1000003")), M("PageOnly", REL("page_reported", title_years=(1980,))),
              M("Unknown"), M("MemberOnly", REL("imdb_t4_link", "tt1000004"))]
    eds = [E("B0V", "Verified Film [DVD]", movie="Verified", runtime=((100, "detail_bullets"),)),
           E("B0H", "Hinted Film [VHS]", movie="Hinted", runtime=((95, "detail_bullets"),)),
           E("B0C", "Cooccur Film", movie="Cooccur", runtime=((90, "product_overview"),)),
           E("B0P", "Page Only (1980)", movie="PageOnly", runtime=((88, "detail_bullets"),)),
           E("B0U", "Unknown Film", movie="Unknown"),
           E("B0COL", "Double Feature Box Set [DVD]", kind="collection", runtime=((190, "detail_bullets"),),
             rating=(("R (Restricted)", "detail_bullets"),), members=((1, "MemberOnly", "Member One"),)),
           E("B0EMPTY", "Mystery Box Set", kind="collection"),
           E("B0TV", "Some Series Season 1", kind="tv_series")]
    return w.World(imdb, movies, eds, decoys=decoys)


def test_ac01_every_movie_has_exactly_one_stage_row_and_status_per_release_state(conn):
    """每部作品（含五类上映状态与只经 contains 出现的成员）恰好一行；只读 map0002、rv0001、t4n1、t3p1；tv_series 不进入。"""
    s, ctx = _run(conn, _scope_world())
    assert conn.execute("SELECT count(*), count(DISTINCT movie_id) FROM task7.movie_scalar_stage_result WHERE extraction_id = %s "
                        "AND stage_key = %s", (s.eid, ctx.inputs.stage_key("movie_scalar"))).fetchone() == (6, 6)
    assert conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = 'map0002'").fetchone()[0] == 6
    years = {m: h.movie_sel(conn, ctx, s, m, "release_year") for m in ("Verified", "Hinted", "Cooccur", "PageOnly", "Unknown", "MemberOnly")}
    assert {m: (v["status"], v["winning_value"]) for m, v in years.items()} == {
        "Verified": ("resolved", "1990"), "Hinted": ("resolved", "1991"), "Cooccur": ("resolved", "1992"),
        "PageOnly": ("resolved", "1980"), "Unknown": ("missing", None), "MemberOnly": ("resolved", "1995")}
    # 干扰版本（rv0002 年份整体 +9、旧 IMDb 快照、t4n0/t3p0 断言）没有进入结果
    assert _result(conn, ctx, s, "Verified")["release_year"] == 1990 and _result(conn, ctx, s, "Verified")["runtime_minutes"] == 100
    assert _result(conn, ctx, s, "Verified")["title"] == "Verified Film"
    assert not conn.execute("SELECT count(*) FROM task7.field_candidate WHERE raw_value LIKE '%%Decoy%%' OR "
                            "normalized_value IN ('999', '888')").fetchone()[0]
    # tv_series 与盒子商品没有作品行；合集 B0COL 的 contains 成员有自己的行
    assert conn.execute("SELECT count(*) FROM task7.field_candidate WHERE source_ref::text LIKE '%%B0TV%%'").fetchone()[0] == 0


def test_ac01_three_imdb_link_classes_carry_their_reliability(conn):
    """三类关联均按各自可靠度（0.95、0.80、0.75）引入 IMDb 候选，年份候选衔接任务 6 的四类来源。"""
    s, ctx = _run(conn, _scope_world())
    for movie, source, rel in (("Verified", "imdb_t4_link", 0.95), ("Hinted", "imdb_t5_hint", 0.80),
                               ("Cooccur", "imdb_t6_cooccur", 0.75)):
        title = _cands(conn, ctx, s, movie, "title")
        imdb = [c for c in title if c["source"] == source]
        assert len(imdb) == 1 and imdb[0]["match_reliability"] == rel and imdb[0]["status"] == "adopted"
        assert imdb[0]["source_ref"]["snapshot"] == "imdb_2026-10-29" and imdb[0]["source_ref"]["table"] == "imdb.title"
        runtime = [c for c in _cands(conn, ctx, s, movie, "runtime_minutes") if c["source"] == source]
        assert runtime and runtime[0]["match_reliability"] == rel
        yc = [c for c in _cands(conn, ctx, s, movie, "release_year") if c["source"] == source]
        assert yc and yc[0]["match_reliability"] == rel and yc[0]["source_ref"]["task6_candidate_id"]
        assert _result(conn, ctx, s, movie)["tconst_source"] == source
    page = _cands(conn, ctx, s, "PageOnly", "release_year")
    assert [(c["source"], c["match_reliability"]) for c in page] == [("amazon_title_year", 0.60)]
    assert _result(conn, ctx, s, "PageOnly")["effective_tconst"] is None


def test_ac01_contains_only_movie_reads_member_title_never_the_box(conn):
    """只经 contains 出现的成员作品：标题读成员标题，不读盒子总标题；片长、分级不下推（R05、R21、R28、R33）。"""
    s, ctx = _run(conn, _scope_world())
    title = _cands(conn, ctx, s, "MemberOnly", "title")
    page = [c for c in title if c["source"] == "page_title"]
    assert [c["raw_value"] for c in page] == ["Member One"] and page[0]["source_ref"]["relation_type"] == "contains"
    assert page[0]["source_key"].startswith("B0COL#1:")
    assert all("Box Set" not in (c["raw_value"] or "") for c in title)
    assert h.movie_sel(conn, ctx, s, "MemberOnly", "runtime_minutes")["status"] == "missing"       # 盒子 190 分钟不下推
    assert h.movie_sel(conn, ctx, s, "MemberOnly", "mpaa_rating")["status"] == "missing"           # 盒子 R 分级不下推
    assert _cands(conn, ctx, s, "MemberOnly", "mpaa_rating") == []


# ------------------------------------------------------------------ AC02 候选保留、状态语义与版本属性隔离（作品层）

def _attribute_world() -> w.World:
    imdb = [IMDB("tt2000001", "Seven Samurai", year=1954, runtime=207)]
    movies = [M("Samurai", REL("imdb_t4_link", "tt2000001"))]
    eds = [E("B0DVD", "Seven Samurai [DVD]", movie="Samurai", runtime=((207, "detail_bullets"),),
             rating=(("NR (Not Rated)", "detail_bullets"),), packaging=("Widescreen Edition",)),
           E("B0VHS", "Seven Samurai [VHS]", movie="Samurai", runtime=((160, "detail_bullets"),),
             rating=(("Unrated", "byline"),), packaging=("Full Screen",)),
           E("B0BOX", "Kurosawa Collection [DVD]", kind="collection", runtime=((600, "detail_bullets"),),
             rating=(("R (Restricted)", "detail_bullets"),), members=((1, "Samurai", "Seven Samurai"),))]
    return w.World(imdb, movies, eds)


def test_ac02_movie_candidates_have_exclusive_status_and_closed_vocabulary(conn):
    """全部候选带完整定位与互斥状态；胜出值相同者都为 adopted；missing 的 considered 为空；冲突类型在闭集内；上游行不变。"""
    s = h.seed(conn, _attribute_world())
    before = h.upstream_digest(conn)
    ctx = h.run_stages(conn, s, upto="movie_scalar")
    assert h.upstream_digest(conn) == before
    key = ctx.inputs.stage_key("movie_scalar")
    rows = conn.execute("SELECT status, source, source_key, source_ref, match_reliability, precision, field_name, raw_value, "
                        "normalized_value FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s "
                        "AND entity_type = 'movie'", (s.eid, key)).fetchall()
    assert rows
    for status, source, source_key, ref, rel, precision, field, raw, norm in rows:
        assert status in CANDIDATE_STATUSES and source and source_key and ref and precision
        assert 0 <= float(rel) <= 1 and field in {"title", "original_title", "release_year", "runtime_minutes", "mpaa_rating"}
    fs = conn.execute("SELECT field_selections FROM task7.movie_scalar_stage_result WHERE extraction_id = %s AND stage_key = %s",
                      (s.eid, key)).fetchone()[0]
    assert set(fs) == {"title", "original_title", "release_year", "runtime_minutes", "mpaa_rating"}
    for field, sel in fs.items():
        assert sel["status"] in SELECTION_STATUSES and sel["conflict_type"] in CONFLICT_TYPES
        assert (sel["status"] == "missing") == (sel["considered_candidate_ids"] == [])
        assert (sel["status"] == "resolved") == (sel["winning_value"] is not None)
    orig = fs["original_title"]
    assert (orig["status"], orig["considered_candidate_ids"], orig["winning_value"]) == ("missing", [], None)
    # 胜出值相同的候选都标 adopted：IMDb 片长 207 与 DVD 版 207
    run = {c["source_key"]: c["status"] for c in h.candidates(conn, ctx, "movie_scalar", s.mid("Samurai"), "runtime_minutes")}
    assert run == {"B0DVD": "adopted", "B0VHS": "rejected", "tt2000001": "adopted"}
    sel = fs["runtime_minutes"]
    assert sel["selected_candidate_ids"] == [next(c["candidate_id"] for c in h.candidates(
        conn, ctx, "movie_scalar", s.mid("Samurai"), "runtime_minutes") if c["source_key"] == "tt2000001")]


def test_ac02_collection_totals_are_not_pushed_down_to_member_movies(conn):
    """合集的盒子总片长 600、分级 R 属于合集版本自身：成员作品只聚合 edition 商品，版本差异保留在版本层。"""
    s, ctx = _run(conn, _attribute_world())
    runtime = h.candidates(conn, ctx, "movie_scalar", s.mid("Samurai"), "runtime_minutes")
    assert {c["source_key"] for c in runtime} == {"tt2000001", "B0DVD", "B0VHS"}        # 没有 B0BOX
    rating = h.candidates(conn, ctx, "movie_scalar", s.mid("Samurai"), "mpaa_rating")
    assert {c["source_key"] for c in rating} == {"B0DVD", "B0VHS"} and {c["normalized_value"] for c in rating} == {"NR/Unrated"}
    assert _result(conn, ctx, s, "Samurai")["mpaa_rating"] == "NR/Unrated"
    assert _result(conn, ctx, s, "Samurai")["runtime_minutes"] == 207
    # 盒子在版本层保留自己的值，DVD 与 VHS 版本的片长差异也留在版本层
    ed = {r[0]: r[1:] for r in conn.execute("SELECT asin, runtime_minutes, mpaa_rating FROM task7.edition_stage_result "
                                           "WHERE extraction_id = %s AND stage_key = %s",
                                           (s.eid, ctx.inputs.stage_key("edition_resolve")))}
    assert ed == {"B0DVD": (207, "NR/Unrated"), "B0VHS": (160, "NR/Unrated"), "B0BOX": (600, "R")}
    assert _diffs(conn, ctx, s, "Samurai") == {"B0DVD": ("exact", False, False, 0), "B0VHS": ("unexplained_gt15", False, False, -47)}


# ------------------------------------------------------------------ AC03 标题、原名与别名

def _ac03_world() -> w.World:
    m1 = case("M1", "imdb_t4_link", imdb_title="Seven Samurai", imdb_original="Shichinin no samurai", imdb_year=1954,
              imdb_runtime=207, title_years=(1954,), page_title="Seven Samurai (Criterion Collection) [DVD]", page_runtime=207,
              akas=(("Seven Samurai", "US", "en"), ("Los siete samurais", "ES", "es")))
    m2 = case("M2", None, title_years=(), page_title="The Kid [VHS]")
    m2_b = E("B0M2b", "The Kid (1921) [DVD]", movie="M2")
    m3 = case("M3", None, title_years=(), page_title="Red Shadow [DVD]")
    m3_b = E("B0M3b", "Crimson Ninja [DVD]", movie="M3")
    m4 = case("M4", "imdb_t6_cooccur", imdb_title="Curse of the Were-Rabbit", imdb_original="Curse of the Were-Rabbit",
              imdb_year=2005, imdb_runtime=85, title_years=(2005,), page_title="Wrong Trousers [VHS]", page_runtime=140,
              akas=(("Wallace & Gromit: Were-Rabbit", "GB", "en"),))
    world = world_of(m1, m2, m3, m4)
    world.editions += [m2_b, m3_b]
    return world


@pytest.fixture
def ac03(conn):
    return _run(conn, _ac03_world())


def test_ac03_imdb_title_wins_and_page_title_is_superseded(conn, ac03):
    """M1：title、original_title 取 IMDb 值，页面候选 superseded_by_specific，别名表含 IMDb 标题及语言。"""
    s, ctx = ac03
    r = _result(conn, ctx, s, "M1")
    assert (r["title"], r["original_title"], r["cross_source_mismatch_halt"]) == ("Seven Samurai", "Shichinin no samurai", False)
    sel = h.movie_sel(conn, ctx, s, "M1", "title")
    assert (sel["status"], sel["conflict_type"], sel["confidence"]) == ("resolved", "formatting_or_alias", 0.95)
    cands = _cands(conn, ctx, s, "M1", "title")
    assert {c["source"]: (c["status"], c["normalized_value"]) for c in cands} == {
        "page_title": ("superseded_by_specific", "Seven Samurai (Criterion Collection)"),
        "imdb_t4_link": ("adopted", "Seven Samurai")}
    assert sel["selected_candidate_ids"] == [next(c["candidate_id"] for c in cands if c["source"] == "imdb_t4_link")]
    orig = h.movie_sel(conn, ctx, s, "M1", "original_title")
    assert (orig["status"], orig["disagreement_detail"]) == ("resolved", {"distinct_from_primary": True})
    # 别名：IMDb primary、original 与 aka（同名别名只留先出现者），aka 带区域与语言；页面变体另列
    aliases = conn.execute("SELECT alias_kind, alias_ordinal, alias, region, language FROM task7.movie_title_alias "
                           "WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s ORDER BY alias_kind, alias_ordinal",
                           (s.eid, ctx.inputs.stage_key("movie_scalar"), s.mid("M1"))).fetchall()
    assert aliases == [("aka", 2, "Los siete samurais", "ES", "es"), ("original", 1, "Shichinin no samurai", None, None),
                       ("page", 1, "Seven Samurai (Criterion Collection)", None, None),
                       ("primary", 1, "Seven Samurai", None, None)]
    assert len([a for a in aliases if a[0] != "page"]) == 3


def test_ac03_page_titles_merge_by_normalized_key_without_imdb(conn, ac03):
    """M2：无 IMDb，The Kid [VHS] 与 The Kid (1921) [DVD] 清洁后相同，title = The Kid，original_title 为 missing。"""
    s, ctx = ac03
    r = _result(conn, ctx, s, "M2")
    assert (r["title"], r["original_title"], r["effective_tconst"]) == ("The Kid", None, None)
    sel = h.movie_sel(conn, ctx, s, "M2", "title")
    assert (sel["status"], sel["conflict_type"], sel["confidence"]) == ("resolved", "none", 0.80)
    cands = _cands(conn, ctx, s, "M2", "title")
    assert [(c["raw_value"], c["normalized_value"], c["status"]) for c in sorted(cands, key=lambda c: c["source_key"])] == [
        ("The Kid [VHS]", "The Kid", "adopted"), ("The Kid (1921) [DVD]", "The Kid", "adopted")]
    assert sel["selected_candidate_ids"] == [min(cands, key=lambda c: c["source_ref"]["asin"])["candidate_id"]]
    orig = h.movie_sel(conn, ctx, s, "M2", "original_title")
    assert (orig["status"], orig["considered_candidate_ids"]) == ("missing", [])
    assert conn.execute("SELECT count(*) FROM task7.movie_title_alias WHERE movie_id = %s AND alias_kind <> 'page'",
                        (s.mid("M2"),)).fetchone()[0] == 0


def test_ac03_task4_normalized_key_hit_is_at_least_alias(conn):
    """页面原串 300 (Limited Collector's Edition + Digital Copy) 的任务 4 规范键 300 命中 IMDb primary：
    至少归为 alias（superseded_by_specific，formatting_or_alias），不能落到 other 记成 cross_source_override。"""
    raw = "300 (Limited Collector's Edition + Digital Copy) [Blu-ray]"
    s, ctx = _run(conn, world_of(case("N1", "imdb_t4_link", imdb_title="300", page_title=raw),
                                 case("N2", "imdb_t4_link", imdb_title="Wallace and Gromit", page_title="Wallace (Special Case)")))
    sel = h.movie_sel(conn, ctx, s, "N1", "title")
    assert (sel["winning_value"], sel["conflict_type"]) == ("300", "formatting_or_alias")
    cands = {c["source"]: c["status"] for c in _cands(conn, ctx, s, "N1", "title")}
    assert cands == {"page_title": "superseded_by_specific", "imdb_t4_link": "adopted"}
    assert _cands(conn, ctx, s, "N1", "title")[0]["source_ref"]["task4_title_key"] == "300"
    # 控制：规范键 wallace 只是单实词，不命中 IMDb 的 wallace and gromit，仍是 other
    sel2 = h.movie_sel(conn, ctx, s, "N2", "title")
    assert sel2["conflict_type"] == "cross_source_override"


def test_ac03_substantially_different_page_titles_stay_unresolved(conn, ac03):
    """M3：无 IMDb，规范标题键 red shadow 与 crimson ninja 实质不同，title 为 conflict_unresolved，候选 conflict_pending。"""
    s, ctx = ac03
    sel = h.movie_sel(conn, ctx, s, "M3", "title")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"], sel["selected_candidate_ids"]) == (
        "conflict_unresolved", None, "intra_source_tie_unresolved", [])
    assert len(sel["considered_candidate_ids"]) == 2
    assert {c["status"] for c in _cands(conn, ctx, s, "M3", "title")} == {"conflict_pending"}
    assert _result(conn, ctx, s, "M3")["title"] is None


def test_ac03_halted_link_falls_back_to_page_title(conn, ac03):
    """M4：已熔断，title 取页面值，IMDb 候选 rejected，original_title 为 conflict_unresolved，别名表无 IMDb 行。"""
    s, ctx = ac03
    assert _result(conn, ctx, s, "M4")["cross_source_mismatch_halt"] is True
    sel = h.movie_sel(conn, ctx, s, "M4", "title")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"]) == ("resolved", "Wrong Trousers", "cross_source_mismatch_halt")
    st = {c["source"]: (c["status"], c["status_reason"]) for c in _cands(conn, ctx, s, "M4", "title")}
    assert st == {"imdb_t6_cooccur": ("rejected", "cross_source_mismatch_halt"), "page_title": ("adopted", None)}
    orig = h.movie_sel(conn, ctx, s, "M4", "original_title")
    assert (orig["status"], orig["winning_value"], orig["conflict_type"]) == ("conflict_unresolved", None, "cross_source_mismatch_halt")
    assert [c["status"] for c in _cands(conn, ctx, s, "M4", "original_title")] == ["conflict_pending"]
    kinds = {r[0] for r in conn.execute("SELECT alias_kind FROM task7.movie_title_alias WHERE movie_id = %s", (s.mid("M4"),))}
    assert kinds == {"page"}


# ------------------------------------------------------------------ AC04 上映年份决策表与推断上界隔离

def _ac04_world() -> w.World:
    cases = [
        case("Y1", "imdb_t4_link", imdb_year=1985, title_years=(1986,)),
        case("Y2", "imdb_t4_link", imdb_year=1954, title_years=(1998,), imdb_dirs=(("nm0000021", "Ann Zulu"),),
             page_dirs=(("Ann Zulu", None),)),
        case("Y3", "imdb_t4_link", imdb_year=1954, title_years=(1998,), imdb_dirs=(("nm0000022", "Bea Yankee"),),
             page_dirs=(("Cy Xray", None),), page_runtime=150),
        case("Y4", "imdb_t6_cooccur", imdb_year=1991, title_years=(2010,), page_runtime=150),
        case("Y5", "imdb_t6_cooccur", imdb_year=1991, title_years=(2010,), imdb_dirs=(("nm0000023", "Di Whiskey"),),
             page_dirs=(("Di Whiskey", None),)),
        case("Y6", "imdb_t5_hint", imdb_year=1991, title_years=(1996,), page_runtime=0, imdb_runtime=None),
        case("Y7", None, title_years=(1971, 1975), release_extra={"missing_reason": "title_years_disagree"}),
        case("Y8", None, title_years=(), release_extra={"inferred": ("2001-05-01", "day", "media_release")}),
    ]
    return world_of(*cases)


@pytest.fixture
def ac04(conn):
    return _run(conn, _ac04_world())


@pytest.mark.parametrize("movie, status, year, ctype, rule", [
    ("Y1", "resolved", "1985", "minor_tolerance", "year.table:YR2"),
    ("Y2", "resolved", "1954", "cross_source_override", "year.table:YR4"),
    ("Y3", "conflict_unresolved", None, "severe_mismatch_unresolved", "year.table:YR3"),
    ("Y4", "conflict_unresolved", None, "cross_source_mismatch_halt", "year.table:YR7"),
    ("Y5", "resolved", "1991", "cross_source_override", "year.table:YR5"),
    ("Y6", "conflict_unresolved", None, "severe_mismatch_unresolved", "year.table:YR6"),
    ("Y7", "conflict_unresolved", None, "intra_source_tie_unresolved", "year.table:YR9"),
    ("Y8", "missing", None, "none", "year.table:YR10"),
])
def test_ac04_year_decision_table(conn, ac04, movie, status, year, ctype, rule):
    s, ctx = ac04
    sel = h.movie_sel(conn, ctx, s, movie, "release_year")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"], sel["selection_rule"]) == (status, year, ctype, rule)
    r = _result(conn, ctx, s, movie)
    assert r["release_year"] == (None if year is None else int(year)) and r["release_precision"] == "year"


def test_ac04_candidate_states_follow_the_decision(conn, ac04):
    s, ctx = ac04
    st = lambda m: {c["source"]: (c["normalized_value"], c["status"]) for c in _cands(conn, ctx, s, m, "release_year")}  # noqa: E731
    assert st("Y1") == {"imdb_t4_link": ("1985", "adopted"), "amazon_title_year": ("1986", "rejected")}
    assert st("Y2") == {"imdb_t4_link": ("1954", "adopted"), "amazon_title_year": ("1998", "rejected")}
    assert {v[1] for v in st("Y3").values()} == {"conflict_pending"}                        # 无法决策：全部待定
    assert {v[1] for v in st("Y4").values()} == {"conflict_pending"}
    assert st("Y5") == {"imdb_t6_cooccur": ("1991", "adopted"), "amazon_title_year": ("2010", "rejected")}
    assert {v[1] for v in st("Y7").values()} == {"conflict_pending"} and len(_cands(conn, ctx, s, "Y7", "release_year")) == 2
    sel = h.movie_sel(conn, ctx, s, "Y2", "release_year")
    assert sel["selected_candidate_ids"] == [next(c["candidate_id"] for c in _cands(conn, ctx, s, "Y2", "release_year")
                                                  if c["source"] == "imdb_t4_link")]
    assert sel["confidence"] == 0.70 and h.movie_sel(conn, ctx, s, "Y1", "release_year")["confidence"] == 0.85   # 0.95 受容差上限限制
    assert h.movie_sel(conn, ctx, s, "Y5", "release_year")["confidence"] == 0.65                # 0.75 − 0.10（弱匹配单信号扣减）


def test_ac04_override_and_severe_cases_write_quality_issues(conn, ac04):
    s, ctx = ac04
    assert _issue_codes(conn, ctx, s, "Y2") == {"cross_source_override"}
    assert _issue_codes(conn, ctx, s, "Y5") == {"cross_source_override"}
    assert _issue_codes(conn, ctx, s, "Y3") == {"severe_mismatch_unresolved", "severe_runtime_conflict"}   # 片长差 50 分钟同时写问题
    assert _issue_codes(conn, ctx, s, "Y6") == {"severe_mismatch_unresolved"}
    assert _issue_codes(conn, ctx, s, "Y1") == set()


def test_ac04_inferred_bound_is_reference_only_never_a_candidate(conn, ac04):
    """Y8：无候选，release_year 为 missing，推断上界 2001-05-01 只在参考列，且不产生年份候选；任务 6 缺失原因记入 detail。"""
    s, ctx = ac04
    r = _result(conn, ctx, s, "Y8")
    assert (r["release_year"], r["inferred_upper_bound"], r["inferred_bound_precision"], r["inferred_bound_evidence"]) == (
        None, date(2001, 5, 1), "day", "media_release")
    assert _cands(conn, ctx, s, "Y8", "release_year") == []
    sel = h.movie_sel(conn, ctx, s, "Y8", "release_year")
    assert sel["considered_candidate_ids"] == [] and sel["disagreement_detail"]["task6_missing_reason"] == "no_candidate"
    assert conn.execute("SELECT count(*) FROM task7.field_candidate WHERE field_name = 'release_year' AND "
                        "(raw_value LIKE '2001%%' OR normalized_value = '2001')").fetchone()[0] == 0
    # 其他作品没有上界时参考列为 NULL
    assert _result(conn, ctx, s, "Y1")["inferred_upper_bound"] is None


# ------------------------------------------------------------------ AC05 片长分层与逐版本差异分类

def _ac05_world() -> w.World:
    r1 = case("R1", "imdb_t4_link", imdb_runtime=100, page_runtime=101)
    r1_edits = [E("B0R1B", "R1 Picture [PAL DVD]", movie="R1", runtime=((96, "detail_bullets"),), video_system=(("PAL", "detail_bullets"),)),
                E("B0R1B2", "R1 Picture [VHS]", movie="R1", runtime=((96, "detail_bullets"),)),
                E("B0R1C", "R1 Picture (Director's Cut)", movie="R1", runtime=((125, "detail_bullets"),),
                  cut_type=(("directors cut", "title"),))]
    r2 = case("R2", "imdb_t4_link", imdb_runtime=90, page_runtime=135)
    r3 = case("R3", None, title_years=(), page_runtime=92)
    r3_b = E("B0R3b", "R3 Picture [VHS]", movie="R3", runtime=((94, "detail_bullets"),))
    r4 = case("R4", None, title_years=(), page_runtime=85)
    r4_b = E("B0R4b", "R4 Picture [VHS]", movie="R4", runtime=((118, "detail_bullets"),))
    r5 = case("R5", "imdb_t6_cooccur", imdb_year=1990, imdb_runtime=100, title_years=(2010,), page_runtime=150)
    world = world_of(r1, r2, r3, r4, r5)
    world.editions += r1_edits + [r3_b, r4_b]
    return world


@pytest.fixture
def ac05(conn):
    return _run(conn, _ac05_world())


def test_ac05_runtime_diff_classes_and_pal_requires_evidence(conn, ac05):
    """R1：IMDb 100，作品层采用 100；逐版本 tolerance_1_2、pal_evidence、variance_3_5（公式吻合但无 PAL 断言）、cut_or_multidisc。"""
    s, ctx = ac05
    assert _diffs(conn, ctx, s, "R1") == {
        "B0R1": ("tolerance_1_2", False, False, 1), "B0R1B": ("pal_evidence", True, True, -4),
        "B0R1B2": ("variance_3_5", True, False, -4), "B0R1C": ("cut_or_multidisc", False, False, 25)}
    sel = h.movie_sel(conn, ctx, s, "R1", "runtime_minutes")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"]) == ("resolved", "100", "framerate_or_edition")
    assert sel["disagreement_detail"]["worst_diff_class"] == "cut_or_multidisc"
    assert _result(conn, ctx, s, "R1")["runtime_minutes"] == 100
    assert _issue_codes(conn, ctx, s, "R1") == set()
    st = {c["source_key"]: c["status"] for c in _cands(conn, ctx, s, "R1", "runtime_minutes")}
    assert [v for k, v in st.items() if k.startswith("tt")] == ["adopted"]                  # IMDb 候选
    assert {k: v for k, v in st.items() if not k.startswith("tt")} == {
        "B0R1": "rejected", "B0R1B": "rejected", "B0R1B2": "rejected", "B0R1C": "rejected"}


def test_ac05_unexplained_difference_keeps_imdb_and_writes_severe_issue(conn, ac05):
    """R2：IMDb 90、单碟无 cut_type、页面 135：作品层取 90，unexplained_gt15，写 severe_runtime_conflict。"""
    s, ctx = ac05
    assert _diffs(conn, ctx, s, "R2") == {"B0R2": ("unexplained_gt15", False, False, 45)}
    sel = h.movie_sel(conn, ctx, s, "R2", "runtime_minutes")
    assert (sel["winning_value"], sel["conflict_type"]) == ("90", "cross_source_override")
    assert _issue_codes(conn, ctx, s, "R2") == {"severe_runtime_conflict"}
    detail = conn.execute("SELECT detail FROM task7.quality_issue WHERE issue_code = 'severe_runtime_conflict' AND entity_id = %s",
                          (s.mid("R2"),)).fetchone()[0]
    assert detail["editions"] == [{"asin": "B0R2", "page": 135, "delta": 45}] and detail["imdb_runtime"] == 90


def test_ac05_without_imdb_small_spread_takes_the_smaller_mode_and_large_spread_is_unresolved(conn, ac05):
    """R3：无 IMDb，92 与 94 取 92（minor_tolerance）；R4：85 与 118 无标记，conflict_unresolved；无 runtime_diff。"""
    s, ctx = ac05
    r3 = h.movie_sel(conn, ctx, s, "R3", "runtime_minutes")
    assert (r3["status"], r3["winning_value"], r3["conflict_type"], r3["confidence"]) == ("resolved", "92", "minor_tolerance", 0.85)
    assert "舍入误差" in r3["reason"]
    r4 = h.movie_sel(conn, ctx, s, "R4", "runtime_minutes")
    assert (r4["status"], r4["winning_value"], r4["conflict_type"]) == ("conflict_unresolved", None, "intra_source_tie_unresolved")
    assert {c["status"] for c in _cands(conn, ctx, s, "R4", "runtime_minutes")} == {"conflict_pending"}
    assert _diffs(conn, ctx, s, "R3") == {} and _diffs(conn, ctx, s, "R4") == {}


def test_ac05_halted_link_leaves_runtime_unresolved_but_editions_keep_theirs(conn, ac05):
    """R5：已熔断，作品层 conflict_unresolved，版本层保留各自片长，不写 runtime_diff。"""
    s, ctx = ac05
    assert _result(conn, ctx, s, "R5")["cross_source_mismatch_halt"] is True
    sel = h.movie_sel(conn, ctx, s, "R5", "runtime_minutes")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"]) == ("conflict_unresolved", None, "cross_source_mismatch_halt")
    assert {c["status"] for c in _cands(conn, ctx, s, "R5", "runtime_minutes")} == {"conflict_pending"}
    assert _diffs(conn, ctx, s, "R5") == {}
    assert conn.execute("SELECT runtime_minutes FROM task7.edition_stage_result WHERE asin = 'B0R5' AND stage_key = %s",
                        (ctx.inputs.stage_key("edition_resolve"),)).fetchone()[0] == 150
    # 作品层其余字段不受影响：mpaa 不依赖 IMDb
    assert h.movie_sel(conn, ctx, s, "R5", "mpaa_rating")["status"] == "missing"


# ------------------------------------------------------------------ AC06 内容分级规范化与跨版本消解

def _ac06_world() -> w.World:
    a1 = case("A1", None, title_years=(), edition_extra={"rating": (("PG", "byline"), ("PG (Parental Guidance Suggested)", "detail_bullets"))})
    p1 = case("P1", None, title_years=(), edition_extra={"rating": (("R", "byline"),)})
    p1_y = E("B0P1y", "P1 Picture [VHS]", movie="P1", rating=(("NR (Not Rated)", "detail_bullets"),))
    p1_z = E("B0P1z", "P1 Picture [Blu-ray]", movie="P1", rating=(("Unrated", "byline"),))
    p2 = case("P2", None, title_years=(), edition_extra={"rating": (("G", "byline"),)})
    p2_b = E("B0P2b", "P2 Picture [VHS]", movie="P2", rating=(("PG", "detail_bullets"),))
    a2 = case("A2", None, title_years=(), edition_extra={"rating": (("Approved", "byline"),)})
    mixed = case("A3", None, title_years=(), edition_extra={"rating": (("Approved", "byline"), ("PG-13 (Parents Strongly Cautioned)", "detail_bullets"))})
    intra = case("A4", None, title_years=(), edition_extra={"rating": (("PG", "byline"), ("R (Restricted)", "detail_bullets"))})
    world = world_of(a1, p1, p2, a2, mixed, intra)
    world.editions += [p1_y, p1_z, p2_b]
    return world


@pytest.fixture
def ac06(conn):
    return _run(conn, _ac06_world())


def test_ac06_abbreviation_and_full_name_normalize_to_one_rating(conn, ac06):
    """A1：byline PG 与 detail_bullets PG (Parental Guidance Suggested) → 版本层 PG（formatting_or_alias），两条都 adopted。"""
    s, ctx = ac06
    sel = h.edition_sel(conn, ctx, "B0A1", "mpaa_rating")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"]) == ("resolved", "PG", "formatting_or_alias")
    c = h.candidates(conn, ctx, "edition_resolve", "B0A1", "mpaa_rating")
    assert {x["raw_value"]: (x["normalized_value"], x["status"]) for x in c} == {
        "PG": ("PG", "adopted"), "PG (Parental Guidance Suggested)": ("PG", "adopted")}
    assert sel["selected_candidate_ids"] == [next(x["candidate_id"] for x in c if x["source_ref"]["section"] == "detail_bullets")]
    assert _result(conn, ctx, s, "A1")["mpaa_rating"] == "PG"


def test_ac06_concrete_rating_beats_unrated(conn, ac06):
    """P1：R、NR (Not Rated)、Unrated → 作品层 R（unrated_vs_rated），未评级候选 superseded_by_specific。"""
    s, ctx = ac06
    sel = h.movie_sel(conn, ctx, s, "P1", "mpaa_rating")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"]) == ("resolved", "R", "unrated_vs_rated")
    cands = _cands(conn, ctx, s, "P1", "mpaa_rating")
    assert {c["source_key"]: (c["normalized_value"], c["status"]) for c in cands} == {
        "B0P1": ("R", "adopted"), "B0P1y": ("NR/Unrated", "superseded_by_specific"),
        "B0P1z": ("NR/Unrated", "superseded_by_specific")}
    assert {c["source_key"]: c["raw_value"] for c in cands}["B0P1y"] == "NR (Not Rated)"           # 原始值保留
    assert sel["selected_candidate_ids"] == [next(c["candidate_id"] for c in cands if c["source_key"] == "B0P1")]
    assert _result(conn, ctx, s, "P1")["mpaa_rating"] == "R"


def test_ac06_two_concrete_ratings_stay_unresolved_and_editions_keep_their_own(conn, ac06):
    """P2：G 与 PG → 作品层 conflict_unresolved；版本层各自保留 G 与 PG。"""
    s, ctx = ac06
    sel = h.movie_sel(conn, ctx, s, "P2", "mpaa_rating")
    assert (sel["status"], sel["winning_value"], sel["conflict_type"]) == ("conflict_unresolved", None, "intra_source_tie_unresolved")
    assert {c["status"] for c in _cands(conn, ctx, s, "P2", "mpaa_rating")} == {"conflict_pending"}
    assert (h.edition_sel(conn, ctx, "B0P2", "mpaa_rating")["winning_value"],
            h.edition_sel(conn, ctx, "B0P2b", "mpaa_rating")["winning_value"]) == ("G", "PG")
    assert _result(conn, ctx, s, "P2")["mpaa_rating"] is None


def test_ac06_unknown_rating_is_recorded_not_guessed(conn, ac06):
    """A2：未知分级串写 unknown_mpaa_rating，版本层保留原值为 conflict_unresolved，作品层没有可用候选；A3：已知分级优先。"""
    s, ctx = ac06
    issues = conn.execute("SELECT detail FROM task7.quality_issue WHERE issue_code = 'unknown_mpaa_rating' AND entity_id = %s "
                          "AND stage_key = %s", (f"B0A2", ctx.inputs.stage_key("edition_resolve"))).fetchall()
    assert [d[0]["raw_value"] for d in issues] == ["Approved"]
    sel = h.edition_sel(conn, ctx, "B0A2", "mpaa_rating")
    assert (sel["status"], sel["winning_value"]) == ("conflict_unresolved", None) and len(sel["considered_candidate_ids"]) == 1
    c = h.candidates(conn, ctx, "edition_resolve", "B0A2", "mpaa_rating")[0]
    assert (c["raw_value"], c["normalized_value"], c["status"]) == ("Approved", None, "conflict_pending")
    assert h.movie_sel(conn, ctx, s, "A2", "mpaa_rating")["status"] == "missing"
    # A3：未知值与已知值并存，已知值采用，未知候选 rejected
    sel3 = h.edition_sel(conn, ctx, "B0A3", "mpaa_rating")
    assert (sel3["winning_value"], sel3["conflict_type"]) == ("PG-13", "none")
    assert {x["raw_value"]: x["status"] for x in h.candidates(conn, ctx, "edition_resolve", "B0A3", "mpaa_rating")} == {
        "Approved": "rejected", "PG-13 (Parents Strongly Cautioned)": "adopted"}


def test_ac06_same_asin_conflict_prefers_detail_bullets(conn, ac06):
    """同 ASIN 的 byline PG 与 detail_bullets R 不一致：优先 detail_bullets（intra_edition_rating_conflict）。"""
    s, ctx = ac06
    sel = h.edition_sel(conn, ctx, "B0A4", "mpaa_rating")
    assert (sel["winning_value"], sel["conflict_type"]) == ("R", "intra_edition_rating_conflict")
    assert {x["raw_value"]: x["status"] for x in h.candidates(conn, ctx, "edition_resolve", "B0A4", "mpaa_rating")} == {
        "PG": "rejected", "R (Restricted)": "adopted"}
    assert _result(conn, ctx, s, "A4")["mpaa_rating"] == "R"


# ------------------------------------------------------------------ 分批恢复（阶段级冒烟；完整的 AC12 在 T7-05 的 test_task7_runs.py）

def _digest(conn, ctx, s) -> dict:
    """与候选 ID、运行 ID 无关的结果摘要：三个阶段的结果行 + 选择记录的状态、胜出值与冲突类型。"""
    out = {}
    q = lambda sql, stage: conn.execute(sql, (s.eid, ctx.inputs.stage_key(stage))).fetchall()  # noqa: E731
    out["edition"] = q("SELECT asin, runtime_minutes, mpaa_rating, publisher, discs, media_format, attributes_detail::text "
                       "FROM task7.edition_stage_result WHERE extraction_id = %s AND stage_key = %s ORDER BY asin",
                       "edition_resolve")
    out["link"] = q("SELECT movie_id, sig_year, sig_runtime, sig_director, sig_title, cross_source_mismatch_halt "
                    "FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s ORDER BY movie_id", "link_assess")
    out["scalar"] = q("SELECT movie_id, title, original_title, release_year, runtime_minutes, mpaa_rating, "
                      "cross_source_mismatch_halt, (SELECT jsonb_object_agg(k, jsonb_build_array(v->'status', "
                      "v->'winning_value', v->'conflict_type')) FROM jsonb_each(field_selections) AS e(k, v))::text "
                      "FROM task7.movie_scalar_stage_result WHERE extraction_id = %s AND stage_key = %s ORDER BY movie_id",
                      "movie_scalar")
    out["diff"] = q("SELECT movie_id, asin, diff_class, delta FROM task7.runtime_diff WHERE extraction_id = %s "
                    "AND stage_key = %s ORDER BY 1, 2", "movie_scalar")
    out["candidates"] = conn.execute(
        "SELECT entity_id, field_name, source, regexp_replace(source_key, ':[0-9]+$', ''), normalized_value, status "
        "FROM task7.field_candidate WHERE extraction_id = %s ORDER BY 1, 2, 3, 4, 5", (s.eid,)).fetchall()
    return out


@pytest.mark.parametrize("stage", ["edition_resolve", "link_assess", "movie_scalar"])
def test_batch_failure_then_rerun_matches_a_single_pass(conn, stage):
    """批大小 2；在该阶段第 2 批提交前注入故障：只有第 1 批落库；重跑跳过已完成批次，结果与一次跑完一致。"""
    world = _ac05_world()
    s = h.seed(conn, world)
    reference = _digest(conn, h.run_stages(conn, s, batch_size=2), s)
    s = h.seed(conn, world)

    def boom(st: str, batch_no: int) -> None:
        if st == stage and batch_no == 2:
            raise RuntimeError("injected")

    ctx = h.start_run(conn, s.inputs())
    with pytest.raises(RuntimeError, match="injected"):
        h.run_stages(conn, s, batch_size=2, before_commit=boom, ctx=ctx)
    marked = conn.execute("SELECT count(*) FROM task7.entity_stage WHERE stage = %s AND entity_id <> '__shared_tconst__'",
                          (stage,)).fetchone()[0]
    assert marked == 2                                                              # 只有第 1 批的 2 个实体落库
    h.run_stages(conn, s, batch_size=2, ctx=ctx)
    assert _digest(conn, ctx, s) == reference
    done = conn.execute("SELECT stage, count(*) FROM task7.entity_stage GROUP BY 1 ORDER BY 1").fetchall()
    assert dict(done) == {"edition_resolve": 10, "link_assess": 6, "movie_scalar": 5}      # link_assess 含共享步骤标记
