"""任务 7 作品层多值字段（movie_multivalue）：类型词面处置与并集、演职员并集与角色边界、同片署名对齐、多人串、
署名顺序、覆盖范围（spec AC07、AC08、AC22，补完 AC02 的合集类型不下推与 AC21 的同一关联判定）。
预期值按 spec 的 AC 表手算；夹具里的类型串由任务 4 的 genre_tags 拆分，人物与来源提及与任务 5 同形，见 tests/fixtures/task7/README.md。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py
"""

from __future__ import annotations

import hashlib

import pytest
import _task7_helpers as h
from task7 import world as w

pytestmark = pytest.mark.integration

M, E, IMDB, REL, CR = w.Movie, w.Ed, w.Imdb, w.Release, w.Credit
case, world_of = h.case, h.world_of


def imdb_pid(nconst: str) -> str:
    """R40 的公式，独立于实现手写：'ps_' + substr(sha256('imdb:' || nconst), 1, 16)。"""
    return "ps_" + hashlib.sha256(f"imdb:{nconst}".encode()).hexdigest()[:16]


def _run(conn, world, **over):
    s = h.seed(conn, world)
    return s, h.run_stages(conn, s, upto="movie_multivalue", skip=("movie_scalar",), **over)


def _sel(conn, ctx, s, movie: str, field: str) -> dict:
    row = conn.execute("SELECT field_selections -> %s FROM task7.movie_multivalue_stage_result WHERE extraction_id = %s "
                       "AND stage_key = %s AND movie_id = %s",
                       (field, s.eid, ctx.inputs.stage_key("movie_multivalue"), s.mid(movie))).fetchone()
    assert row is not None, f"作品 {movie} 没有 movie_multivalue 阶段结果"
    return row[0]


def _result(conn, ctx, s, movie: str) -> dict:
    cols = ("cast_coverage", "genre_count", "director_count", "starring_count", "cast_count")
    row = conn.execute(f"SELECT {', '.join(cols)} FROM task7.movie_multivalue_stage_result WHERE extraction_id = %s "
                       "AND stage_key = %s AND movie_id = %s",
                       (s.eid, ctx.inputs.stage_key("movie_multivalue"), s.mid(movie))).fetchone()
    return dict(zip(cols, row))


def _genres(conn, ctx, s, movie: str) -> dict[str, tuple]:
    return {g: (cov, raw, ref) for g, cov, raw, ref in conn.execute(
        "SELECT genre, source_coverage, raw_terms, source_ref FROM task7.movie_genre WHERE extraction_id = %s "
        "AND stage_key = %s AND movie_id = %s", (s.eid, ctx.inputs.stage_key("movie_multivalue"), s.mid(movie)))}


def _terms(conn, ctx, s, movie: str) -> dict[tuple[str, str], tuple]:
    return {(sec, term): (disp, mapped, asins) for sec, term, disp, mapped, asins in conn.execute(
        "SELECT section, term, disposition, mapped_genres, asins FROM task7.movie_genre_term WHERE extraction_id = %s "
        "AND stage_key = %s AND movie_id = %s", (s.eid, ctx.inputs.stage_key("movie_multivalue"), s.mid(movie)))}


def _people(conn, ctx, s, movie: str, role: str) -> dict[str, dict]:
    cols = ("person_id", "source_coverage", "status", "credit_order", "imdb_ordering", "nconst", "raw_names",
            "resolution_note")
    return {r[0]: dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM task7.movie_person_resolved WHERE extraction_id = %s AND stage_key = %s "
        "AND movie_id = %s AND role = %s", (s.eid, ctx.inputs.stage_key("movie_multivalue"), s.mid(movie), role))}


def _align(conn, ctx, s, movie: str) -> list[dict]:
    cols = ("role", "page_person_id", "raw_name", "segment", "segment_name", "resolved_person_id", "nconst", "level",
            "outcome")
    return [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM task7.credit_alignment WHERE extraction_id = %s AND stage_key = %s "
        "AND movie_id = %s ORDER BY role, page_person_id, segment",
        (s.eid, ctx.inputs.stage_key("movie_multivalue"), s.mid(movie)))]


