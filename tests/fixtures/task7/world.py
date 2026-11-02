"""任务 7 合成夹具的规格类（全部为手写的合成数据，非真实商品与作品）。

测试用 `_task7_helpers.seed` 把一个 `World` 直接写成任务 3–6 与 IMDb 的上游表行（不运行上游流水线），所以每个案例
只含自己需要的作品与商品，案例之间互不影响。预期值按 spec 的 AC 表手算，写在各测试里，不由实现生成。
README.md 列出各案例对应的 AC 场景。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# 三类 IMDb 关联（任务 6 候选来源）与它们对应的任务 6 选择状态
IMDB_SOURCES = ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur")
STATUS_OF_SOURCE = {"imdb_t4_link": "verified", "imdb_t5_hint": "hint_matched", "imdb_t6_cooccur": "cooccur_matched",
                    "page_reported": "page_reported"}


def movie_id(name: str) -> str:
    """作品名 → 稳定的 movie_id（符合 task4.movie_identity 的格式 mv_ + 16 位十六进制）。"""
    return "mv_" + hashlib.md5(name.encode()).hexdigest()[:16]


def person_id(name: str, nconst: str | None = None, anchor: str | None = None) -> str:
    """页面人物的 person_id，与任务 5 同一公式 `ps_ + sha256(锚点)[:16]`：已关联 IMDb 的以 `imdb:<nconst>` 为锚点，
    其余以固定的提及锚点 `mention:fix:<姓名>` 为锚点（夹具里同名页面人物视为同一人）。"""
    anchor = anchor or (f"imdb:{nconst}" if nconst else f"mention:fix:{name}")
    return "ps_" + hashlib.sha256(anchor.encode()).hexdigest()[:16]


@dataclass
class Imdb:
    """IMDb 快照里的一部作品：标题、别名、导演与主要演员。"""
    tconst: str
    primary: str
    original: str | None = None
    year: int | None = None
    runtime: int | None = None
    genres: tuple[str, ...] = ()
    akas: tuple[tuple[str, str | None, str | None], ...] = ()                  # (标题, 区域, 语言)
    directors: tuple[tuple[str, str], ...] = ()                                # (nconst, 姓名)
    principals: tuple[tuple[str, str, str], ...] = ()                          # (nconst, 姓名, category)


@dataclass
class Release:
    """任务 6 对一部作品的上映信息结果。source 为 None 表示 missing。"""
    source: str | None = None             # imdb_t4_link | imdb_t5_hint | imdb_t6_cooccur | page_reported
    tconst: str | None = None             # IMDb 来源的 tconst
    imdb_year: int | None = None          # 该 tconst 的 IMDb 年份（自动取 Imdb.year）
    title_years: tuple[int, ...] = ()     # 页面标题年份候选（amazon_title_year）
    missing_reason: str | None = None
    bounds: tuple[tuple[str, str], ...] = ()          # (证据 review|media_release, ISO 日期)：bound_evidence 行
    inferred: tuple[str, str, str] | None = None      # 推断上界 (ISO 日期, 精度, 证据)


@dataclass
class Credit:
    """任务 5 人物映射里一部作品的一条页面署名（`movie_person` 与它的来源提及 `movie_person_source`）。
    name 为页面写法（可含多人串）；nconst 为任务 5 已关联的 IMDb 人物；ranks 为各来源商品的署名行序号
    (ASIN, 序号)，序号为 None 表示详情要点来源（没有署名顺序）；aliases 为同一人物的其他页面写法。"""
    name: str
    role: str                                       # director | starring | cast
    nconst: str | None = None
    ranks: tuple[tuple[str, int | None], ...] = ()
    aliases: tuple[str, ...] = ()
    anchor: str | None = None                       # 人物锚点；默认已关联用 imdb:<nconst>，否则 mention:fix:<姓名>

    @property
    def pid(self) -> str:
        return person_id(self.name, self.nconst, self.anchor)


@dataclass
class Movie:
    """任务 4 映射版本里的一部作品。directors：任务 5 页面导演 (姓名, nconst 或 None)；credits：其余页面署名。"""
    name: str
    release: Release = field(default_factory=Release)
    directors: tuple[tuple[str, str | None], ...] = ()
    credits: tuple[Credit, ...] = ()


@dataclass
class Ed:
    """一个商品（ASIN）。movie 为 edition 关系的作品名；members 为合集的 contains 成员 (序号, 作品名, 成员标题)。
    各属性断言的取值为 (原值, 区域)；分级用任务 3 页面字段 (原值, 区域)。"""
    asin: str
    title: str | None = None
    kind: str = "movie"                   # movie | collection | tv_series
    movie: str | None = None
    members: tuple[tuple[int, str, str], ...] = ()
    basis: str = "singleton"
    runtime: tuple[tuple[int, str], ...] = ()
    publisher: tuple[tuple[str, str], ...] = ()
    discs: tuple[tuple[int, str], ...] = ()
    packaging: tuple[str, ...] = ()
    rating: tuple[tuple[str, str], ...] = ()
    dates: tuple[tuple[str, str, tuple[str, ...]], ...] = ()     # (kind, ISO 日期, 标记)
    media_format: tuple[tuple[str, str], ...] = ()
    language: tuple[tuple[str, str], ...] = ()
    subtitles: tuple[tuple[str, str], ...] = ()
    aspect_ratio: tuple[tuple[str, str], ...] = ()
    region: tuple[tuple[str, str], ...] = ()
    cut_type: tuple[tuple[str, str], ...] = ()
    video_system: tuple[tuple[str, str], ...] = ()
    swatch: tuple[str, ...] = ()          # 任务 3 format_swatch 字段值
    genre: tuple[str, ...] = ()           # 任务 3 概览区 Genre 字段的整串（逗号、斜线分隔），由任务 4 的 genre_tags 拆成断言
    breadcrumb: tuple[str, ...] = ()      # 任务 3 面包屑各级（field_key = breadcrumb）


@dataclass
class World:
    imdb: list[Imdb] = field(default_factory=list)
    movies: list[Movie] = field(default_factory=list)
    editions: list[Ed] = field(default_factory=list)
    shared: list[tuple[str, str, str, str]] = field(default_factory=list)   # (作品名, tconst, 占有作品名, 来源 candidate_shared|cooccur)
    decoys: bool = False                  # 加入其他映射版本、上映版本、规范化版本与 IMDb 快照的干扰行（AC01）
