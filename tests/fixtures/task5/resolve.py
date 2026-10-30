"""任务 5 归并夹具（AC05、AC06、AC07）：手写的 IMDb 作品与商品，预期值见 README.md。"""

from __future__ import annotations

N = None


def _names(rows):
    return [[nc, name, str(b) if b else N, N, N, N] for nc, name, b in rows]


def _work(tt, title, year, director_nc, principals):
    return ([tt, "movie", title, title, "0", str(year), N, "100", "Drama"], [tt, director_nc, N],
            [[tt, str(n), nc, cat, N, N] for n, (nc, cat) in enumerate(principals, start=1)])


def _imdb(works, names):
    return dict(basics=[w[0] for w in works], akas=[], crew=[w[1] for w in works],
                principals=[r for w in works for r in w[2]], names=_names(names))


# ------------------------------------------------------------------ AC05
CASES_IMDB = _imdb(
    [_work("tt0003001", "Link One", 1971, "nm0003001", [("nm0003002", "actor"), ("nm0003003", "actress")]),
     _work("tt0003002", "Link Two", 1972, "nm0003001", [("nm0003004", "actor"), ("nm0003005", "actress")]),
     _work("tt0003003", "Era Base", 1999, "nm0003001", [("nm0003006", "actor"), ("nm0003007", "actress")]),
     _work("tt0003004", "Chain Why", 1980, "nm0003010", [("nm0003011", "actor"), ("nm0003012", "actress")]),
     _work("tt0003005", "Chain Zee", 1985, "nm0003013", [("nm0003014", "actor"), ("nm0003015", "actress")])],
    [("nm0003001", "Dir Link", 1940), ("nm0003002", "Robert Young", 1907), ("nm0003003", "Lia One", 1950),
     ("nm0003004", "Robert Young", 1958), ("nm0003005", "Lia Two", 1950), ("nm0003006", "Late Born", 1980),
     ("nm0003007", "Era Mate", 1960), ("nm0003010", "Dir Why", 1940), ("nm0003011", "Chain Name", 1950),
     ("nm0003012", "Anchor Actor", 1950), ("nm0003013", "Dir Zee", 1940), ("nm0003014", "Chain Name", 1955),
     ("nm0003015", "Anchor Actor", 1955)])


def case_products():
    from _task4_helpers import P

    trio = ("Chain Name", "Anchor Actor", "Cho One", "Cho Two")
    return [
        P("B0T5R00A01", "movie", "Same Work (1960)", directors=("Dana Same",)),                       # 1
        P("B0T5R00A02", "movie", "Same Work (1960 VHS)", directors=("Dana Same",)),
        P("B0T5R00B01", "movie", "Dual Role (1970)", directors=("Orson Dual",), actors=("Orson Dual", "Pat Other")),  # 2
        P("B0T5R00C01", "movie", "Alpha Tale (1961)", directors=("Dir One",), actors=("Sam Solo",)),  # 3
        P("B0T5R00C02", "movie", "Beta Tale (1962)", directors=("Dir Two",), actors=("Sam Solo",)),
        P("B0T5R00D01", "movie", "Gamma Tale (1963)", directors=("Dir Three",),                       # 4
          actors=("Cy Match", "Co Star", "Co Two")),
        P("B0T5R00D02", "movie", "Delta Tale (1964)", directors=("Dir Four",), actors=("Cy Match", "Co Star", "Co Two")),
        P("B0T5R00E01", "movie", "Link One (1971)", directors=("Dir Link",), actors=("Robert Young", "Lia One")),   # 5
        P("B0T5R00E02", "movie", "Link Two (1972)", directors=("Dir Link",), actors=("Robert Young", "Lia Two")),
        P("B0T5R00F01", "movie", "Era Base (1999)", directors=("Dir Link",), actors=("Late Born", "Era Mate")),     # 6
        P("B0T5R00F02", "movie", "Early Page (1950)", directors=("Dir Early",), actors=("Late Born",)),
        P("B0T5R00G01", "movie", "Chain Why (1980)", directors=("Dir Why",), actors=trio),                         # 7
        P("B0T5R00G02", "movie", "Chain Zee (1985)", directors=("Dir Zee",), actors=trio),
        P("B0T5R00G03", "movie", "Chain Ex (1983)", directors=("Dir Ex",), actors=("Chain Name", "Cho One", "Cho Two")),
        P("B0T5R00H01", "movie", "Smith One (1991)", directors=("Dir Eight",), actors=("J. Smith",)),              # 8
        P("B0T5R00H02", "movie", "Smith Two (1992)", directors=("Dir Nine",), actors=("John Smith",)),
    ]


# ------------------------------------------------------------------ AC06 迭代
ITER_IMDB = _imdb(
    [_work("tt0004001", "Peak P", 1990, "nm0004001", [("nm0004011", "actor"), ("nm0004012", "actor"),
                                                      ("nm0004013", "actor"), ("nm0004014", "actor")]),
     _work("tt0004002", "Peak Q", 1991, "nm0004002", [("nm0004011", "actor"), ("nm0004012", "actor")]),
     _work("tt0004003", "Peak R", 1992, "nm0004003", [("nm0004013", "actor"), ("nm0004014", "actor")])],
    [("nm0004001", "Dir P", 1940), ("nm0004002", "Dir Q", 1940), ("nm0004003", "Dir R", 1940),
     ("nm0004011", "Vee One", 1950), ("nm0004012", "Vee Two", 1950), ("nm0004013", "Vee Three", 1950),
     ("nm0004014", "Vee Four", 1950)])


def iter_products():
    from _task4_helpers import P

    return [
        P("B0T5I00P01", "movie", "Peak P (1990)", directors=("Dir P",),
          actors=("Ann Alpha", "Vee One", "Vee Two", "Vee Three", "Vee Four")),
        P("B0T5I00Q01", "movie", "Peak Q (1991)", directors=("Dir Q",), actors=("Ann Alpha", "Vee One", "Vee Two", "Bea Bee")),
        P("B0T5I00R01", "movie", "Peak R (1992)", directors=("Dir R",), actors=("Ann Alpha", "Vee Three", "Vee Four", "Bea Bee")),
    ]


# ------------------------------------------------------------------ AC06 特征开关
def switch_products():
    from _task4_helpers import P

    pub = (("detail_bullets", "Studio", "Pub House", "studio"),)
    return [
        P("B0T5S00A01", "movie", "Rare One (1950)", directors=("Dir Ra",), actors=("Rora Rare", "Shared Mate")),
        P("B0T5S00A02", "movie", "Rare Two (1951)", directors=("Dir Rb",), actors=("Rora Rare", "Shared Mate")),
        P("B0T5S00B01", "movie", "Pub One (1953)", directors=("Dir Pa",), actors=("Pam Pub",), extra=pub),
        P("B0T5S00B02", "movie", "Pub Two (1954)", directors=("Dir Pb",), actors=("Pam Pub",), extra=pub),
    ]