def _issues(conn, ctx, s, movie: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for code, detail in conn.execute(
            "SELECT issue_code, detail FROM task7.quality_issue WHERE extraction_id = %s AND stage_key = %s "
            "AND entity_id = %s ORDER BY issue_id", (s.eid, ctx.inputs.stage_key("movie_multivalue"), s.mid(movie))):
        out.setdefault(code, []).append(detail)
    return out


def _ext_nconsts(conn, s) -> set[str]:
    return {r[0] for r in conn.execute("SELECT nconst FROM task7.imdb_person_ext WHERE extraction_id = %s", (s.eid,))}


# ------------------------------------------------------------------ AC07 类型映射、词面处置与多源并集

def _ac07_world() -> w.World:
    g1, g1m, g1e = case("G1", "imdb_t4_link", imdb_title="Genre One", imdb_year=1990,
                        edition_extra=dict(genre=("Action & Adventure, Science Fiction & Fantasy See more",
                                                  "faith_&_spirituality"),
                                           breadcrumb=("Movies & TV", "DVD", "Anime & Manga")))
    g1.genres = ("Action", "Sci-Fi")
    # G2：弱匹配，年份与片长同时矛盾 → 熔断；IMDb 的 Horror 不得并入，页面 Drama 照常
    g2, g2m, g2e = case("G2", "imdb_t6_cooccur", imdb_title="Genre Two", imdb_year=1990, imdb_runtime=100,
                        title_years=(1975,), page_runtime=140, edition_extra=dict(genre=("Drama",)))
    g2.genres = ("Horror", "Thriller")
    # G3：真实形态里 raw_value 是整串（含逗号），任何一个标签都映射不上整串；只有逐标签读 normalized_value 才对
    g3, g3m, g3e = case("G3", None, edition_extra=dict(genre=("Comedy, Romance, Kids & Family/10-12 Years",)))
    # G4：只有噪声与未映射词面 → missing；G5：没有任何词面 → missing
    g4, g4m, g4e = case("G4", None, edition_extra=dict(genre=("Spielberg, alien movie",), breadcrumb=("Movies & TV",)))
    g5, g5m, g5e = case("G5", None)
    return world_of(( g1, g1m, g1e), (g2, g2m, g2e), (g3, g3m, g3e), (g4, g4m, g4e), (g5, g5m, g5e))


@pytest.fixture
def ac07(conn):
    s, ctx = _run(conn, _ac07_world())
    return conn, s, ctx


def test_ac07_union_of_imdb_and_page_genres_with_coverage(ac07):
    conn, s, ctx = ac07
    g = _genres(conn, ctx, s, "G1")
    assert set(g) == {"Action", "Adventure", "Animation", "Anime", "Fantasy", "Sci-Fi"}
    assert g["Action"][0] == "both" and g["Sci-Fi"][0] == "both"
    assert g["Adventure"][0] == "page_only" and g["Fantasy"][0] == "page_only" and g["Anime"][0] == "page_only"
    assert g["Action"][1] == ["action & adventure"]          # 页面词面（规整后）随成员保留
    sel = _sel(conn, ctx, s, "G1", "genres")
    assert sel["status"] == "resolved" and sel["conflict_type"] == "complementary_union"
    assert sel["winning_value"] == "Action|Adventure|Animation|Anime|Fantasy|Sci-Fi"
    assert sel["selected_candidate_ids"] == [] and sel["considered_candidate_ids"] == []
    assert _result(conn, ctx, s, "G1")["genre_count"] == 6


def test_ac07_every_page_term_is_kept_with_its_disposition(ac07):
    conn, s, ctx = ac07
    t = _terms(conn, ctx, s, "G1")
    assert t[("genre", "action & adventure")][:2] == ("mapped", ["Action", "Adventure"])
    assert t[("genre", "science fiction & fantasy")][:2] == ("mapped", ["Sci-Fi", "Fantasy"])   # see more 被去除
    assert t[("genre", "faith & spirituality")][:2] == ("unmapped", [])                         # 下划线还原
    assert t[("breadcrumb", "Movies & TV")][0] == "noise" and t[("breadcrumb", "DVD")][0] == "noise"
    assert t[("breadcrumb", "Anime & Manga")][:2] == ("mapped", ["Animation", "Anime"])
    assert t[("genre", "action & adventure")][2] == ["B0G1"]


def test_ac07_unmapped_term_is_not_a_genre_and_writes_a_quality_issue(ac07):
    conn, s, ctx = ac07
    assert not any("faith" in g.lower() for g in _genres(conn, ctx, s, "G1"))
    issues = _issues(conn, ctx, s, "G1")["unmapped_genre_term"]
    assert [i["term"] for i in issues] == ["faith & spirituality"] and issues[0]["section"] == "genre"


def test_ac07_halted_link_does_not_bring_imdb_genres(ac07):
    conn, s, ctx = ac07
    link = conn.execute("SELECT cross_source_mismatch_halt FROM task7.link_assessment WHERE extraction_id = %s "
                        "AND movie_id = %s", (s.eid, s.mid("G2"))).fetchone()
    assert link == (True,)
    g = _genres(conn, ctx, s, "G2")
    assert set(g) == {"Drama"} and g["Drama"][0] == "page_only"
    assert _sel(conn, ctx, s, "G2", "genres")["conflict_type"] == "none"


def test_ac07_reads_normalized_tags_not_the_whole_raw_string(ac07):
    """回归（第一步的教训）：raw_value 是整串，用 raw 时整串无法映射；读 normalized_value 才能逐标签映射，年龄段进 noise。"""
    conn, s, ctx = ac07
    assert set(_genres(conn, ctx, s, "G3")) == {"Comedy", "Family", "Romance"}
    t = _terms(conn, ctx, s, "G3")
    assert t[("genre", "10-12 years")][0] == "noise"
    assert not any(disp == "unmapped" for disp, _m, _a in t.values())


def test_ac07_no_mappable_term_is_missing(ac07):
    conn, s, ctx = ac07
    for movie in ("G4", "G5"):
        sel = _sel(conn, ctx, s, movie, "genres")
        assert sel["status"] == "missing" and sel["winning_value"] is None and sel["conflict_type"] == "none"
        assert _genres(conn, ctx, s, movie) == {}
    assert _terms(conn, ctx, s, "G4")[("genre", "spielberg")][0] == "unmapped"


def test_ac07_status_and_conflict_type_stay_inside_the_closed_sets(ac07):
    from etl_task7.rules import CONFLICT_TYPES, SELECTION_STATUSES
    conn, s, ctx = ac07
    for movie in ("G1", "G2", "G3", "G4", "G5"):
        for field in ("genres", "director", "starring", "cast"):
            sel = _sel(conn, ctx, s, movie, field)
            assert sel["status"] in SELECTION_STATUSES and sel["conflict_type"] in CONFLICT_TYPES


# ------------------------------------------------------------------ AC02 补完：合集类型不下推

def test_ac02_collection_genres_are_not_pushed_down_to_member_movies(conn):
    imdb = [IMDB("tt2000001", "Member Film", year=1995, genres=("Drama",))]
    movies = [M("Member", REL("imdb_t4_link", "tt2000001"))]
    eds = [E("B0BOX", "Horror Box Set [DVD]", kind="collection", members=((1, "Member", "Member Film"),),
             genre=("Horror, Thriller",), breadcrumb=("Anime & Manga",)),
           E("B0MEM", "Member Film [DVD]", movie="Member", genre=("Drama",))]
    s, ctx = _run(conn, w.World(imdb, movies, eds))
    assert set(_genres(conn, ctx, s, "Member")) == {"Drama"}
    assert all(asins == ["B0MEM"] for _d, _m, asins in _terms(conn, ctx, s, "Member").values())


def test_ac02_movie_known_only_through_a_collection_gets_imdb_genres_only(conn):
    imdb = [IMDB("tt2000002", "Contained Film", year=1995, genres=("Western",))]
    movies = [M("Contained", REL("imdb_t4_link", "tt2000002"))]
    eds = [E("B0BOX2", "Western Box [DVD]", kind="collection", members=((1, "Contained", "Contained Film"),),
             genre=("Horror",))]
    s, ctx = _run(conn, w.World(imdb, movies, eds))
    g = _genres(conn, ctx, s, "Contained")
    assert set(g) == {"Western"} and g["Western"][0] == "imdb_only"
    assert _terms(conn, ctx, s, "Contained") == {}


# ------------------------------------------------------------------ AC08 演职员并集、角色边界、顺序与覆盖范围

D1, D2, S1, A2 = "nm2000001", "nm2000002", "nm2000010", "nm2000011"


def _ac08_world() -> w.World:
    # C1：页面导演 D1（已关联）、主演 S1（三个版本署名序号 1、2、1）；IMDb 另有导演 D2、演员 A2
    c1, c1m, c1e = case("C1", "imdb_t4_link", imdb_title="Credits One", imdb_dirs=((D1, "Dee One"), (D2, "Dee Two")))
    c1.principals = ((S1, "Es One", "actor"), (A2, "Ay Two", "actress"))
    c1m.credits = (CR("Es One", "starring", S1, (("B0C1", 1), ("B0C1B", 2), ("B0C1C", 1))),)
    c1m.directors = (("Dee One", D1),)
    extra = [E("B0C1B", "Credits One [VHS]", movie="C1"), E("B0C1C", "Credits One [Blu-ray]", movie="C1")]
    # A2 在别的作品里是任务 5 已有的页面人物，person_id 来自别的锚点，不是 imdb: 公式
    other, otherm, othere = case("Other", "imdb_t4_link", imdb_title="Other Picture")
    otherm.credits = (CR("Ay Two", "cast", A2, (("B0Other", None),), anchor="mention:fix:Ay Two (page)"),)
    # C3：页面导演 John Doe 未关联，IMDb 导演 Jane Roe
    c3, c3m, c3e = case("C3", "imdb_t4_link", imdb_title="Credits Three", imdb_dirs=(("nm2000020", "Jane Roe"),))
    c3m.credits = (CR("John Doe", "director", None, (("B0C3", 1),)),)
    # C4：只有 IMDb 演员
    c4, c4m, c4e = case("C4", "imdb_t4_link", imdb_title="Credits Four")
    c4.principals = (("nm2000030", "Ann Actor", "actress"), ("nm2000031", "Bob Actor", "actor"))
    # C6：只有页面署名、没有 IMDb；C7：什么都没有
    c6, c6m, c6e = case("C6", None, imdb_title="Credits Six")
    c6m.credits = (CR("Pat Page", "cast", None, (("B0C6", None),)),)
    c7, c7m, c7e = case("C7", None, imdb_title="Credits Seven")
    world = world_of((c1, c1m, c1e), (other, otherm, othere), (c3, c3m, c3e), (c4, c4m, c4e), (c6, c6m, c6e),
                     (c7, c7m, c7e))
    world.editions += extra
    return world


@pytest.fixture
def ac08(conn):
    world = _ac08_world()
    s = h.seed(conn, world)
    before = h.upstream_digest(conn)
    ctx = h.run_stages(conn, s, upto="movie_multivalue", skip=("movie_scalar",))
    return conn, s, ctx, before


def test_ac08_directors_are_the_union_of_page_and_imdb(ac08):
    conn, s, ctx, _ = ac08
    d = _people(conn, ctx, s, "C1", "director")
    d1 = w.person_id("Dee One", D1)
    assert set(d) == {d1, imdb_pid(D2)}
    assert d[d1]["source_coverage"] == "both" and d[d1]["status"] == "adopted" and d[d1]["imdb_ordering"] == 1
    assert d[imdb_pid(D2)]["source_coverage"] == "imdb_only" and d[imdb_pid(D2)]["nconst"] == D2
    assert d[imdb_pid(D2)]["credit_order"] > d[d1]["credit_order"]          # IMDb 独有成员排在页面成员之后
    assert d[imdb_pid(D2)]["imdb_ordering"] == 2
    assert _sel(conn, ctx, s, "C1", "director")["status"] == "resolved"


def test_ac08_cast_unions_page_and_imdb_principals_and_starring_is_page_only(ac08):
    conn, s, ctx, _ = ac08
    s1 = w.person_id("Es One", S1)
    cast = _people(conn, ctx, s, "C1", "cast")
    assert set(cast) == {s1, w.person_id("Ay Two", A2, "mention:fix:Ay Two (page)")}
    assert cast[s1]["source_coverage"] == "both"
    starring = _people(conn, ctx, s, "C1", "starring")
    assert set(starring) == {s1} and starring[s1]["source_coverage"] == "page_only"       # IMDb 演员 A2 不进 starring
    assert _sel(conn, ctx, s, "C1", "starring")["winning_value"] == s1
    r = _result(conn, ctx, s, "C1")
    assert (r["starring_count"], r["cast_count"], r["director_count"]) == (1, 2, 2)


def test_ac08_imdb_only_person_reuses_task5_person_id_when_known(ac08):
    """A2 在任务 5 里已有 person_id（别的锚点），复用它，不重新登记；D2 没有，按公式登记到 imdb_person_ext。"""
    conn, s, ctx, _ = ac08
    reused = w.person_id("Ay Two", A2, "mention:fix:Ay Two (page)")
    assert reused != imdb_pid(A2)
    cast = _people(conn, ctx, s, "C1", "cast")
    assert reused in cast and imdb_pid(A2) not in cast and cast[reused]["source_coverage"] == "imdb_only"
    ext = {r[0]: r[1:] for r in conn.execute("SELECT nconst, person_id, in_task5_identity, primary_name "
                                             "FROM task7.imdb_person_ext WHERE extraction_id = %s", (s.eid,))}
    assert A2 not in ext and S1 not in ext and D1 not in ext
    assert ext[D2] == (imdb_pid(D2), False, "Dee Two")


def test_ac08_billing_order_takes_the_most_common_across_editions(ac08):
    conn, s, ctx, _ = ac08
    s1 = w.person_id("Es One", S1)
    for role in ("starring", "cast"):
        p = _people(conn, ctx, s, "C1", role)[s1]
        assert p["credit_order"] == 1
        assert p["resolution_note"]["billing"] == {"1": 2, "2": 1} and p["resolution_note"]["billing_conflict"] is True
        assert _sel(conn, ctx, s, "C1", role)["conflict_type"] == "billing_conflict_resolved"


def test_ac08_page_only_director_conflicts_with_imdb_director(ac08):
    conn, s, ctx, _ = ac08
    d = _people(conn, ctx, s, "C3", "director")
    doe = w.person_id("John Doe")
    assert d[doe]["status"] == "conflict_pending" and d[doe]["source_coverage"] == "page_only"
    assert d[imdb_pid("nm2000020")]["status"] == "adopted" and d[imdb_pid("nm2000020")]["source_coverage"] == "imdb_only"
    sel = _sel(conn, ctx, s, "C3", "director")
    assert sel["status"] == "resolved" and sel["conflict_type"] == "director_source_disagreement"
    assert sel["winning_value"] == imdb_pid("nm2000020")
    assert "director_source_disagreement" in _issues(conn, ctx, s, "C3")


def test_ac08_cast_coverage_follows_r43(ac08):
    conn, s, ctx, _ = ac08
    got = {m: _result(conn, ctx, s, m)["cast_coverage"] for m in ("C1", "C3", "C4", "C6", "C7")}
    assert got == {"C1": "page_and_imdb_principals", "C3": "none", "C4": "imdb_principals_only",
                   "C6": "page_only", "C7": "none"}


def test_ac08_imdb_only_actors_without_page_credits_and_no_starring(ac08):
    conn, s, ctx, _ = ac08
    cast = _people(conn, ctx, s, "C4", "cast")
    assert set(cast) == {imdb_pid("nm2000030"), imdb_pid("nm2000031")}
    assert [cast[imdb_pid(n)]["credit_order"] for n in ("nm2000030", "nm2000031")] == [1, 2]
    assert _sel(conn, ctx, s, "C4", "cast")["conflict_type"] == "none"
    s_sel = _sel(conn, ctx, s, "C4", "starring")
    assert s_sel["status"] == "missing" and _people(conn, ctx, s, "C4", "starring") == {}


def test_ac08_page_only_movie_keeps_page_members_without_imdb(ac08):
    conn, s, ctx, _ = ac08
    pat = w.person_id("Pat Page")
    cast = _people(conn, ctx, s, "C6", "cast")
    assert set(cast) == {pat} and cast[pat]["source_coverage"] == "page_only" and cast[pat]["status"] == "adopted"
    assert cast[pat]["credit_order"] is None                        # 只有详情要点来源：没有署名顺序
    assert _sel(conn, ctx, s, "C6", "director")["status"] == "missing"


def test_ac08_task5_and_other_upstream_tables_are_unchanged(ac08):
    conn, _s, _ctx, before = ac08
    assert h.upstream_digest(conn) == before


def test_ac08_people_carry_a_role_and_no_member_is_in_two_roles_by_accident(ac08):
    conn, s, ctx, _ = ac08
    rows = conn.execute("SELECT role, count(*) FROM task7.movie_person_resolved WHERE extraction_id = %s "
                        "AND movie_id = %s GROUP BY 1 ORDER BY 1", (s.eid, s.mid("C1"))).fetchall()
    assert rows == [("cast", 2), ("director", 2), ("starring", 1)]


# ------------------------------------------------------------------ AC22 同片署名对齐与多人串

def _ac22_world() -> w.World:
    cases = []

    def add(tag, dirs=(), principals=(), credits=(), source="imdb_t4_link"):
        i, m, e = case(tag, source, imdb_title=f"{tag} Picture", imdb_dirs=dirs)
        if i is not None:
            i.principals = tuple(principals)
        m.credits = tuple(credits)
        cases.append((i, m, e))

    add("C5", principals=[("nm3000001", "Gary Busey", "actor")],
        credits=[CR("Garey Busey", "cast", None, (("B0C5", None),))])
    add("C6", principals=[("nm3000010", "John Cusack", "actor"), ("nm3000011", "Joan Cusack", "actress")],
        credits=[CR("John Cusack", "cast", None, (("B0C6", None),))])
    add("C7", principals=[("nm3000020", "John Smith", "actor"), ("nm3000021", "Joan Smith", "actress")],
        credits=[CR("Jon Smith", "cast", None, (("B0C7", None),))])
    add("C8", dirs=(("nm3000030", "Tomás Gutiérrez Alea"), ("nm3000031", "Carlos Garcia Agraz")),
        credits=[CR("Tomas Guiterrez Alea; Carlos Garcia Agraz", "director", None, (("B0C8", 1),))])
    add("C9", principals=[("nm3000040", "Alpha Beta", "actor")],
        credits=[CR("Alpha Beta; Gamma Delta", "cast", None, (("B0C9", None),))])
    add("C10", source=None, credits=[CR("Xavier Yu; Zed Wu", "director", None, (("B0C10", 1),))])
    add("C11", dirs=(("nm3000050", "Real Person"),),
        credits=[CR("Black and Blue Productions", "director", None, (("B0C11", 1),))])
    return world_of(*cases)


@pytest.fixture
def ac22(conn):
    s = h.seed(conn, _ac22_world())
    before = h.upstream_digest(conn)
    ctx = h.run_stages(conn, s, upto="movie_multivalue", skip=("movie_scalar",))
    return conn, s, ctx, before


def test_ac22_misspelled_page_name_aligns_to_the_same_imdb_person(ac22):
    conn, s, ctx, _ = ac22
    cast = _people(conn, ctx, s, "C5", "cast")
    target = imdb_pid("nm3000001")
    assert set(cast) == {target}                      # 页面人物与 IMDb 人物是同一人，只有一个 person_id
    assert cast[target]["source_coverage"] == "both" and cast[target]["raw_names"] == ["Garey Busey"]
    assert cast[target]["nconst"] == "nm3000001" and cast[target]["resolution_note"]["alignment_level"] == "L4"
    (a,) = _align(conn, ctx, s, "C5")
    assert (a["role"], a["raw_name"], a["segment"], a["level"], a["outcome"]) == ("cast", "Garey Busey", 0, "L4", "aligned")
    assert a["page_person_id"] == w.person_id("Garey Busey") and a["resolved_person_id"] == target
    assert _sel(conn, ctx, s, "C5", "cast")["conflict_type"] == "formatting_or_alias"


def test_ac22_strict_match_beats_a_near_match(ac22):
    conn, s, ctx, _ = ac22
    cast = _people(conn, ctx, s, "C6", "cast")
    assert set(cast) == {imdb_pid("nm3000010"), imdb_pid("nm3000011")}
    assert cast[imdb_pid("nm3000010")]["source_coverage"] == "both"
    assert cast[imdb_pid("nm3000011")]["source_coverage"] == "imdb_only"           # Joan Cusack 不是页面那个人
    (a,) = _align(conn, ctx, s, "C6")
    assert a["level"] == "L1" and a["nconst"] == "nm3000010"


def test_ac22_two_hits_at_the_same_level_do_not_align(ac22):
    conn, s, ctx, _ = ac22
    cast = _people(conn, ctx, s, "C7", "cast")
    jon = w.person_id("Jon Smith")
    assert cast[jon]["status"] == "conflict_pending" and cast[jon]["source_coverage"] == "page_only"
    assert cast[jon]["resolution_note"]["alignment"]["outcome"] == "tie_not_aligned"
    assert cast[imdb_pid("nm3000020")]["source_coverage"] == "imdb_only" and cast[imdb_pid("nm3000021")]["status"] == "adopted"
    (a,) = _align(conn, ctx, s, "C7")
    assert a["outcome"] == "tie_not_aligned" and a["resolved_person_id"] is None and a["level"] == "L4"


def test_ac22_multi_person_string_aligns_only_when_every_segment_matches(ac22):
    conn, s, ctx, _ = ac22
    d = _people(conn, ctx, s, "C8", "director")
    assert set(d) == {imdb_pid("nm3000030"), imdb_pid("nm3000031")}                # 原行被取代
    assert w.person_id("Tomas Guiterrez Alea; Carlos Garcia Agraz") not in d
    assert all(p["source_coverage"] == "both" and p["status"] == "adopted" for p in d.values())
    rows = _align(conn, ctx, s, "C8")
    assert [(r["segment"], r["segment_name"], r["level"], r["outcome"]) for r in rows] == [
        (0, "Tomas Guiterrez Alea", "L4", "aligned"), (1, "Carlos Garcia Agraz", "L1", "aligned")]
    assert {r["raw_name"] for r in rows} == {"Tomas Guiterrez Alea; Carlos Garcia Agraz"}   # 原始署名原样保留
    assert _sel(conn, ctx, s, "C8", "director")["status"] == "resolved"


def test_ac22_partially_matched_multi_person_string_keeps_the_original_row(ac22):
    conn, s, ctx, _ = ac22
    cast = _people(conn, ctx, s, "C9", "cast")
    raw = w.person_id("Alpha Beta; Gamma Delta")
    assert cast[raw]["status"] == "conflict_pending" and cast[raw]["raw_names"] == ["Alpha Beta; Gamma Delta"]
    assert cast[imdb_pid("nm3000040")]["source_coverage"] == "imdb_only"            # 没有取代原行，所以不是 both
    rows = _align(conn, ctx, s, "C9")
    assert {r["outcome"] for r in rows} == {"unsplit_multi_person"}
    assert [(r["segment_name"], r["nconst"]) for r in rows] == [("Alpha Beta", "nm3000040"), ("Gamma Delta", None)]
    assert "unsplit_multi_person" in _issues(conn, ctx, s, "C9")


def test_ac22_multi_person_string_without_imdb_is_kept_and_unresolved(ac22):
    conn, s, ctx, _ = ac22
    d = _people(conn, ctx, s, "C10", "director")
    raw = w.person_id("Xavier Yu; Zed Wu")
    assert set(d) == {raw} and d[raw]["status"] == "conflict_pending"
    sel = _sel(conn, ctx, s, "C10", "director")
    assert sel["status"] == "conflict_unresolved" and sel["winning_value"] is None
    assert sel["conflict_type"] == "intra_source_tie_unresolved"
    assert "unsplit_multi_person" in _issues(conn, ctx, s, "C10")


def test_ac22_company_name_with_and_is_not_split_into_people(ac22):
    conn, s, ctx, _ = ac22
    d = _people(conn, ctx, s, "C11", "director")
    black = w.person_id("Black and Blue Productions")
    assert d[black]["status"] == "conflict_pending" and d[black]["raw_names"] == ["Black and Blue Productions"]
    assert d[imdb_pid("nm3000050")]["status"] == "adopted"
    assert _sel(conn, ctx, s, "C11", "director")["status"] == "resolved"
    assert "unsplit_multi_person" in _issues(conn, ctx, s, "C11")


def test_ac22_no_person_is_created_for_a_segment_and_task5_is_untouched(ac22):
    conn, s, ctx, before = ac22
    segment_ids = {w.person_id(n) for n in ("Tomas Guiterrez Alea", "Carlos Garcia Agraz", "Alpha Beta", "Gamma Delta",
                                            "Xavier Yu", "Zed Wu", "Black", "Blue Productions")}
    used = {r[0] for r in conn.execute("SELECT person_id FROM task7.movie_person_resolved WHERE extraction_id = %s",
                                       (s.eid,))}
    assert not (used & segment_ids)
    assert h.upstream_digest(conn) == before


# ------------------------------------------------------------------ AC22 t7r2 姓名兼容放宽（R16、R38、R41）

def _relax_world() -> w.World:
    cases = []

    def add(tag, dirs=(), principals=(), credits=()):
        i, m, e = case(tag, "imdb_t4_link", imdb_title=f"{tag} Picture", imdb_dirs=dirs)
        i.principals = tuple(principals)
        m.credits = tuple(credits)
        cases.append((i, m, e))

    # 真实形态：267 位页面独有导演多为这两类，不放宽时 R41 把页面名标为 conflict_pending
    add("C12", dirs=(("nm5000010", "J.P. Dutta"),), credits=[CR("J.P.Dutta", "director", None, (("B0C12", 1),))])
    add("C13", dirs=(("nm5000020", "Rakesh Chaturvedi Om"),),
        credits=[CR("Rakesh Chaturvedi", "director", None, (("B0C13", 1),))])
    # 单个词的名字不触发 LS；中间多出一个词由 level 原有规则处理（L3，两版相同）
    add("C14", principals=[("nm5000030", "Madonna Louise", "actress")],
        credits=[CR("Madonna", "cast", None, (("B0C14", None),))])
    add("C16", principals=[("nm5000050", "Michael B. Jordan", "actor")],
        credits=[CR("Michael Jordan", "cast", None, (("B0C16", None),))])
    # 同片同角色里两人都与页面名构成 LS：不对齐
    add("C15", principals=[("nm5000040", "Rakesh Chaturvedi Om", "actor"), ("nm5000041", "Rakesh Chaturvedi Shah", "actor")],
        credits=[CR("Rakesh Chaturvedi", "cast", None, (("B0C15", None),))])
    # 多人串：一段是格式差异，一段是词序子集，t7r2 下全部成段命中
    add("C17", dirs=(("nm5000060", "J.P. Dutta"), ("nm5000061", "Rakesh Chaturvedi Om")),
        credits=[CR("J.P.Dutta; Rakesh Chaturvedi", "director", None, (("B0C17", 1),))])
    return world_of(*cases)


@pytest.fixture
def ac22_relax(conn):
    s = h.seed(conn, _relax_world())
    old = h.run_stages(conn, s, upto="movie_multivalue", skip=("movie_scalar",), rule_version="t7r1")
    new = h.run_stages(conn, s, upto="movie_multivalue", skip=("movie_scalar",), rule_version="t7r2")
    return conn, s, old, new


def test_ac22_t7r2_aligns_format_only_page_director_instead_of_marking_it_pending(ac22_relax):
    conn, s, old, new = ac22_relax
    page = w.person_id("J.P.Dutta")
    # 修复前（t7r1）：页面独有导演 conflict_pending，字段为 director_source_disagreement
    d = _people(conn, old, s, "C12", "director")
    assert d[page]["status"] == "conflict_pending" and d[imdb_pid("nm5000010")]["status"] == "adopted"
    assert _sel(conn, old, s, "C12", "director")["conflict_type"] == "director_source_disagreement"
    assert "director_source_disagreement" in _issues(conn, old, s, "C12")
    # t7r2：同一人，只剩一个 person_id，覆盖范围 both，没有 R41 分歧
    d = _people(conn, new, s, "C12", "director")
    assert set(d) == {imdb_pid("nm5000010")}
    assert d[imdb_pid("nm5000010")]["source_coverage"] == "both" and d[imdb_pid("nm5000010")]["raw_names"] == ["J.P.Dutta"]
    assert d[imdb_pid("nm5000010")]["resolution_note"]["alignment_level"] == "L1"
    assert _sel(conn, new, s, "C12", "director")["conflict_type"] == "formatting_or_alias"
    assert "director_source_disagreement" not in _issues(conn, new, s, "C12")
    (a,) = _align(conn, new, s, "C12")
    assert (a["raw_name"], a["level"], a["outcome"], a["resolved_person_id"]) == ("J.P.Dutta", "L1", "aligned", imdb_pid("nm5000010"))


def test_ac22_t7r2_subset_name_aligns_at_level_ls(ac22_relax):
    conn, s, old, new = ac22_relax
    page = w.person_id("Rakesh Chaturvedi")
    assert _people(conn, old, s, "C13", "director")[page]["status"] == "conflict_pending"           # 修复前
    d = _people(conn, new, s, "C13", "director")
    assert set(d) == {imdb_pid("nm5000020")} and d[imdb_pid("nm5000020")]["source_coverage"] == "both"
    assert d[imdb_pid("nm5000020")]["resolution_note"]["alignment_level"] == "LS"
    (a,) = _align(conn, new, s, "C13")
    assert (a["raw_name"], a["level"], a["outcome"], a["nconst"]) == ("Rakesh Chaturvedi", "LS", "aligned", "nm5000020")
    assert _sel(conn, new, s, "C13", "director")["status"] == "resolved"


def test_ac22_t7r2_single_word_name_does_not_align(ac22_relax):
    conn, s, old, new = ac22_relax
    for ctx in (old, new):
        cast = _people(conn, ctx, s, "C14", "cast")
        assert cast[w.person_id("Madonna")]["source_coverage"] == "page_only"
        assert cast[imdb_pid("nm5000030")]["source_coverage"] == "imdb_only"
        (a,) = _align(conn, ctx, s, "C14")
        assert (a["outcome"], a["resolved_person_id"], a["level"]) == ("no_match", None, None)


def test_ac22_t7r2_middle_word_is_handled_by_the_existing_level_not_ls(ac22_relax):
    conn, s, old, new = ac22_relax
    for ctx in (old, new):
        (a,) = _align(conn, ctx, s, "C16")
        assert (a["level"], a["outcome"]) == ("L3", "aligned")


def test_ac22_t7r2_two_subset_hits_in_the_same_film_and_role_do_not_align(ac22_relax):
    conn, s, old, new = ac22_relax
    page = w.person_id("Rakesh Chaturvedi")
    # t7r1 里两人都不兼容，页面行照常采用；t7r2 里两人并列 LS：都不对齐，页面行保留冲突
    for ctx, status in ((old, "adopted"), (new, "conflict_pending")):
        cast = _people(conn, ctx, s, "C15", "cast")
        assert cast[page]["status"] == status
        assert cast[imdb_pid("nm5000040")]["source_coverage"] == "imdb_only"
        assert cast[imdb_pid("nm5000041")]["source_coverage"] == "imdb_only"
    (a,) = _align(conn, new, s, "C15")
    assert (a["outcome"], a["level"], a["resolved_person_id"]) == ("tie_not_aligned", "LS", None)
    assert _people(conn, new, s, "C15", "cast")[w.person_id("Rakesh Chaturvedi")]["resolution_note"]["alignment"] == {
        "outcome": "tie_not_aligned", "level": "LS", "tied": ["nm5000040", "nm5000041"]}


def test_ac22_t7r2_multi_person_string_aligns_through_format_and_subset_segments(ac22_relax):
    conn, s, old, new = ac22_relax
    raw = w.person_id("J.P.Dutta; Rakesh Chaturvedi")
    assert _people(conn, old, s, "C17", "director")[raw]["status"] == "conflict_pending"
    assert "unsplit_multi_person" in _issues(conn, old, s, "C17")
    d = _people(conn, new, s, "C17", "director")
    assert set(d) == {imdb_pid("nm5000060"), imdb_pid("nm5000061")} and raw not in d          # 原行被取代
    assert all(p["source_coverage"] == "both" and p["status"] == "adopted" for p in d.values())
    assert [(r["segment_name"], r["level"], r["outcome"]) for r in _align(conn, new, s, "C17")] == [
        ("J.P.Dutta", "L1", "aligned"), ("Rakesh Chaturvedi", "LS", "aligned")]
    assert "unsplit_multi_person" not in _issues(conn, new, s, "C17")


def test_ac22_t7r2_creates_no_person_id_from_a_page_spelling(ac22_relax):
    conn, s, _old, new = ac22_relax
    pages = {w.person_id(n) for n in ("J.P.Dutta", "Rakesh Chaturvedi", "J.P.Dutta; Rakesh Chaturvedi")}
    used = {r[0] for r in conn.execute("SELECT person_id FROM task7.movie_person_resolved WHERE extraction_id = %s "
                                       "AND stage_key = %s AND status = 'adopted'",
                                       (s.eid, new.inputs.stage_key("movie_multivalue")))}
    assert not (used & pages)


# ------------------------------------------------------------------ AC21 补完：读取同一条关联判定

def test_ac21_movie_multivalue_reads_the_persisted_link_assessment(conn):
    """把一条未熔断的评估改成已熔断（保持 CHECK 成立）后重新运行 movie_multivalue，IMDb 类型与演职员随之消失：
    说明读取的是 link_assessment，而不是自己重算。"""
    i, m, e = case("L4X", "imdb_t6_cooccur", imdb_title="Linked Four", imdb_dirs=(("nm4000001", "Ima Director"),),
                   edition_extra=dict(genre=("Drama",)))
    i.genres = ("Horror",)
    i.principals = (("nm4000002", "Ima Actor", "actor"),)
    s = h.seed(conn, w.World([i], [m], [e]))
    ctx = h.run_stages(conn, s, upto="link_assess")
    link = conn.execute("SELECT cross_source_mismatch_halt FROM task7.link_assessment WHERE extraction_id = %s",
                        (s.eid,)).fetchone()
    assert link == (False,)
    from etl_task7 import movies_multivalue
    from etl_task7.rules import ruleset
    movies_multivalue.resolve_movies_multivalue(conn, ctx, ruleset(ctx.inputs.rule_version))
    assert set(_genres(conn, ctx, s, "L4X")) == {"Drama", "Horror"}
    assert set(_people(conn, ctx, s, "L4X", "director")) == {imdb_pid("nm4000001")}
    conn.execute("DELETE FROM task7.entity_stage WHERE stage = 'movie_multivalue'")
    conn.execute("DELETE FROM task7.movie_genre")
    conn.execute("DELETE FROM task7.movie_genre_term")
    conn.execute("DELETE FROM task7.movie_person_resolved")
    conn.execute("DELETE FROM task7.credit_alignment")
    conn.execute("DELETE FROM task7.movie_multivalue_stage_result")
    conn.execute("UPDATE task7.link_assessment SET cross_source_mismatch_halt = true, halt_reason = 'Y+RT'")
    movies_multivalue.resolve_movies_multivalue(conn, ctx, ruleset(ctx.inputs.rule_version))
    assert set(_genres(conn, ctx, s, "L4X")) == {"Drama"}
    assert _people(conn, ctx, s, "L4X", "director") == {} and _people(conn, ctx, s, "L4X", "cast") == {}
