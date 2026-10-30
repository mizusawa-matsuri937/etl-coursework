"""任务 5 合成夹具：人物提及用的商品与 IMDb 一侧的作品、演职员、人物（全部手写，非真实数据，预期值见 README.md）。

商品用 tests/integration/_task4_helpers.P 描述（署名行、详情要点分开写），IMDb 数据交给 task4 的 imdb_files.build_snapshot 现场写成
.tsv.gz 并装载；任务 4 映射版本由测试里真正运行 etl_task4.runner.execute_match 得到，所以任务 5 读到的都是真实的任务 4 结构。
"""

from __future__ import annotations

N = None

# ------------------------------------------------------------------ IMDb 一侧
IMDB = dict(
    basics=[
        ["tt0000301", "movie", "Casablanca", "Casablanca", "0", "1942", N, "102", "Drama,Romance"],
        ["tt0000302", "movie", "Men in Black", "Men in Black", "0", "1997", N, "98", "Action,Comedy"],
        ["tt0000303", "movie", "Alien", "Alien", "0", "1979", N, "117", "Horror,Sci-Fi"],
        ["tt0000304", "movie", "Aliens", "Aliens", "0", "1986", N, "137", "Action,Sci-Fi"],
    ],
    akas=[],
    crew=[["tt0000301", "nm0000301", N], ["tt0000302", "nm0000305", N], ["tt0000303", "nm0000308", N],
          ["tt0000304", "nm0000310", N]],
    principals=[
        ["tt0000301", "1", "nm0000302", "actor", N, N], ["tt0000301", "2", "nm0000303", "actress", N, N],
        ["tt0000301", "3", "nm0000304", "actor", N, N],
        ["tt0000302", "1", "nm0000306", "actor", N, N], ["tt0000302", "2", "nm0000307", "actor", N, N],
        ["tt0000303", "1", "nm0000309", "actress", N, N], ["tt0000304", "1", "nm0000309", "actress", N, N],
    ],
    names=[
        ["nm0000301", "Michael Curtiz", "1886", "1962", "director", N],
        ["nm0000302", "Humphrey Bogart", "1899", "1957", "actor", N],
        ["nm0000303", "Ingrid Bergman", "1915", "1982", "actress", N],
        ["nm0000304", "Claude Rains", "1889", "1967", "actor", N],
        ["nm0000305", "Barry Sonnenfeld", "1953", N, "director", N],
        ["nm0000306", "Tommy Lee Jones", "1946", N, "actor", N],
        ["nm0000307", "Will Smith", "1968", N, "actor", N],
        ["nm0000308", "Ridley Scott", "1937", N, "director", N],
        ["nm0000309", "Sigourney Weaver", "1949", N, "actress", N],
        ["nm0000310", "James Cameron", "1954", N, "director", N],
    ],
)

# ------------------------------------------------------------------ 商品（P 的参数见 _task4_helpers.P）
# 返回函数而不是模块级对象，避免导入时依赖测试辅助模块。


def products():
    from _task4_helpers import P

    return [
        # 关联 IMDb 的两个版本：署名行倒序写法、详情要点重复署名行并多出一位、编剧与制片署名
        P("B0T5M00A01", "movie", "Casablanca [DVD]", directors=("Curtiz, Michael",),
          directors_detail="Michael Curtiz", actors=("Bogart, Humphrey", "Ingrid Bergman"),
          actors_detail="Claude Rains, Humphrey Bogart, Ingrid Bergman", runtime="1 hour and 42 minutes", reviews=3,
          extra=(("detail_bullets", "Writers", "Julius J. Epstein", "writers"),
                 ("detail_bullets", "Producers", "Hal B. Wallis", "producers"),
                 ("byline", "Writer", "Howard Koch", "writers"))),
        P("B0T5M00A02", "movie", "Casablanca [Blu-ray]", directors=("Michael Curtiz",),
          actors=("Humphrey Bogart", "Ingrid Bergman"), runtime="1 hour and 42 minutes", reviews=2),
        # 关联 IMDb；署名行 “Tommy Jones” 对 IMDb 的 “Tommy Lee Jones”；详情要点有 “Robert Downey, Jr.”
        P("B0T5M00B01", "movie", "Men in Black [VHS]", directors=("Barry Sonnenfeld",),
          actors=("Tommy Jones", "Will Smith"),
          actors_detail="Linda Fiorentino, Robert Downey, Jr., Tommy Lee Jones, Will Smith",
          runtime="1 hour and 38 minutes"),
        # 没有 IMDb 关联的作品
        P("B0T5M00C01", "movie", "Unlisted Film", directors=("Pat Poe",), actors=("Ann Lee",)),
        # 完全没有署名行：导演与演员都来自详情要点
        P("B0T5M00D01", "movie", "Rocky II [VHS]", directors_detail="Sylvester Stallone",
          actors_detail="Sylvester Stallone, Talia Shire"),
        # 占位署名，以及无法确定边界的详情要点
        P("B0T5M00E01", "movie", "Mixed Credits", directors=("Various",), actors=("Various",),
          actors_detail="Arlt, Lewis, Brown, Blair"),
        P("B0T5M00F01", "movie", "Wilson Story", actors=("Robert Wilson (III)",)),
        # 合集：成员 Alien、Aliens 都在 IMDb 里；Lance Henriksen 不在任何成员的演职员里
        P("B0T5M00K01", "collection", "Alien / Aliens (Double Feature) [DVD]",
          directors=("Ridley Scott", "James Cameron"), actors=("Sigourney Weaver", "Lance Henriksen"), reviews=4),
        # 剧集：不参与匹配，没有提及
        P("B0T5M00T01", "tv_series", "Some Series Season 1", directors=("Series Director",),
          actors=("Series Actor",)),
    ]
