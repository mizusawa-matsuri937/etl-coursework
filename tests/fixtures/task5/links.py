"""任务 5 外部 ID 夹具（AC03、AC04）：手写的 IMDb 作品、演职员、人物与商品，预期值见 README.md。

CREDITS 世界：同作品演职员里的姓名兼容等级；FILM 世界：已核实人物的作品年表反查。
FILM 世界里没有 IMDb 关联的商品都写了一位与 IMDb 导演不同的导演（Wrong Person），让任务 4 因 director_disjoint 否决关联，保持 page_only。
"""

from __future__ import annotations

N = None
BAD = "�"


def _names(rows):
    return [[nc, name, str(b) if b else N, N, N, N] for nc, name, b in rows]


# ------------------------------------------------------------------ AC03
CREDITS_IMDB = dict(
    basics=[["tt0001001", "movie", "Calm Harbor", "Calm Harbor", "0", "1990", N, "100", "Drama"],
            ["tt0001002", "movie", "Gray Lantern", "Gray Lantern", "0", "1995", N, "100", "Drama"],
            ["tt0001003", "movie", "Silver Mirror", "Silver Mirror", "0", "2000", N, "100", "Drama"],
            ["tt0001004", "movie", "Other Place", "Other Place", "0", "1999", N, "100", "Drama"]],
    akas=[],
    crew=[["tt0001001", "nm0001001", N], ["tt0001002", "nm0001006,nm0001017", N], ["tt0001003", "nm0001010,nm0001011", N],
          ["tt0001004", "nm0001015", N]],
    principals=[
        ["tt0001001", "1", "nm0001002", "actor", N, N], ["tt0001001", "2", "nm0001003", "actor", N, N],
        ["tt0001001", "3", "nm0001004", "actor", N, N], ["tt0001001", "4", "nm0001005", "actress", N, N],
        ["tt0001002", "1", "nm0001007", "actor", N, N], ["tt0001002", "2", "nm0001008", "actor", N, N],
        ["tt0001002", "3", "nm0001009", "actress", N, N],
        ["tt0001003", "1", "nm0001012", "actress", N, N], ["tt0001003", "2", "nm0001013", "actress", N, N],
        ["tt0001003", "3", "nm0001014", "actress", N, N],
        ["tt0001004", "1", "nm0001016", "actor", N, N]],
    names=_names([
        ("nm0001001", "Alan J. Pakula", 1928), ("nm0001002", "Humphrey Bogart", 1899),
        ("nm0001003", "Tommy Lee Jones", 1946), ("nm0001004", "Billy Bob Thornton", 1955),
        ("nm0001005", "Ann Strict", 1960), ("nm0001006", "François Girard", 1963),
        ("nm0001007", "Robert Young", 1907), ("nm0001008", "Robert Young", 1958),
        ("nm0001009", "Cyd Charisse", 1922), ("nm0001010", "Kay Real", 1950), ("nm0001011", "Dee Cross", 1951),
        ("nm0001012", "Ann Actor", 1970), ("nm0001013", "Lana Wachowski", 1965), ("nm0001014", "Lilly Wachowski", 1967),
        ("nm0001015", "Oth Dir", 1940), ("nm0001016", "Zed Outside", 1945), ("nm0001017", "Co Director", 1964)]),
)


def credits_products():
    from _task4_helpers import P

    return [
        P("B0T5L00A01", "movie", "Calm Harbor (1990)", directors=("Alan Pakula",),
          actors=("Bogart, Humphrey", "Tommy Jones", "Billy Bob Thorton", "Ann Strict")),
        P("B0T5L00B01", "movie", "Gray Lantern (1995)", directors=(f"Fran{BAD}ois Girard", "Co Director"),
          actors=("Robert Young", "Cyd Charisse")),
        P("B0T5L00C01", "movie", "Silver Mirror (2000)", directors=("Kay Real",),
          actors=("Dee Cross", "Andy Wachowski", "Ann Actor", "Zed Outside")),
        P("B0T5L00D01", "movie", "Nowhere Film", directors=("Kay Real",), actors=("Ann Strict",)),
    ]


