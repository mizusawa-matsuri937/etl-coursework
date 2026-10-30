"""任务 6 合成夹具（AC01–AC17）：手写的 IMDb 作品、演职员、人物、商品、评论与任务 5 线索。

预期值见 README.md。商品用 tests/integration/_task4_helpers.P 描述；评论的时间与正文在 REVIEWS 里按 ASIN 给出
（其余商品没有评论）；任务 5 的升级线索在 HINTS 里给出，由测试辅助直接写成 task5.t4_upgrade_hint 行。
IMDb 数据交给 task4 的 imdb_files.build_snapshot 现场写成 .tsv.gz 并装载，任务 4 映射版本由测试里
真正运行 etl_task4.runner.execute_match 得到。

设计要点（与 spec 的案例一一对应）：
- B0T6A1*/B0T6B10001/B0T6V10001 经任务 4 自动关联（titleType 各为 movie/tvMovie/video）→ verified；
- B0T6D10001 关联的 tt0006010 没有 startYear（由辅助直接把该商品的作品标为 imdb_linked），不出候选；
- Film J / Film K2 在 IMDb 里有对应作品（各带一位导演），合集 B0T6FLJ003 页面同时署名两位导演，
  使两个成员各自满足 R22 自动关联：成员 1 归到 B0T6FLJ001/002 的作品（edition），成员 2 的新作品
  只经 contains 出现（imdb:tt0006061，verified 2002）；
- 共现反查的商品标题都带“系列说明尾巴”，任务 4 的标题键对不上、保持 page_only；
- 线索商品同理（标题键不等于 IMDb 标题键，防止任务 4 自动关联）。
"""

from __future__ import annotations

N = None

