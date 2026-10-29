"""标准化（spec R07–R11、R17，NORM_VERSION = t4n1）：标题键、说明分类与标题年份、姓名键、片长、合集成员拆分。纯函数，不访问数据库。

词表与正则集中在本文件；改动词表或任何一步的行为必须升 etl_task4.NORM_VERSION。
原始标题永远不改：这里只产出“标题键 + 被去掉的说明（含原文与分类）”，调用方把两者都写成字段断言。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from etl_task3.rules import runtime_minutes  # noqa: F401  片长解析复用任务 3，re-export 供 editions 使用

# ---------------------------------------------------------------- 通用折叠

_MARKS = re.compile("[™®©\u200e\u200f\u202a-\u202e]")
_APOSTROPHES = re.compile(r"['’‘`´ʼ]")
_NONWORD = re.compile(r"[\W_]+")


def fold(text: str) -> str:
    """NFKC → 去掉拉丁字母上的重音 → 大小写折叠。非拉丁文字（日文假名的浊点等）的组合符号保留。"""
    nfkd = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", _MARKS.sub("", text)))
    out: list[str] = []
    for ch in nfkd:
        if unicodedata.category(ch) == "Mn" and out and ord(out[-1]) < 0x250:
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out)).casefold()


# ---------------------------------------------------------------- 标题键（R07）

ARTICLES = frozenset({"the", "a", "an", "le", "la", "les", "el", "los", "las", "il", "der", "die", "das"})
_ROMAN = {r: i for i, r in enumerate(
    ["II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII",
     "XVIII", "XIX", "XX"], start=2)}
# 只转独立的词，且位于末尾，或后面紧跟冒号、连字符（单独的 “I” 不转，以免把代词改掉）
_ROMAN_WORD = re.compile(r"(?<![\w'’])([IVX]{1,5})(?![\w'’])(?=\s*(?:$|[:\-–—]))")


def _roman_to_arabic(text: str) -> str:
    def repl(m: re.Match) -> str:
        n = _ROMAN.get(m.group(1))
        return str(n) if n else m.group(0)

    return _ROMAN_WORD.sub(repl, text)


def title_key(text: str) -> str:
    """标题（已去掉说明段的主体，或 IMDb 的标题）→ 标题键。续集编号与副标题保留在键里。"""
    s = unicodedata.normalize("NFKC", _MARKS.sub("", text))
    s = _roman_to_arabic(s)                              # 罗马数字必须在大小写折叠之前判断
    s = s.replace("&", " and ").replace("+", " and ")
    s = _APOSTROPHES.sub("", fold(s))
    tokens = _NONWORD.sub(" ", s).split()
    if len(tokens) > 1 and tokens[0] in ARTICLES:
        tokens = tokens[1:]
    return " ".join(tokens)


# ---------------------------------------------------------------- 说明分类（R08）

_Y = r"(?:18[89]\d|19\d\d|20[01]\d)"
_LANGS = (r"English|Spanish|French|German|Italian|Japanese|Portuguese|Russian|Chinese|Mandarin|Cantonese|Korean|"
          r"Hindi|Dutch|Swedish|Danish|Norwegian|Polish|Greek|Turkish|Arabic|Hebrew")
_DIRECTORS = r"Director[’']?s?"

# (属性字段, 类别, 规范值, 正则)。按顺序匹配，已匹配的片段被遮住，不会被后面的条目重复匹配。
# 规范值为 None 时取匹配到的文字（小写）。类别的先后也是一段说明有多个类别时取“主类别”的顺序。
_VOCAB: tuple[tuple[str, str, str | None, re.Pattern], ...] = tuple(
    (f, c, v, re.compile(p, flags)) for f, c, v, p, flags in (
        ("title_year", "year", None, rf"\b{_Y}\b", 0),
        ("edition.language", "language", "\\1", rf"\b({_LANGS})\s+(?:Version|Language|Dubbed|Subtitles?|Subtitled|Audio)\b",
         re.I),
        ("edition.language", "language", "subtitled", r"\bSubtitled\b", re.I),
        ("edition.language", "language", "dubbed", r"\bDubbed\b", re.I),
        ("edition.region", "region", "region \\1", r"\bRegion\s*(\d)\b", re.I),
        ("edition.video_system", "region", None, r"\b(?:PAL|NTSC|SECAM)\b", re.I),
        ("edition.region", "region", None, r"\b(?:UK|U\.K\.|USA|US)\b", 0),
        ("edition.region", "region", None, r"\b(?:Import(?:ed)?|Canadian|Canada|Australian|Australia|European|Europe)\b",
         re.I),
        ("edition.cut_type", "cut", "directors cut", rf"\b{_DIRECTORS}\s+Cut\b", re.I),
        ("edition.cut_type", "cut", "final cut", r"\bFinal\s+Cut\b", re.I),
        ("edition.cut_type", "cut", "extended", r"\bExtended(?:\s+(?:Edition|Version|Cut))?\b", re.I),
        ("edition.cut_type", "cut", "unrated", r"\bUnrated\b", re.I),
        ("edition.cut_type", "cut", "uncut", r"\bUncut\b", re.I),
        ("edition.cut_type", "cut", "remastered", r"\b(?:Digitally\s+)?Remaster(?:ed)?\b", re.I),
        ("edition.cut_type", "cut", "restored", r"\b(?:Fully\s+)?Restored\b", re.I),
        ("edition.cut_type", "cut", "colorized", r"\bColou?rized\b", re.I),
        ("edition.cut_type", "cut", "theatrical", r"\bTheatrical(?:\s+(?:Edition|Version|Cut))?\b", re.I),
        ("edition.media_format", "media", "hd-dvd", r"\bHD[- ]?DVD\b", re.I),
        ("edition.media_format", "media", "blu-ray", r"\bBlu-?ray(?:\s*3D)?\b", re.I),
        ("edition.media_format", "media", "dvd", r"\bDVD(?:-Video)?\b", re.I),
        ("edition.media_format", "media", "vhs", r"\bVHS\b", re.I),
        ("edition.media_format", "media", "umd", r"\bUMD\b", re.I),
        ("edition.media_format", "media", "4k", r"\b(?:4K(?:\s*Ultra\s*HD)?|Ultra\s*HD)\b", re.I),
        ("edition.media_format", "media", "laserdisc", r"\bLaser\s*Disc\b", re.I),
        ("edition.media_format", "media", "digital", r"\bDigital(?:\s+(?:Copy|HD))?\b", re.I),
        ("edition.publisher_series", "brand", "criterion collection", r"\b(?:The\s+)?Criterion\s+Collection\b", re.I),
        ("edition.publisher_series", "brand", "superbit", r"\bSuperbit\b", re.I),
        ("edition.publisher_series", "brand", "anchor bay", r"\bAnchor\s+Bay\b", re.I),
        ("edition.publisher_series", "brand", "masterpiece collection", r"\bMasterpiece\s+Collection\b", re.I),
        ("edition.packaging", "packaging", None,
         r"\b(?:\d+(?:st|nd|rd|th)\s+Anniversary(?:\s+Edition)?|(?:Special|Collector[’']?s|Anniversary|Diamond|Ultimate|"
         r"Limited|Deluxe|Platinum|Signature|Legacy|Gold|Silver|Commemorative|Collectible)\s+(?:Edition|Series|Set))\b",
         re.I),
        ("edition.packaging", "packaging", None, r"\b(?:Two|Three|Four|Five|\d+)[- ]Discs?\b", re.I),
        ("edition.packaging", "packaging", None,
         r"\b(?:Snap\s*Case|Slim\s*Case|Steelbook|Gift\s*Set|Widescreen|Full\s*Screen|Fullscreen|Letterbox(?:ed)?|"
         r"Anamorphic|Packaging|Combo(?:\s+Pack)?|Editions?)\b", re.I),
    ))

# 覆盖检查时允许剩下的虚词与数字（“Blu-ray/DVD Combo in DVD Packaging” 里的 in）
_FILLER = frozenset({"and", "in", "the", "of", "with", "a", "an", "for", "from", "by", "only", "plus", "or", "to"})


@dataclass(frozen=True)
class Descriptor:
    """一段被从标题键里去掉的说明：原文、主类别与它带来的版本属性。"""
    raw: str
    category: str                                  # year/language/region/cut/media/brand/packaging/unknown
    position: str                                  # paren、bracket、dash、prefix
    attributes: tuple[tuple[str, str], ...] = ()   # (属性字段, 规范值)，同一字段的重复值已去重
    year: int | None = None
    covered: bool = False                          # 去掉词表命中的片段后，剩下的只有虚词


def classify_segment(text: str) -> tuple[str, tuple[tuple[str, str], ...], int | None, bool]:
    """一段说明 → (主类别, 属性, 年份, 是否完全由词表解释)。"""
    working = _APOSTROPHES.sub("'", unicodedata.normalize("NFKC", _MARKS.sub("", text)))
    attrs: list[tuple[str, str]] = []
    cats: list[str] = []
    year: int | None = None
    for field, category, canon, pattern in _VOCAB:
        for m in list(pattern.finditer(working)):
            value = m.group(0).lower() if canon is None else m.expand(canon).lower()
            if field == "title_year":
                year = year if year is not None else int(m.group(0))
            elif (field, value) not in attrs:
                attrs.append((field, value))
            cats.append(category)
            working = working[:m.start()] + " " * (m.end() - m.start()) + working[m.end():]
    leftover = [t for t in _NONWORD.sub(" ", working.casefold()).split() if t not in _FILLER and not t.isdigit()]
    return (cats[0] if cats else "unknown"), tuple(attrs), year, bool(cats) and not leftover


_BRACKETS = re.compile(r"\[[^\]]*\]|\((?:[^()]|\([^()]*\))*\)")
_BRAND_PREFIX = re.compile(r"^\s*(Superbit[™®©]?)\s+", re.I)
_TAIL_SEP = re.compile(r"\s+[-–—]\s+|\s*[:,]\s+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class TitleParts:
    raw: str
    body: str                                      # 去掉说明后的标题主体
    key: str
    descriptors: tuple[Descriptor, ...]
    title_year: int | None                         # 括号里的四位年份；其他位置的数字不算


def _descriptor(raw: str, position: str) -> Descriptor:
    category, attrs, year, covered = classify_segment(raw)
    return Descriptor(raw, category, position, attrs, year, covered)


def split_title(raw: str) -> TitleParts:
    """原始标题 → 标题主体、标题键与说明段。括号段无论分到哪一类都从键里去掉（未知类也去掉，见 design 取舍）；
    破折号、冒号、逗号之后的尾部说明只在完全由词表解释时才去掉，避免 “Star Trek - The Motion Picture” 被截断。"""
    text = raw
    descriptors: list[Descriptor] = []
    m = _BRAND_PREFIX.match(text)
    if m:
        descriptors.append(_descriptor(m.group(1), "prefix"))
        text = text[m.end():]
    pieces: list[tuple[int, Descriptor]] = []
    for m in _BRACKETS.finditer(text):
        seg = m.group(0)
        pieces.append((m.start(), _descriptor(seg[1:-1].strip(), "paren" if seg[0] == "(" else "bracket")))
    descriptors.extend(d for _, d in pieces)
    text = _SPACES.sub(" ", _BRACKETS.sub(" ", text)).strip()
    for _ in range(4):                                # 末尾可能连着多段说明
        seps = list(_TAIL_SEP.finditer(text))
        if not seps:
            break
        last = seps[-1]
        tail = text[last.end():].strip()
        d = _descriptor(tail, "dash")
        if not tail or d.category == "unknown" or not d.covered:
            break
        descriptors.append(d)
        text = text[:last.start()].strip()
    year = next((d.year for d in descriptors if d.year is not None), None)
    return TitleParts(raw, text, title_key(text), tuple(descriptors), year)


# ---------------------------------------------------------------- 姓名（R10）

PLACEHOLDER_NAMES = frozenset({"various", "various artists", "artist not provided", "n/a", "na", "unknown",
                               "not available", "none", "not specified"})
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "md", "phd"})
STRONG_QUALIFIERS = frozenset({"byline", "reversed", "detail"})   # 参与评分的人物来源；其余只算弱证据或不参与


def strict_name(name: str) -> str:
    """严格键：去重音、大小写折叠；撇号与缩写点直接删除，其他标点换成空格。"""
    s = fold(name).replace(".", "")
    return _NONWORD.sub(" ", _APOSTROPHES.sub("", s)).strip()


_PLACEHOLDER_KEYS = frozenset(strict_name(n) for n in PLACEHOLDER_NAMES)


def loose_name(strict: str) -> str:
    """宽松键：去掉中间的单字母缩写（“hamilton s luske” → “hamilton luske”）；只作弱证据。"""
    tokens = strict.split()
    if len(tokens) <= 2:
        return strict
    return " ".join(t for i, t in enumerate(tokens) if len(t) > 1 or i in (0, len(tokens) - 1))


def name_keys(name: str) -> tuple[str, str]:
    s = strict_name(name)
    return s, loose_name(s)


@dataclass(frozen=True)
class Person:
    raw: str
    strict: str | None            # 占位署名为 None，不参与比较
    loose: str | None
    qualifier: str                # byline、reversed、detail、name_ambiguous、placeholder
    ordinal: int                  # 来源字段的区域内序号


def _reverse_if_last_first(value: str) -> tuple[str, bool]:
    """“姓, 名” → “名 姓”：恰好一个逗号、两侧非空、右侧不是 Jr. 之类的后缀。"""
    parts = [p.strip() for p in value.split(",")]
    if len(parts) == 2 and all(parts) and strict_name(parts[1]) not in _SUFFIXES:
        return f"{parts[1]} {parts[0]}", True
    return value, False


def _person(raw: str, qualifier: str, ordinal: int) -> Person:
    if strict_name(raw) in _PLACEHOLDER_KEYS or not strict_name(raw):
        return Person(raw, None, None, "placeholder", ordinal)
    strict, loose = name_keys(raw)
    return Person(raw, strict, loose, qualifier, ordinal)


def looks_last_first(tokens: Sequence[str]) -> bool:
    """“Arlt, Lewis, Brown, Blair” 这样每项只有一个词、项数为偶数的逗号串：边界无法确定。"""
    return len(tokens) >= 2 and len(tokens) % 2 == 0 and all(len(t.split()) == 1 for t in tokens)


def parse_people(byline: Sequence[tuple[int, str]], detail: Sequence[tuple[int, str]]) -> list[Person]:
    """署名行优先，每行一人；只有一个逗号的行按“姓, 名”倒序。没有署名行才按逗号拆分详情要点
    （项数为偶数且每项一个词时判为 name_ambiguous，只算弱证据）。有署名行时不使用详情要点。"""
    people: list[Person] = []
    if byline:
        for ordinal, value in byline:
            value = value.strip()
            if not value:
                continue
            name, reversed_ = _reverse_if_last_first(value)
            p = _person(name, "reversed" if reversed_ else "byline", ordinal)
            people.append(Person(value, p.strict, p.loose, p.qualifier, ordinal))   # 原值保留“姓, 名”写法
        return people
    for ordinal, value in detail:
        tokens = [t.strip() for t in value.split(",") if t.strip()]
        ambiguous = looks_last_first(tokens)
        for t in tokens:
            p = _person(t, "detail", ordinal)
            people.append(Person(p.raw, p.strict, p.loose, "name_ambiguous", ordinal)
                          if ambiguous and p.qualifier == "detail" else p)
    return people


# ---------------------------------------------------------------- 片长、日期、类型（R11）

_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y", "%Y-%m-%d")


def release_date_iso(text: str) -> str | None:
    """商品发售日期 → ISO 日期（缺日时取月初）；只作版本属性，不是作品年份的证据。"""
    cleaned = text.strip().replace("Sept ", "Sep ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def genre_tags(text: str) -> list[str]:
    return [t for t in (fold(p).strip() for p in re.split(r"[,/]", text)) if t]


# ---------------------------------------------------------------- 合集成员（R17）

MEMBER_MIN, MEMBER_MAX = 2, 6
_FEATURE_MARK = re.compile(r"\b(?:Double|Triple)\s+Feature\b", re.I)


@dataclass(frozen=True)
class CollectionSplit:
    mode: str                   # paren_list、slash、feature_amp
    members: tuple[str, ...]    # 成员原文，按页面顺序
    valid: bool                 # 成员数在 2–6 之内


def split_collection(raw: str) -> CollectionSplit | None:
    """按显式分隔拆分成员：括号里的 “ / ” 列表，标题里的 “ / ”，带 Double/Triple Feature 标记的 “A & B”。
    成员数不在 2–6 之间时 valid=False（调用方记 collection_unsplit）；没有任何显式分隔返回 None。"""
    candidate: tuple[str, list[str]] | None = None
    for m in _BRACKETS.finditer(raw):
        inner = m.group(0)[1:-1]
        if " / " not in inner:
            continue
        items = [i.strip() for i in inner.split(" / ")]
        # “(Blu-ray 3D / Blu-ray / DVD)” 这类全是版本说明的括号不是成员列表
        if all(i and classify_segment(i)[0] != "unknown" for i in items):
            continue
        candidate = ("paren_list", items)
        break
    if candidate is None:
        body = split_title(raw).body
        if " / " in body:
            candidate = ("slash", [i.strip() for i in body.split(" / ")])
        elif _FEATURE_MARK.search(raw) and " & " in body:
            candidate = ("feature_amp", [i.strip() for i in body.split(" & ")])
    if candidate is None:
        return None
    mode, items = candidate
    return CollectionSplit(mode, tuple(items), MEMBER_MIN <= len(items) <= MEMBER_MAX and all(items))