# ------------------------------------------------------------------ AC04
FILM_IMDB = dict(
    basics=[
        ["tt0002001", "movie", "Verified Origin", "Verified Origin", "0", "1980", N, "100", "Drama"],
        ["tt0002002", "movie", "Second Origin", "Second Origin", "0", "1985", N, "100", "Drama"],
        ["tt0002011", "movie", "Lost Voyage", "Lost Voyage", "0", "1985", N, "100", "Drama"],
        ["tt0002012", "movie", "Hidden Stars", "Hidden Stars", "0", "1986", N, "100", "Drama"],
        ["tt0002013", "movie", "Twin Title", "Twin Title", "0", "1970", N, "100", "Drama"],
        ["tt0002014", "movie", "Twin Title", "Twin Title", "0", "1990", N, "100", "Drama"],
        ["tt0002015", "movie", "Shared Title", "Shared Title", "0", "1987", N, "100", "Drama"],
        ["tt0002016", "movie", "Shared Title", "Shared Title", "0", "1988", N, "100", "Drama"],
        ["tt0002017", "movie", "Quiet Echo", "Quiet Echo", "0", "1990", N, "100", "Drama"],
        ["tt0002018", "movie", "Roleswap", "Roleswap", "0", "1992", N, "100", "Drama"],
        ["tt0002019", "movie", "Late Bloom", "Late Bloom", "0", "1990", N, "100", "Drama"],
        ["tt0002020", "movie", "Double Cast", "Double Cast", "0", "1995", N, "100", "Drama"],
        ["tt0002021", "movie", "Quiet Hall", "Quiet Hall", "0", "1991", N, "100", "Drama"],
    ],
    akas=[["tt0002012", "1", "Etoiles Cachees", "FR", "fr", N, N, "0"]],
    crew=[["tt0002001", "nm0002010", N], ["tt0002002", "nm0002010", N]]
         + [[f"tt00020{n}", "nm0002011", N] for n in range(11, 22)],
    principals=[
        ["tt0002001", "1", "nm0002001", "actor", N, N], ["tt0002001", "2", "nm0002002", "actress", N, N],
        ["tt0002002", "1", "nm0002003", "actor", N, N],
        ["tt0002011", "1", "nm0002001", "actor", N, N], ["tt0002012", "1", "nm0002001", "actor", N, N],
        ["tt0002013", "1", "nm0002001", "actor", N, N], ["tt0002014", "1", "nm0002001", "actor", N, N],
        ["tt0002015", "1", "nm0002001", "actor", N, N], ["tt0002016", "1", "nm0002003", "actor", N, N],
        ["tt0002017", "1", "nm0002020", "actress", N, N], ["tt0002018", "1", "nm0002001", "actor", N, N],
        ["tt0002019", "1", "nm0002001", "actor", N, N],
        ["tt0002020", "1", "nm0002001", "actor", N, N], ["tt0002020", "2", "nm0002002", "actress", N, N],
        ["tt0002021", "1", "nm0002001", "actor", N, N]],
    names=_names([
        ("nm0002001", "Ava Verified", 1950), ("nm0002002", "Bea Verified", 1955), ("nm0002003", "Ava Verified", 1960),
        ("nm0002010", "Orin Director", 1940), ("nm0002011", "Real Dir", 1941), ("nm0002020", "Nora Neverseen", 1962)]),
)


def film_products():
    from _task4_helpers import P

    wrong = dict(directors=("Wrong Person",))
    return [
        P("B0T5F00V01", "movie", "Verified Origin (1980)", directors=("Orin Director",),
          actors=("Ava Verified", "Bea Verified")),
        P("B0T5F00V02", "movie", "Second Origin (1985)", directors=("Orin Director",), actors=("Ava Verified",)),
        P("B0T5F00U01", "movie", "Lost Voyage (1985)", actors=("Ava Verified",), **wrong),
        P("B0T5F00U02", "movie", "Etoiles Cachees (1986)", actors=("Ava Verified",), **wrong),
        P("B0T5F00U03", "movie", "Twin Title", actors=("Ava Verified",), **wrong),
        P("B0T5F00U04", "movie", "Shared Title (1987)", actors=("Ava Verified",), **wrong),
        P("B0T5F00U05", "movie", "Quiet Echo (1990)", actors=("Nora Neverseen",), **wrong),
        P("B0T5F00U06", "movie", "Roleswap (1992)", directors=("Ava Verified",), actors=("Zed Extra",)),
        P("B0T5F00U07", "movie", "Late Bloom (1993)", actors=("Ava Verified",), **wrong),
        P("B0T5F00U08", "movie", "Double Cast (1995)", actors=("Ava Verified", "Bea Verified"), **wrong),
        P("B0T5F00K01", "collection", "Mirror Lake / Quiet Hall (Double Feature) [DVD]", actors=("Ava Verified",),
          **wrong),
    ]