# ------------------------------------------------------------------ IMDb 一侧
IMDB = dict(
    basics=[
        ["tt0006001", "movie", "Alpha Film", "Alpha Film", "0", "1999", N, "100", "Drama"],
        ["tt0006002", "tvMovie", "Beta Tale", "Beta Tale", "0", "2004", N, "90", "Drama"],
        ["tt0006003", "video", "Video Works", "Video Works", "0", "2001", N, "85", "Drama"],
        ["tt0006010", "movie", "Null Year Film", N, "0", N, N, N, N],          # startYear 为 \N
        ["tt0006011", "movie", "Hinted Feature", "Hinted Feature", "0", "1975", N, "96", "Drama"],
        ["tt0006012", "movie", "First Fantasy", "First Fantasy", "0", "2001", N, "95", "Drama"],
        ["tt0006013", "movie", "Second Fantasy", "Second Fantasy", "0", "2002", N, "95", "Drama"],
        ["tt0006020", "movie", "Treasure Hunt Island", "Treasure Hunt Island", "0", "1950", N, "88", "Adventure"],
        ["tt0006024", "movie", "Land Before Saga", "Land Before Saga", "0", "1988", N, "70", "Animation"],
        ["tt0006030", "movie", "Ambi One", "Ambi One", "0", "1950", N, "90", "Drama"],
        ["tt0006031", "movie", "Ambi One", "Ambi One", "0", "1972", N, "96", "Drama"],
        ["tt0006032", "movie", "Ambi Two", "Ambi Two", "0", "1950", N, "91", "Drama"],
        ["tt0006033", "movie", "Ambi Two", "Ambi Two", "0", "1972", N, "97", "Drama"],
        ["tt0006040", "movie", "Film F", "Film F", "0", "1990", N, "99", "Drama"],
        ["tt0006041", "movie", "Film G", "Film G", "0", "1990", N, "98", "Drama"],
        ["tt0006042", "movie", "Film H", "Film H", "0", "1990", N, "97", "Drama"],
        ["tt0006043", "movie", "Indie Picture", "Indie Picture", "0", "2001", N, "94", "Drama"],
        ["tt0006044", "movie", "Another Legacy", "Another Legacy", "0", "1970", N, "93", "Drama"],
        ["tt0006045", "movie", "Lucky Legacy", "Lucky Legacy", "0", "1962", N, "92", "Drama"],
        ["tt0006046", "movie", "Neutral Numbers", "Neutral Numbers", "0", "1966", N, "91", "Drama"],
        ["tt0006049", "movie", "Bound Odyssey", "Bound Odyssey", "0", "2004", N, "100", "Drama"],
        ["tt0006050", "movie", "Early Review Saga", "Early Review Saga", "0", "2004", N, "101", "Drama"],
        ["tt0006051", "movie", "Clash Century", "Clash Century", "0", "2004", N, "102", "Drama"],
        ["tt0006060", "movie", "Film J", "Film J", "0", "2001", N, "95", "Drama"],
        ["tt0006061", "movie", "Film K2", "Film K2", "0", "2002", N, "96", "Drama"],
    ],
    akas=[],
    crew=[
        ["tt0006001", "nm0006001", N], ["tt0006002", "nm0006003", N], ["tt0006003", "nm0006004", N],
        ["tt0006010", N, N], ["tt0006011", "nm0006011", N], ["tt0006012", N, N], ["tt0006013", N, N],
        ["tt0006020", "nm0006020", N], ["tt0006024", N, N],
        ["tt0006030", "nm0006030", N], ["tt0006031", "nm0006030", N], ["tt0006032", "nm0006030", N],
        ["tt0006033", "nm0006030", N],
        ["tt0006040", "nm0006040", N], ["tt0006041", "nm0006041", N], ["tt0006042", "nm0006042", N],
        ["tt0006043", N, N], ["tt0006044", N, N], ["tt0006045", "nm0006045", N], ["tt0006046", "nm0006046", N],
        ["tt0006049", "nm0006049", N], ["tt0006050", "nm0006050", N], ["tt0006051", "nm0006051", N],
        ["tt0006060", "nm0006060", N], ["tt0006061", "nm0006061", N],
    ],
    principals=[
        ["tt0006001", "1", "nm0006002", "actor", N, N],
        ["tt0006024", "1", "nm0006022", "actor", N, N], ["tt0006024", "2", "nm0006023", "actress", N, N],
    ],
    names=[
        ["nm0006001", "Ann Auteur", "1960", N, "director", N],
        ["nm0006002", "Bob Bravo", "1970", N, "actor", N],
        ["nm0006003", "Cy Director", "1950", N, "director", N],
        ["nm0006004", "Dee Director", "1955", N, "director", N],
        ["nm0006011", "Eve Executive", "1940", N, "director", N],
        ["nm0006020", "Fletcher Frame", "1910", N, "director", N],
        ["nm0006022", "Heidi Helper", "1960", N, "actor", N],
        ["nm0006023", "Ivan Idle", "1965", N, "actress", N],
        ["nm0006030", "Ambi Director", "1920", N, "director", N],
        ["nm0006040", "Finn Flimmer", "1950", N, "director", N],
        ["nm0006041", "Gale Guide", "1951", N, "director", N],
        ["nm0006042", "Hank Helmer", "1952", N, "director", N],
        ["nm0006045", "Lorne Legend", "1930", N, "director", N],
        ["nm0006046", "Ned Navigator", "1935", N, "director", N],
        ["nm0006049", "Oscar Overseer", "1960", N, "director", N],
        ["nm0006050", "Quinn Quiet", "1961", N, "director", N],
        ["nm0006051", "Rita Retro", "1962", N, "director", N],
        ["nm0006060", "Jay Joiner", "1970", N, "director", N],
        ["nm0006061", "Kay Keeper", "1975", N, "director", N],
    ],
)


