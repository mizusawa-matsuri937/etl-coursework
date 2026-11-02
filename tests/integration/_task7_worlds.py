"""任务 7 运行与报告测试共用的案例世界：small_world（几部作品，足够分批）与 rich_world（覆盖 R60 的各类实例）。
预期值在各测试里按 spec 手算；这里只负责造数据。"""

from __future__ import annotations

import _task7_helpers as h
from task7 import world as w

M, E, IMDB, REL, CR = w.Movie, w.Ed, w.Imdb, w.Release, w.Credit
case, world_of = h.case, h.world_of


def small_world(n: int = 5) -> w.World:
    """n 部作品（强关联，各一个 edition 商品，带类型、导演、演员与页面署名），批大小 2 时每个阶段至少 3 批。"""
    cases = []
    for k in range(1, n + 1):
        tag = f"S{k}"
        i, m, e = case(tag, "imdb_t4_link", imdb_title=f"Small Picture {k}", imdb_year=1980 + k, imdb_runtime=90 + k,
                       page_runtime=90 + k, imdb_dirs=((f"nm50000{k}0", f"Dina Direct{k}"),),
                       edition_extra=dict(genre=("Drama, Action & Adventure",), breadcrumb=("Movies & TV",),
                                          publisher=(("Warner Home Video", "detail_bullets"),)))
        i.genres = ("Drama", "Thriller")
        i.principals = ((f"nm50000{k}1", f"Acton Star{k}", "actor"),)
        m.credits = (CR(f"Acton Star{k}", "starring", f"nm50000{k}1", ((f"B0{tag}", 1),)),
                     CR(f"Dina Direct{k}", "director", f"nm50000{k}0"),)
        cases.append((i, m, e))
    return world_of(*cases)


