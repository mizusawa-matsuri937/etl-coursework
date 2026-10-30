"""姓名规范化与兼容等级（spec R06–R09、R11，NORM_VERSION = t5n1）。纯函数，不访问数据库。

原始姓名永远不改：这里只产出拆分出的部分（倒序、昵称、括号段、称谓、后缀）与五种比较键，调用方把它们与原值一起写入提及。
严格键沿用任务 4 的 strict_name，便于与任务 4 的断言对照；后缀保留在严格键里（Lon Chaney 与 Lon Chaney Jr. 不同键）。
改动词表或任何一步的行为必须升 etl_task5.NORM_VERSION。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from etl_task4.normalize import PLACEHOLDER_NAMES, looks_last_first, strict_name

SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
HONORIFICS = frozenset({"dr", "sir", "dame", "mr", "mrs", "ms", "prof"})
ROMAN_DISAMBIGUATORS = frozenset({"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"})
REPLACEMENT = "�"

_PLACEHOLDER_KEYS = frozenset(strict_name(n) for n in PLACEHOLDER_NAMES)
_PAREN = re.compile(r"\s*\(([^()]*)\)")
_NICK = re.compile(r"(?:(?<=\s)|^)[\"'“‘]([^\"'“”‘’]+)[\"'”’](?=\s|$)")
_WILD = "zzwildzz"


@dataclass(frozen=True)
class Name:
    raw: str
    display: str                  # 倒序后、去掉昵称、括号段与称谓的写法（保留后缀）
    reversed: bool
    nickname: str | None
    disambiguator: str | None     # 罗马数字括号段，如 “(III)”
    paren_text: str | None        # 其他括号段原文
    honorific: str | None
    suffix: str | None
    encoding_damaged: bool
    strict: str
    loose: str
    tokenset: str
    family: str
    initial: str

    @property
    def placeholder(self) -> bool:
        return not self.strict or self.strict in _PLACEHOLDER_KEYS

    def core(self) -> list[str]:
        """严格键去掉末尾后缀后的词。"""
        toks = self.strict.split()
        while toks and toks[-1] in SUFFIXES and len(toks) > 1:
            toks.pop()
        return toks

    def suffix_key(self) -> str:
        return " ".join(self.strict.split()[len(self.core()):])


def _reverse(value: str) -> tuple[str, bool, str | None]:
    """恰好一个逗号：右侧是后缀时并回（“Robert Downey, Jr.”），否则倒序为“名 姓”。"""
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2 or not all(parts):
        return value, False, None
    if strict_name(parts[1]) in SUFFIXES:
        return f"{parts[0]} {parts[1]}", False, parts[1]
    return f"{parts[1]} {parts[0]}", True, None


def parse(raw: str) -> Name:
    value = " ".join(raw.split())
    value, reversed_, comma_suffix = _reverse(value)
    nickname = None
    m = _NICK.search(value)
    if m:
        nickname = m.group(1).strip()
        value = (value[:m.start()] + value[m.end():]).strip()
    disamb = paren = None
    for pm in list(_PAREN.finditer(value)):
        inner = pm.group(1).strip()
        if inner.upper() in ROMAN_DISAMBIGUATORS and disamb is None:
            disamb = inner.upper()
        else:
            paren = inner if paren is None else f"{paren}; {inner}"
    value = " ".join(_PAREN.sub(" ", value).split())
    tokens = value.split()
    honorific = None
    if len(tokens) > 1 and strict_name(tokens[0]) in HONORIFICS:
        honorific = tokens.pop(0)
    suffix = comma_suffix
    if suffix is None and len(tokens) > 2 and strict_name(tokens[-1]) in SUFFIXES:
        suffix = tokens[-1]
    display = " ".join(tokens)
    strict = strict_name(display)
    return _keys(raw, display, strict, reversed_, nickname, disamb, paren, honorific, suffix)


def _keys(raw, display, strict, reversed_, nickname, disamb, paren, honorific, suffix) -> Name:
    toks = strict.split()
    core = list(toks)
    while core and core[-1] in SUFFIXES and len(core) > 1:
        core.pop()
    tail = toks[len(core):]
    if len(core) > 2:
        loose_core = [t for i, t in enumerate(core) if len(t) > 1 or i in (0, len(core) - 1)]
    else:
        loose_core = core
    family = core[-1] if core else ""
    initial = f"{core[0][0]} {family}" if len(core) >= 2 else ""
    return Name(raw=raw, display=display, reversed=reversed_, nickname=nickname, disambiguator=disamb,
                paren_text=paren, honorific=honorific, suffix=suffix, encoding_damaged=REPLACEMENT in raw,
                strict=strict, loose=" ".join(loose_core + tail), tokenset=" ".join(sorted(toks)), family=family,
                initial=initial)


def is_placeholder(raw: str) -> bool:
    return parse(raw).placeholder


# ---------------------------------------------------------------- 详情要点的逗号串（R05）

def split_list(value: str) -> tuple[list[str], bool]:
    """逗号串 → 名字列表；单独成项的后缀并回前一个名字（“Robert Downey, Jr.”）。
    第二个返回值为 True 表示边界无法确定（任务 4 的 name_ambiguous 规则：偶数个单词项），整串不参与归并。"""
    raw = [t.strip() for t in value.split(",") if t.strip()]
    tokens: list[str] = []
    for t in raw:
        if tokens and strict_name(t) in SUFFIXES:
            tokens[-1] = f"{tokens[-1]}, {t}"
        else:
            tokens.append(t)
    return tokens, looks_last_first(tokens)


# ---------------------------------------------------------------- 兼容等级（R11）

L_STRICT, L_TOKENSET, L_PARTIAL, L_INITIAL, L_NEAR = "L1", "L2", "L3", "LI", "L4"
X_LEVEL = {L_STRICT: "X1", L_TOKENSET: "X2", L_PARTIAL: "X3", L_INITIAL: "X4", L_NEAR: "X4"}
N_LEVEL = {L_STRICT: "N1", L_TOKENSET: "N2", L_PARTIAL: "N2", L_INITIAL: "N3", L_NEAR: "N4"}


def _initial_of(a: str, b: str) -> bool:
    return len(a) == 1 and b.startswith(a) or len(b) == 1 and a.startswith(b)


def _middles_compatible(ma: Sequence[str], mb: Sequence[str]) -> bool:
    if not ma or not mb:
        return True
    return len(ma) == len(mb) and all(x == y or _initial_of(x, y) for x, y in zip(ma, mb))


def levenshtein(a: str, b: str, cutoff: int) -> int:
    """编辑距离，超过 cutoff 时返回 cutoff + 1。"""
    if abs(len(a) - len(b)) > cutoff:
        return cutoff + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > cutoff:
            return cutoff + 1
        prev = cur
    return prev[-1]


def _initials_conflict(a: str, b: str) -> bool:
    """词数相同、对应位置上都是单字母却不同（“john a smith” 与 “john b smith”）：是两个人，不能当作拼写错误。"""
    ta, tb = a.split(), b.split()
    return len(ta) == len(tb) and any(x != y and len(x) == 1 and len(y) == 1 for x, y in zip(ta, tb))


def _wild_match(damaged: Name, other: Name) -> bool:
    """替换字符当作通配：损坏的字符在严格键里可能对应 0–2 个字母（ç → c）。"""
    key = strict_name(damaged.display.replace(REPLACEMENT, _WILD))
    pattern = "^" + re.escape(key).replace(_WILD, r"\w{0,2}") + "$"
    return re.match(pattern, other.strict) is not None


def level(a: Name, b: Name) -> str | None:
    """两个姓名的兼容等级：L1 严格键相同；L2 词集键相同；L3 首末词相同、只差中间缩写、中间名或后缀；
    LI 名只有首字母（“J. Smith” 与 “John Smith”）；L4 近似（替换字符通配，或编辑距离 ≤2、首字母相同、姓氏键编辑距离 ≤1）。
    不兼容为 None。称谓、昵称与括号段不在严格键里，所以只差这些的为 L1。"""
    if a.placeholder or b.placeholder:
        return None
    if a.strict == b.strict:
        return L_STRICT
    if a.tokenset == b.tokenset:
        return L_TOKENSET
    ca, cb = a.core(), b.core()
    sa, sb = a.suffix_key(), b.suffix_key()
    if len(ca) >= 2 and len(cb) >= 2 and ca[-1] == cb[-1] and (not sa or not sb or sa == sb):
        if ca[0] == cb[0] and _middles_compatible(ca[1:-1], cb[1:-1]):
            return L_PARTIAL
        if _initial_of(ca[0], cb[0]) and _middles_compatible(ca[1:-1], cb[1:-1]):
            return L_INITIAL
    if a.encoding_damaged and _wild_match(a, b) or b.encoding_damaged and _wild_match(b, a):
        return L_NEAR
    if _initials_conflict(a.strict, b.strict):
        return None
    if (len(ca) >= 2 and len(cb) >= 2 and ca[0][0] == cb[0][0] and min(len(a.strict), len(b.strict)) >= 6
            and levenshtein(a.strict, b.strict, 2) <= 2 and levenshtein(ca[-1], cb[-1], 1) <= 1):
        return L_NEAR
    return None