def products():
    from _task4_helpers import P

    return [
        # AC01：不参与匹配的剧集（有页面日期与评论，但不应出现在 task6 的商品层）
        P("B0T6TV0001", "tv_series", "Some Series Season 1", release_date="October 5, 2005"),
        # AC02/AC03：verified 的 movie / tvMovie / video；Alpha Film 两个版本、两个发售日期
        P("B0T6A10001", "movie", "Alpha Film (1999)", directors=("Ann Auteur",), actors=("Bob Bravo",),
          release_date="May 20, 2003"),
        P("B0T6A10002", "movie", "Alpha Film (1999) [VHS]", directors=("Ann Auteur",), actors=("Bob Bravo",),
          release_date="January 1, 2001"),
        P("B0T6B10001", "movie", "Beta Tale (2004)", directors=("Cy Director",),
          release_date="June 6, 2004"),
        P("B0T6V10001", "movie", "Video Works (2001)", directors=("Dee Director",)),
        # AC03：作品的 tconst 指向没有 startYear 的 tt0006010（辅助在任务 4 匹配后手工标为 imdb_linked）
        P("B0T6D10001", "movie", "Null Year Film"),
        # AC04：线索候选 / 线索 tconst 已被占用 / 合集成员线索 / 两个 tconst 的线索
        P("B0T6HNT001", "movie", "Hinted Feature Special Edition", directors=("Eve Executive",)),
        P("B0T6OWN001", "movie", "Owned Feature Collector Series"),
        P("B0T6MHA001", "movie", "Multi Hint Saga (1999)", directors=("Mona Mover",)),
        P("B0T6MHA002", "movie", "Multi Hint Saga (2001) [VHS]", directors=("Mona Mover",)),
        # AC05：共现反查的六个案例（标题带尾巴，任务 4 全部保持 page_only）
        P("B0T6TRS001", "movie", "Treasure Hunt Island Walt Disney Film Classics [VHS]",
          directors=("Fletcher Frame",)),
        P("B0T6SOL001", "movie", "Solo Star Show [VHS]", actors=("Heidi Helper",)),
        P("B0T6DIS001", "movie", "Distant Words [VHS]", actors=("Heidi Helper", "Ivan Idle")),
        P("B0T6LND001", "movie", "Land Before Saga VIII [VHS]", actors=("Heidi Helper", "Ivan Idle")),
        P("B0T6AM1001", "movie", "Ambi One Fun Edition [VHS]", directors=("Ambi Director",)),
        P("B0T6SHR001", "movie", "Beta Tale Extended Version [VHS]", directors=("Cy Director",)),
        # AC06：经评论年份消歧的共现；只有评论年份提及、没有候选的作品
        P("B0T6AM2001", "movie", "Ambi Two Fun Edition [VHS]", directors=("Ambi Director",)),
        P("B0T6RVT001", "movie", "Review Talk [VHS]"),
        # AC07：商品标题年份 一致 / 不一致 / 单一
        P("B0T6FLC001", "movie", "Film C (1985)", directors=("Kip Keeper",),
          extra=(("detail_bullets", "Publication date", "March 4, 2002", "product_release_date"),)),
        P("B0T6FLC002", "movie", "Film C (1985) [VHS]", directors=("Kip Keeper",),
          extra=(("detail_bullets", "Date First Available", "July 9, 2004", "product_release_date"),)),
        P("B0T6FLD001", "movie", "Film D (1985)", directors=("Dot Doubter",)),
        P("B0T6FLD002", "movie", "Film D (1987)", directors=("Dot Doubter",)),
        P("B0T6FLE001", "movie", "Film E (1986)", release_date="February 11, 2014"),
        # AC08：verified 与标题年份一致 / 相差 1 年 / 相差 4 年（H 第二个商品由辅助并入）；线索与标题同年；
        # 线索与共现 tconst 不同；只有共现与标题年份
        P("B0T6FLF001", "movie", "Film F (1990)", directors=("Finn Flimmer",)),
        P("B0T6FLG001", "movie", "Film G (1991)", directors=("Gale Guide",)),
        P("B0T6FLH001", "movie", "Film H (1990)", directors=("Hank Helmer",)),
        P("B0T6FLH002", "movie", "Film H (1994) [VHS]", directors=("Hank Helmer",)),
        P("B0T6IDX001", "movie", "Indexed Indie Special Cut (2001)", directors=("Ivor Indie",)),
        P("B0T6LUK001", "movie", "Lucky Legacy Adventure Cut [VHS]", directors=("Lorne Legend",)),
        P("B0T6NEU001", "movie", "Neutral Numbers Special Disk (1966)", directors=("Ned Navigator",)),
        # AC10：两个版本的 Film J 与含 Film J、Film K2 的合集（K2 只经 contains 出现）；Film K 没有评论。
        # 合集页面署名两位成员各自的导演，使两个成员都能满足 R22 自动关联（人物取自合集页面，R10）
        P("B0T6FLJ001", "movie", "Film J (2001)", directors=("Jay Joiner",)),
        P("B0T6FLJ002", "movie", "Film J (2001) [VHS]", directors=("Jay Joiner",)),
        P("B0T6FLJ003", "collection", "Film J / Film K2 (Double Feature) [DVD]",
          directors=("Jay Joiner", "Kay Keeper")),
        P("B0T6FLK001", "movie", "Film K (2002)", directors=("Kay Keeper",)),
        # AC11：B1–B5 与 M1–M4 的反例和正例
        P("B0T6ONB001", "movie", "Film B1 (2001)", directors=("Bri Builder",), release_date="August 8, 2003"),
        P("B0T6ONB002", "movie", "Film B1", directors=("Bri Builder",)),
        P("B0T6SPR001", "movie", "Sparse Show [VHS]"),
        P("B0T6ANC001", "movie", "Ancient Show [VHS]"),
        P("B0T6JAN001", "movie", "January Movie", release_date="January 1, 2001"),
        P("B0T6LST001", "movie", "Listed Movie",
          extra=(("detail_bullets", "Date First Available", "March 3, 2003", "product_release_date"),)),
        P("B0T6ERS001", "movie", "Early Review Saga (2004)", directors=("Quinn Quiet",),
          release_date="June 1, 2005"),
        P("B0T6CLS001", "movie", "Clash Century (2004)", directors=("Rita Retro",),
          release_date="June 6, 1998"),
        P("B0T6BOT001", "movie", "Bound Odyssey (2004)", directors=("Oscar Overseer",),
          release_date="June 6, 2004"),
        # AC12：多版本作品中最早评论早于全部 media_release 日期
        P("B0T6CRM001", "movie", "Cross Movie (2001)", directors=("Cam Crosser",),
          release_date="June 1, 2005"),
        P("B0T6CRM002", "movie", "Cross Movie (2001) [VHS]", directors=("Cam Crosser",),
          release_date="September 9, 2005"),
    ]


