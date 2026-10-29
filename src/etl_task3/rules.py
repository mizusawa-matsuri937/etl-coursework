"""分类规则（spec R08–R17）：抽取字段 → 类别、证据级别、命中规则与信号。纯函数，不访问数据库。

先由字段算出信号（每个信号指向一个具体字段），再按固定优先级匹配规则，首个命中即定。
命中规则用到的信号 used=True；其余信号照样保留（used=False），其中指向别的类别的就是冲突证据。
模式与阈值随 etl_task3.RULE_VERSION 记录，取值说明见 design“分类规则”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .extract import Field

CATEGORIES = ("movie", "non_movie", "collection", "tv_series", "concert_other", "pending")
BASIS_LEVELS = ("structured", "inferred", "missing")

VIDEO_ROOT = "Movies & TV"
FEATURE_MINUTES = (40, 240)
MIN_BOX_SET_DISCS = 2
# 首级不是视频类、又没有载体时，这些信号说明页面描述的仍是视频节目，判为冲突而不是非电影。
# t3r2 加入标题里的载体字样、Film/Movie 字样与标题模式：它们不作电影依据（R09），只阻止误判为非电影
VIDEO_HINTS = {"series_title", "tv_category", "concert_category", "film_category", "director", "actors",
               "title_carrier", "film_title", "concert_title", "collection_title"}

_CARRIER = re.compile(r"\b(DVD|Blu-ray|VHS|HD DVD|UMD|LaserDisc|Prime Video)\b", re.I)
_TV_SYSTEM = re.compile(r"\b(NTSC|PAL)\b")  # 电视制式只出现在视频商品的格式字段里
_NOT_VIDEO = re.compile(r"\bDVD[- ]Audio\b", re.I)
# 类别标签：概览 Genre 按逗号、斜杠切开的各项，加上面包屑第 2 级起的各级（小写精确比较）
_TV_TAGS = {"tv", "television", "tv series", "reality tv", "tv news programming"}
_CONCERT_TAGS = {"music videos & concerts", "music video & concerts", "exercise & fitness", "special interests",
                 "special interest", "sports", "performing arts", "instructional", "safety training dvds & videos"}
_FILM_TAGS = {"drama", "comedy", "action & adventure", "action", "adventure", "horror", "mystery & thrillers",
              "mystery & suspense", "thriller", "thrillers", "mystery", "suspense", "romance", "science fiction",
              "sci-fi", "science fiction & fantasy", "fantasy", "westerns", "western", "military & war", "war",
              "crime", "animation", "kids & family", "documentary", "musicals", "musical", "foreign films",
              "foreign", "independent", "classics", "feature"}
_SERIES_TITLE = re.compile(
    r"\b(Seasons?\s*\d|Seasons?\s+(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)\b"
    r"|(First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Final)\s+Seasons?"
    r"|Complete\s+(\w+\s+){0,3}(Series|Seasons?)|Episodes?\b|TV\s+Series|Mini-?Series)", re.I)
_CONCERT_TITLE = re.compile(
    r"\b(Live\s+(at|in|from)|in\s+Concert|Concert|Unplugged|Workout|Yoga|Pilates|Aerobics|Fitness"
    r"|Learn\s+to|How\s+to|Instructional|Lessons?)\b", re.I)
_LABEL_COLLECTION = re.compile(r"\(?\b(The\s+)?Criterion\s+Collection\b\)?", re.I)  # 发行品牌，不是合集
_COLLECTION_TITLE = re.compile(
    r"\b(Double\s+Feature|Triple\s+Feature|Trilogy|Quadrilogy|Anthology|\d+[- ](Movie|Film)s?"
    r"|(Movie|Film)\s+Collection|Collection|Box\s+Set|\d+[- ]Pack)\b|\s/\s", re.I)
# t3r2：只由载体与画面格式组成的括号组（如 “(Blu-ray 3D / Blu-ray / DVD)”）是版本说明，不是多个片名
_CARRIER_WORD = (r"(?:Blu-?ray(?:\s+3D)?|3D|4K|Ultra\s*HD|HD|DVD|VHS|Digital(?:\s+Copy)?|UltraViolet|"
                 r"Combo(?:\s+Pack)?|Widescreen|Full\s*Screen|NTSC|PAL|Region\s*\d)")
_CARRIER_GROUP = re.compile(rf"[(\[]\s*{_CARRIER_WORD}(?:\s*(?:/|\+|,|&|and)\s*{_CARRIER_WORD})*\s*[)\]]", re.I)
_TITLE_CARRIER = re.compile(r"\b(DVD|VHS|Blu-?ray)\b", re.I)
_FILM_TITLE = re.compile(r"\b(Film|Movie)s?\b", re.I)
# t3r2：占位署名不是作品级证据
_PLACEHOLDER_CREDIT = re.compile(r"^(artist not provided|various( artists)?|n/?a|not available|unknown|none"
                                 r"|not specified|-+)$", re.I)
_FILM_RATING = re.compile(r"^(G|PG|PG-13|R|NC-17|X)(\s|\(|$)")
_HOURS = re.compile(r"(\d+)\s*hours?", re.I)
_MINUTES = re.compile(r"(\d+)\s*min", re.I)


@dataclass(frozen=True)
class Signal:
    name: str
    supports: str | None        # 该信号指向的类别；载体等中性信号为 None
    section: str
    ordinal: int
    value_raw: str
    value_num: float | None = None


@dataclass(frozen=True)
class Evidence:
    signal: Signal
    used: bool


@dataclass(frozen=True)
class Result:
    category: str
    basis_level: str
    rule_id: str
    pending_reason: str | None
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    basis_level: str
    pending_reason: str | None = None


RULES = {r.rule_id: r for r in (
    Rule("NM1", "non_movie", "structured"),
    Rule("PX1", "pending", "missing", "conflicting_evidence"),
    Rule("PX2", "pending", "missing", "no_video_evidence"),
    Rule("TV1", "tv_series", "structured"),
    Rule("TV2", "tv_series", "inferred"),
    Rule("CO1", "concert_other", "structured"),
    Rule("CO2", "concert_other", "inferred"),
    Rule("CL2", "collection", "inferred"),
    Rule("MV1", "movie", "structured"),
    Rule("MV2", "movie", "inferred"),
    Rule("PD1", "pending", "missing", "insufficient_evidence"),
)}


def runtime_minutes(text: str) -> int | None:
    h, m = _HOURS.search(text), _MINUTES.search(text)
    if not h and not m:
        return None
    return (int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0)


def _tags(f: Field) -> list[str]:
    if f.field_key == "genre":
        return [t.strip() for t in re.split(r"[,/]", f.value_raw) if t.strip()]
    if f.field_key == "breadcrumb" and f.ordinal >= 2:
        return [f.value_raw]
    return []


def signals(fields: Iterable[Field]) -> list[Signal]:
    out: list[Signal] = []

    def add(name: str, supports: str | None, f: Field, num: float | None = None) -> None:
        out.append(Signal(name, supports, f.section, f.ordinal, f.value_raw, num))

    for f in fields:
        v = f.value_raw
        if f.field_key == "breadcrumb" and f.ordinal == 1:
            add("breadcrumb_root", None if v == VIDEO_ROOT else "non_movie", f)
        carrier = _CARRIER.search(v) and not _NOT_VIDEO.search(v)
        if (f.field_key == "media_format" and (carrier or _TV_SYSTEM.search(v))) \
                or (f.field_key == "breadcrumb" and f.ordinal >= 2 and carrier):
            add("video_carrier", None, f)
        for tag in _tags(f):
            low = tag.lower()
            if low in _TV_TAGS:
                add("tv_category", "tv_series", f)
            elif low in _CONCERT_TAGS or low.startswith("exercise_"):
                add("concert_category", "concert_other", f)
            elif low in _FILM_TAGS:
                add("film_category", "movie", f)
        if f.field_key == "title":
            plain = _CARRIER_GROUP.sub(" ", _LABEL_COLLECTION.sub(" ", v))
            if _TITLE_CARRIER.search(v):
                add("title_carrier", None, f)
            if _FILM_TITLE.search(plain):
                add("film_title", None, f)
            if _SERIES_TITLE.search(plain):
                add("series_title", "tv_series", f)
            if _CONCERT_TITLE.search(plain):
                add("concert_title", "concert_other", f)
            if _COLLECTION_TITLE.search(plain):
                add("collection_title", "collection", f)
        if f.field_key == "media_format" and re.search(r"\bBox set\b", v, re.I):
            add("box_set", "collection", f)
        if f.field_key == "number_of_discs" and v.strip().isdigit() and int(v) >= MIN_BOX_SET_DISCS:
            add("multi_disc", "collection", f, int(v))
        if f.field_key in ("director", "actors") and v:
            if _PLACEHOLDER_CREDIT.match(v.strip()):
                add("placeholder_credit", None, f)
            else:
                add(f.field_key, "movie", f)
        if f.field_key == "runtime":
            minutes = runtime_minutes(v)
            if minutes is not None:
                lo, hi = FEATURE_MINUTES
                add("feature_runtime" if lo <= minutes <= hi else "other_runtime",
                    "movie" if lo <= minutes <= hi else None, f, minutes)
        if f.field_key == "mpaa_rating" and _FILM_RATING.match(v):
            add("film_rating", "movie", f)
    return out


def _decide(names: set[str], root_other: bool) -> tuple[str, set[str]]:
    """按优先级返回 (rule_id, 用到的信号名)。root_other：面包屑首级存在且不是 Movies & TV。"""
    has = names.__contains__
    work = {n for n in ("director", "actors", "film_rating", "film_category") if has(n)}
    if root_other and not has("video_carrier"):
        hints = VIDEO_HINTS & names
        return ("PX1", {"breadcrumb_root"} | hints) if hints else ("NM1", {"breadcrumb_root"})
    if not has("breadcrumb_root") and not has("video_carrier"):
        return "PX2", set()
    if has("tv_category"):
        return "TV1", {"tv_category"}
    if has("series_title"):
        return "TV2", {"series_title"}
    if has("concert_category"):
        return "CO1", {"concert_category"}
    if has("concert_title"):
        return "CO2", {"concert_title"}
    if has("collection_title"):
        return "CL2", {"collection_title"}
    if has("director") and has("feature_runtime") and work - {"director"}:
        return "MV1", {"director", "feature_runtime"} | (work - {"director"})
    if has("feature_runtime") and work:
        return "MV2", {"feature_runtime"} | work
    if not has("feature_runtime") and not has("other_runtime") and len(work) >= 2:
        return "MV2", work
    return "PD1", set()


def classify(fields: Iterable[Field]) -> Result:
    sigs = signals(fields)
    root_other = any(s.name == "breadcrumb_root" and s.supports == "non_movie" for s in sigs)
    rule_id, used = _decide({s.name for s in sigs}, root_other)
    rule = RULES[rule_id]
    evidence = tuple(Evidence(s, s.name in used) for s in sigs)
    return Result(rule.category, rule.basis_level, rule_id, rule.pending_reason, evidence)