def rich_world() -> w.World:
    """覆盖 R60 全部类别：标题别名、年份分歧与错配熔断、片长各类差异、演职员并集与署名对齐、多人串、署名顺序、
    类型映射、分级、版本层发行商与发售日、共享 tconst。"""
    cases = []
    # 标题：页面带品牌括号，IMDb 有别名；年份相差 1 年与相差 2 年以上（强关联，导演兼容）；弱匹配年份与片长同时矛盾 → 熔断
    cases.append(case("Sam", "imdb_t4_link", imdb_title="Seven Samurai", imdb_original="Shichinin no samurai",
                      imdb_year=1954, title_years=(1954,), page_title="Seven Samurai (Criterion Collection) [DVD]",
                      akas=(("Seven Samurai", "US", "en"), ("Les Sept Samourais", "FR", "fr"))))
    cases.append(case("Y1", "imdb_t4_link", imdb_year=1985, title_years=(1986,)))
    cases.append(case("Y2", "imdb_t4_link", imdb_year=1954, title_years=(1998,), imdb_dirs=(("nm6000001", "Sam Director"),),
                      page_dirs=(("Sam Director", None),)))
    cases.append(case("Y4", "imdb_t6_cooccur", imdb_year=1990, imdb_runtime=100, title_years=(2010,), page_runtime=140))
    # 片长：R1 的四个版本、R6 差 10 分钟、R2 差 45 分钟
    r1 = case("R1", "imdb_t4_link", imdb_runtime=100, page_runtime=101)
    cases.append(r1)
    cases.append(case("R6", "imdb_t4_link", imdb_runtime=100, page_runtime=110))
    cases.append(case("R2", "imdb_t4_link", imdb_runtime=90, page_runtime=135))
    extra = [E("B0R1B", "R1 Picture [PAL DVD]", movie="R1", runtime=((96, "detail_bullets"),),
               video_system=(("PAL", "detail_bullets"),)),
             E("B0R1B2", "R1 Picture [VHS]", movie="R1", runtime=((96, "detail_bullets"),)),
             E("B0R1C", "R1 Picture (Director's Cut)", movie="R1", runtime=((125, "detail_bullets"),),
               cut_type=(("directors cut", "title"),))]
    # 分级：具体分级与未评级并存；版本层发行商与发售日
    p1 = case("P1", "imdb_t4_link", edition_extra=dict(rating=(("R (Restricted)", "detail_bullets"),)))
    extra.append(E("B0P1B", "P1 Picture [VHS]", movie="P1", rating=(("NR (Not Rated)", "detail_bullets"),)))
    cases.append(p1)
    cases.append((None, M("Mv1"), None))
    extra += [E("B0E1", "Warner Film [DVD]", movie="Mv1", publisher=(("Warner Home Video", "detail_bullets"),
                                                                       ("Warner Bros.", "product_overview")),
                dates=(("media_release", "2003-05-20", ()), ("listing_first_available", "2001-09-10", ()))),
              E("B0E2", "Sony Film", movie="Mv1", publisher=(("Universal Studios", "product_overview"),
                                                             ("Sony Pictures", "detail_bullets")))]
    # 类型：IMDb 与页面并集、未映射词面
    g1 = case("G1", "imdb_t4_link", edition_extra=dict(genre=("Action & Adventure, Science Fiction & Fantasy See more",
                                                              "faith_&_spirituality"),
                                                       breadcrumb=("Movies & TV", "DVD", "Anime & Manga")))
    g1[0].genres = ("Action", "Sci-Fi")
    cases.append(g1)
    # 演职员：主演序号 1、2、1；错别字对齐；多人串
    c1 = case("C1", "imdb_t4_link", imdb_dirs=(("nm7000001", "Dee One"), ("nm7000002", "Dee Two")))
    c1[0].principals = (("nm7000010", "Es One", "actor"), ("nm7000011", "Ay Two", "actress"))
    c1[1].credits = (CR("Es One", "starring", "nm7000010", (("B0C1", 1), ("B0C1B", 2), ("B0C1C", 1))),
                     CR("Dee One", "director", "nm7000001"))
    cases.append(c1)
    extra += [E("B0C1B", "C1 Picture [VHS]", movie="C1"), E("B0C1C", "C1 Picture [Blu-ray]", movie="C1")]
    c5 = case("C5", "imdb_t4_link")
    c5[0].principals = (("nm7000020", "Gary Busey", "actor"),)
    c5[1].credits = (CR("Garey Busey", "cast", None, (("B0C5", None),)),)
    cases.append(c5)
    c8 = case("C8", "imdb_t4_link", imdb_dirs=(("nm7000030", "Tomás Gutiérrez Alea"), ("nm7000031", "Carlos Garcia Agraz")))
    c8[1].credits = (CR("Tomas Guiterrez Alea; Carlos Garcia Agraz", "director", None, (("B0C8", 1),)),)
    cases.append(c8)
    c9 = case("C9", "imdb_t4_link")
    c9[0].principals = (("nm7000040", "Alpha Beta", "actor"),)
    c9[1].credits = (CR("Alpha Beta; Gamma Delta", "cast", None, (("B0C9", None),)),)
    cases.append(c9)
    # 共享 tconst：已熔断、标题相等且导演兼容
    cases.append(case("Q1a", "imdb_t4_link", tconst="tt7000001", imdb_title="Shared Picture One"))
    cases.append(case("Q1b", "imdb_t6_cooccur", tconst="tt7000001", imdb_title="Shared Picture One", title_years=(2010,),
                      page_runtime=150))
    cases.append(case("Q2a", "imdb_t4_link", tconst="tt7000002", imdb_title="Shared Picture Two",
                      imdb_dirs=(("nm7000050", "Eve Epsilon"),), page_dirs=(("Eve Epsilon", None),)))
    cases.append(case("Q2b", "imdb_t5_hint", tconst="tt7000002", imdb_title="Shared Picture Two",
                      imdb_dirs=(("nm7000050", "Eve Epsilon"),), page_dirs=(("E. Epsilon", None),)))
    imdb = {c[0].tconst: c[0] for c in cases if c[0] is not None}
    movies = [c[1] for c in cases]
    editions = [c[2] for c in cases if c[2] is not None] + extra
    world = w.World(list(imdb.values()), movies, editions)
    world.shared = [("Q1b", "tt7000001", "Q1a", "candidate_shared"), ("Q2b", "tt7000002", "Q2a", "candidate_shared")]
    return world