# ------------------------------------------------------------------ 评论（时间 + 正文；未列出的商品没有评论）
# “(YYYY)” 模式的年份在正文里；晚于评论年份的提及不计（AC06 的 (2003)）。
REVIEWS = {
    "B0T6A10001": [("1999-11-02", "Cinema at its finest."),
                   ("2000-03-01", "Still holds up.")],
    "B0T6A10002": [("2001-05-10", "This edition (2001) is a nice remaster; the planned reissue (2003) never happened."),
                    ("2001-08-01", "Love the (2001) release.")],
    "B0T6B10001": [("2005-02-01", "Great television film."), ("2005-03-01", "Worth watching.")],
    "B0T6TV0001": [("2005-10-06", "Nice season.")],
    "B0T6FLJ001": [("2001-03-02", "Funny one."), ("2001-05-10", "One of my favorites.")],
    "B0T6FLJ002": [("2000-12-01", "Classic material.")],
    "B0T6FLJ003": [("1999-01-01", "Two films, one tape.")],
    "B0T6AM1001": [("1999-04-04", "Nice transfer.")],
    "B0T6AM2001": [("1999-05-01", "this 1950 classic is superb"),
                    ("1999-06-15", "it was made in 1950 and still shines")],
    "B0T6RVT001": [("1999-05-01", "A true (1950) gem"),
                    ("1999-06-01", "this 1950 classic rocks"),
                    ("2000-07-07", "it was made in 1950")],
    "B0T6ONB001": [("2002-01-05", "Okay film."), ("2002-06-01", "Bought the DVD later.")],
    "B0T6SPR001": [("2001-01-01", "Only one review here.")],
    "B0T6ANC001": [("1970-01-01", "Saw this long ago.")],
    "B0T6ERS001": [("1999-12-01", "Wait, this is out already?"), ("2000-02-02", "Great action.")],
    "B0T6BOT001": [("2005-02-01", "Solid drama."), ("2005-03-15", "Recommended.")],
    "B0T6CRM001": [("2000-01-01", "Loved it."), ("2000-06-01", "Same here.")],
    "B0T6HNT001": [("2001-07-07", "Rare find.")],
    "B0T6OWN001": [("1988-08-08", "Old gem.")],
}

