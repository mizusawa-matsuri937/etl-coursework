"""任务 5 MoviePerson 夹具（AC08）：手写的 IMDb 作品与商品，预期值见 README.md。"""

from __future__ import annotations

from task5.resolve import _imdb, _work

# Harbor Lights（tt0005001）有两个单品、也是合集的第 1 个成员；Harbor Nights（tt0005002）只在合集里。
MP_IMDB = _imdb(
    [_work("tt0005001", "Harbor Lights", 1990, "nm0005001", [("nm0005002", "actor"), ("nm0005003", "actress"),
                                                              ("nm0005004", "actor")]),
     _work("tt0005002", "Harbor Nights", 1992, "nm0005005", [("nm0005006", "actor")])],
    [("nm0005001", "Dir One", 1940), ("nm0005002", "Ann Star", 1950), ("nm0005003", "Bea Lead", 1955),
     ("nm0005004", "Cy Support", 1948), ("nm0005005", "Dir Two", 1941), ("nm0005006", "Dan Other", 1952)])


def mp_products():
    from _task4_helpers import P

    return [
        # 署名行 Ann Star 第 1、Bea Lead 第 2；详情要点按字母排序并多出 Cy Support 与导演兼演员的 Dir One
        P("B0T5P00W01", "movie", "Harbor Lights (1990)", directors=("Dir One",), actors=("Ann Star", "Bea Lead"),
          actors_detail="Ann Star, Bea Lead, Cy Support, Dir One"),
        # 另一个版本：署名行顺序相反，所以 Ann Star、Bea Lead 的署名顺序在两个版本里不一致
        P("B0T5P00W02", "movie", "Harbor Lights (1990 VHS)", directors=("Dir One",), actors=("Bea Lead", "Ann Star")),
        # 合集：Ann Star 只在第 1 个成员的演职员里、Dan Other 只在第 2 个（都关联成功）；Zed Nobody 哪个成员里都没有（unattributed）
        P("B0T5P00K01", "collection", "Harbor Lights / Harbor Nights (Double Feature) [DVD]",
          directors=("Dir One", "Dir Two"), actors=("Ann Star", "Dan Other", "Zed Nobody"), reviews=4),
    ]