# ------------------------------------------------------------------ 任务 5 升级线索（写成 task5.t4_upgrade_hint 行）
# (asin, member_ordinal, tconst, mention_keys, link_decision_ids)
HINTS = [
    ("B0T6HNT001", 0, "tt0006011", ["B0T6HNT001:byline:1:1", "B0T6HNT001:byline:1:2"], [101, 102]),
    ("B0T6OWN001", 0, "tt0006001", ["B0T6OWN001:byline:1:1", "B0T6OWN001:byline:1:2"], [103, 104]),
    ("B0T6FLJ003", 1, "tt0006050", ["B0T6FLJ003:byline:1:1", "B0T6FLJ003:byline:1:2"], [105, 106]),
    ("B0T6MHA001", 0, "tt0006012", ["B0T6MHA001:byline:1:1", "B0T6MHA001:byline:1:2"], [107, 108]),
    ("B0T6MHA002", 0, "tt0006013", ["B0T6MHA002:byline:1:1", "B0T6MHA002:byline:1:2"], [109, 110]),
    ("B0T6IDX001", 0, "tt0006043", ["B0T6IDX001:byline:1:1", "B0T6IDX001:byline:1:2"], [111, 112]),
    ("B0T6LUK001", 0, "tt0006044", ["B0T6LUK001:byline:1:1", "B0T6LUK001:byline:1:2"], [113, 114]),
]

# 未在 REVIES 之列、也不在 NO_REVIEW 里的商品都有一条默认评论（时间与正文无年份，只用来让商品进清单；
# 任务 2 的清单产品来自 observed ASIN，包括只出现在隔离记录里的 ASIN）。
REVIEW_QUARANTINED = {"B0T6FLK001": [("2003-05-05", "Quarantined: never a valid review.")]}   # 作品 K 没有有效评论
NO_REVIEW = {"B0T6JAN001", "B0T6LST001", "B0T6CLS001"}          # 只有商品日期、没有评论的案例
DEFAULT_REVIEW = ("2004-04-04", "Plain review text.")


def review_plan() -> dict[str, list[tuple[str, str]]]:
    """每个商品的 (时间, 正文) 列表；B0T6FLK001 与 NO_REVIEW 商品只写隔离记录，不进有效评论。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for p in _products_asins():
        if p in quarantined_plan():
            continue
        out[p] = REVIEWS.get(p, []) if (p in REVIEWS or p in NO_REVIEW) else [DEFAULT_REVIEW]
    return out


def quarantined_plan() -> dict[str, list[tuple[str, str]]]:
    """只写隔离记录（score=6）的商品：商品能进清单（observed ASIN），但没有有效评论。"""
    out = dict(REVIEW_QUARANTINED)
    for asin in NO_REVIEW:
        out.setdefault(asin, [("2003-06-06", "Quarantined: not a valid review.")])
    return out


def _products_asins():
    return [p.asin for p in products()]
#   (asin, tconst)  → 把该商品的作品直接标为 imdb_linked（startYear 为 \N 的案例）；
#   (asin, asin)    → 把第二个商品并入第一个商品的作品（任务 4 无法自动产生的形态：作品内标题年份
#                     1985/1987、1990/1994 的分歧，以及 1999/2001 的多 tconst 线索所在的合并）。
FORCE_LINK = [("B0T6D10001", "tt0006010")]
FORCE_MERGE = [("B0T6FLD001", "B0T6FLD002"), ("B0T6FLH001", "B0T6FLH002"), ("B0T6MHA001", "B0T6MHA002")]
