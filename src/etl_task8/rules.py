"""任务 8 的规则版本与纯函数判定（spec R06–R09、R12–R19、R21、R24，规则版本 t8r1）。不访问数据库。

RuleSet 集中所有默认值：比较键、家族阈值、weak_support 上限、核验允许差异、代表记录与 helpfulness 选择、近似阈值。
四组字段分别构成四个阶段键的“规则签名”（design“输入键与阶段键”）：只改家族阈值时 review_family 及下游的阶段键变化，
review_clean 不变（R34）。改任何数值或决策算法都要登记新的规则版本（RuleSet.with_version）：规则版本进入输入键，
ALGORITHM 标记只是让阶段签名随之变化，不能代替升规则版本（任务 7 交接第 6 节第 4 条的教训）。

几处判定在 SQL 里批量执行（helpfulness 状态、内容键哈希、分区桶号），这里同时给出 Python 版本与 SQL 片段，
集成测试逐条核对两者一致（AC02、AC14）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from . import CURRENT_RULE_VERSION, RULE_VERSION

# 各阶段决策算法的标记：进入阶段签名；改算法须同时登记新的规则版本
ALGORITHM = {"clean": "c1", "family": "f1", "dedup": "d1", "attribution": "a1"}

HELPFULNESS_STATUSES = ("ok", "zero_zero", "numerator_exceeds_denominator", "negative", "unparseable", "missing")  # R06
HELPFULNESS_ANOMALIES = {"numerator_exceeds_denominator": "numerator_exceeds_denominator",
                         "negative": "negative_votes", "unparseable": "unparseable_helpfulness",
                         "missing": "missing_helpfulness"}              # 状态 → review_issue.code（R06；0/0 不是异常，R07）
ISSUE_CODES = ("numerator_exceeds_denominator", "negative_votes", "unparseable_helpfulness", "missing_helpfulness",
               "incomplete_review", "hash_collision")                   # 与 sql/701 的 CHECK 一致
MATCH_LEVELS = ("representative", "D1_exact_same_asin", "D2_same_asin_variant", "D3_family_copy")      # R19
MAPPING_STATUSES = ("mapped", "quarantined", "incomplete")
CANDIDATE_TYPES = ("cross_family_same_content", "near_duplicate")       # R20、R21
DIFF_FIELDS = ("helpfulness", "profile_name")                           # R17：核验只允许这两项不同

CLEAN_FIELDS = ("key_fields",)
FAMILY_FIELDS = ("family_jaccard", "weak_support_max")
DEDUP_FIELDS = ("allowed_diff", "representative", "helpfulness_pick", "near_dup_jaccard", "token_pattern")
ATTRIBUTION_FIELDS = ("near_dup_jaccard", "token_pattern", "collection_policy", "stat_group_edges")

_INT64_MAX = 2**63 - 1
_HELPFULNESS = re.compile(r"(-?\d+)/(-?\d+)", re.ASCII)     # 与任务 1 的解析式相同


@dataclass(frozen=True)
class RuleSet:
    version: str
    # ---- review_clean（R06–R09）
    key_fields: tuple[str, ...] = ("reviewer_id", "review_day", "score", "summary", "review_text")   # R09：内容键只用原值
    # ---- review_family（R12、R13）
    family_jaccard: float = 0.5         # 两个签名组共享内容键的 Jaccard ≥ 此值时相连
    weak_support_max: int = 2           # 家族最大评论集合 ≤ 此值时标 weak_support
    # ---- review_dedup（R14–R21）
    allowed_diff: tuple[str, ...] = DIFF_FIELDS
    representative: str = "min_record_no"          # R18：代表记录
    helpfulness_pick: str = "max_total_votes"      # R18：ok 或 zero_zero 的副本中总票数最大，并列取较小 record_no
    near_dup_jaccard: float = 0.9                  # R21：同一用户正文词集 Jaccard ≥ 此值记 near_duplicate
    token_pattern: str = r"[a-z0-9']+"             # R21：小写后的分词式
    # ---- review_attribution（R26–R31，T8-04 使用）
    collection_policy: str = "single_over_collection"                      # R27：合集不进作品；合集与单品并存按单品
    stat_group_edges: tuple[str, ...] = ("likely_duplicate_movie", "shared_logical_review")   # R29

    def with_version(self, version: str, **changes) -> "RuleSet":
        return dataclasses.replace(self, version=version, **changes)

    def signature(self, group: str) -> str:
        """某阶段规则签名：该组字段的取值与算法标记的摘要。组：clean、family、dedup、attribution。"""
        fields = {"clean": CLEAN_FIELDS, "family": FAMILY_FIELDS, "dedup": DEDUP_FIELDS,
                  "attribution": ATTRIBUTION_FIELDS}[group]
        payload = {name: getattr(self, name) for name in fields}
        payload["algorithm"] = ALGORITHM[group]
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=list)
        return f"{group[0]}{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


DEFAULT_RULES = RuleSet(RULE_VERSION)
RULESETS: dict[str, RuleSet] = {DEFAULT_RULES.version: DEFAULT_RULES}
assert CURRENT_RULE_VERSION in RULESETS


def ruleset(version: str) -> RuleSet:
    try:
        return RULESETS[version]
    except KeyError:
        raise ValueError(f"未登记的规则版本 {version!r}，已登记：{sorted(RULESETS)}") from None


# ---------------------------------------------------------------- helpfulness（R06、R07）

def helpfulness_status(raw: str | None) -> tuple[str, int | None, int | None]:
    """原值 → (状态, 有用票, 总票)。票数只在 ok、zero_zero 时有值（与任务 1 的规范值一致，异常时为 None）。
    判定顺序与任务 1 的解析相同：格式 → 负数 → 超出 int64（归 unparseable）→ 分子大于分母 → 0/0。"""
    if raw is None:
        return "missing", None, None
    m = _HELPFULNESS.fullmatch(raw)
    if not m:
        return "unparseable", None, None
    helpful, total = int(m.group(1)), int(m.group(2))
    if helpful < 0 or total < 0:
        return "negative", None, None
    if helpful > _INT64_MAX or total > _INT64_MAX:
        return "unparseable", None, None
    if helpful > total:
        return "numerator_exceeds_denominator", None, None
    if total == 0:
        return "zero_zero", 0, 0
    return "ok", helpful, total


def helpfulness_status_sql(col: str) -> str:
    """helpfulness_status 的 SQL 版本（列为原值）；与 helpfulness_status 逐项一致（AC02 核对）。"""
    a, b = f"split_part({col}, '/', 1)::numeric", f"split_part({col}, '/', 2)::numeric"
    return (f"CASE WHEN {col} IS NULL THEN 'missing' "
            f"WHEN {col} !~ '^-?[0-9]+/-?[0-9]+$' THEN 'unparseable' "
            f"WHEN {a} < 0 OR {b} < 0 THEN 'negative' "
            f"WHEN {a} > {_INT64_MAX} OR {b} > {_INT64_MAX} THEN 'unparseable' "
            f"WHEN {a} > {b} THEN 'numerator_exceeds_denominator' "
            f"WHEN {b} = 0 THEN 'zero_zero' ELSE 'ok' END")


# ---------------------------------------------------------------- 内容键（R09）

def score_text(score: Decimal | None) -> str | None:
    """规范 Decimal 的文本：3.0 → '3'，4.50 → '4.5'（SQL 为 trim_scale(score)::text）。"""
    if score is None:
        return None
    return format(Decimal(score).normalize(), "f")


def day_text(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def content_key(reviewer_id: str | None, review_day: date | None, score: Decimal | None, summary: str | None,
                text: str | None) -> bytes:
    """五项原值的长度前缀串（字符数 + ':' + 值；NULL 记 '-'），避免分隔符歧义；不做大小写、空白或 HTML 规范化。"""
    parts = (reviewer_id, day_text(review_day), score_text(score), summary, text)
    return "".join("-" if p is None else f"{len(p)}:{p}" for p in parts).encode("utf-8")


def content_hash(reviewer_id: str | None, review_day: date | None, score: Decimal | None, summary: str | None,
                 text: str | None) -> bytes:
    return hashlib.sha256(content_key(reviewer_id, review_day, score, summary, text)).digest()


def _prefixed_sql(expr: str) -> str:
    return f"coalesce(length({expr})::text || ':' || {expr}, '-')"


def content_hash_sql(reviewer: str, day_utc: str, score: str, summary: str, text: str) -> str:
    """content_hash 的 SQL 版本。day_utc 为 timestamptz 列（取 UTC 日期），score 为 numeric 列。"""
    day = f"to_char({day_utc} AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
    parts = ", ".join(_prefixed_sql(e) for e in (reviewer, day, f"trim_scale({score})::text", summary, text))
    return f"sha256(convert_to(concat({parts}), 'UTF8'))"


# ---------------------------------------------------------------- 分区桶（R36）

BUCKETS = 1024      # 固定桶数；N 个分区时第 p 区为 bucket % N = p 的桶


def bucket(value: str | None) -> int:
    h = hashlib.sha256((value or "").encode("utf-8")).digest()
    return (h[0] << 2) | (h[1] >> 6)


def bucket_sql(expr: str) -> str:
    h = f"sha256(convert_to(coalesce({expr}, ''), 'UTF8'))"
    return f"(get_byte({h}, 0) * 4 + get_byte({h}, 1) / 64)"


def partition_buckets(partition: int, partitions: int) -> list[int]:
    if not 1 <= partitions <= BUCKETS or not 0 <= partition < partitions:
        raise ValueError(f"分区 {partition}/{partitions} 不合法（分区数 1–{BUCKETS}）")
    return [b for b in range(BUCKETS) if b % partitions == partition]


# ---------------------------------------------------------------- 评论家族（R12、R13、R24）

def signature(hashes: Iterable[bytes]) -> bytes:
    """评论集合签名：去重排序后的内容键哈希串的 SHA-256（SQL 为 sha256(string_agg(DISTINCT h ORDER BY h))）。"""
    return hashlib.sha256(b"".join(sorted(set(hashes)))).digest()


def jaccard(shared: int, size_a: int, size_b: int) -> float:
    union = size_a + size_b - shared
    return shared / union if union > 0 else 0.0


@dataclass(frozen=True)
class Edge:
    sig_a: bytes
    sig_b: bytes
    shared: int
    jaccard: float


def family_components(sizes: dict[bytes, int], overlaps: Iterable[tuple[bytes, bytes, int]],
                      threshold: float) -> tuple[dict[bytes, bytes], list[Edge]]:
    """签名组的连通分量：Jaccard ≥ threshold 的签名组对相连。返回 (签名 → 分量内最小签名, 相连的边)。
    并查集按最小签名合并，结果与边的顺序无关。"""
    parent = {s: s for s in sizes}

    def find(x: bytes) -> bytes:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges: list[Edge] = []
    for a, b, shared in overlaps:
        if a == b:
            continue
        a, b = min(a, b), max(a, b)
        j = jaccard(shared, sizes[a], sizes[b])
        if j >= threshold:
            edges.append(Edge(a, b, shared, j))
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {s: find(s) for s in sizes}, sorted(edges, key=lambda e: (e.sig_a, e.sig_b))


def family_id(min_asin: str) -> str:
    """R24：rf_ + sha256(家族内最小 ASIN) 前 16 位。"""
    return "rf_" + hashlib.sha256(min_asin.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Family:
    family_id: str
    asins: tuple[str, ...]              # 按码点排序（与 COLLATE "C" 相同）
    signatures: tuple[bytes, ...]
    max_review_count: int
    weak_support: bool
    edges: tuple[Edge, ...]


def assemble_families(asin_sig: dict[str, bytes], sizes: dict[bytes, int], root_of: dict[bytes, bytes],
                      edges: Sequence[Edge], weak_support_max: int) -> list[Family]:
    """ASIN → 家族：同一分量的签名组的全部 ASIN 为一个家族；最大评论集合 ≤ weak_support_max 时标 weak_support（R13）。"""
    by_root: dict[bytes, list[str]] = {}
    for asin, sig in asin_sig.items():
        by_root.setdefault(root_of[sig], []).append(asin)
    edges_of: dict[bytes, list[Edge]] = {}
    for e in edges:
        edges_of.setdefault(root_of[e.sig_a], []).append(e)
    out = []
    for root, asins in by_root.items():
        asins.sort()
        sigs = tuple(sorted({asin_sig[a] for a in asins}))
        biggest = max(sizes[s] for s in sigs)
        out.append(Family(family_id(asins[0]), tuple(asins), sigs, biggest, biggest <= weak_support_max,
                          tuple(edges_of.get(root, ()))))
    return sorted(out, key=lambda f: f.asins[0])


# ---------------------------------------------------------------- 去重层次与核验（R14–R19、R24）

@dataclass(frozen=True)
class Copy:
    """同一逻辑评论的一条原始记录（内容键已核验相同）。"""
    record_no: int
    asin: str
    helpfulness_raw: str | None
    profile_name_raw: str | None
    helpfulness_status: str
    helpful_votes: int | None
    total_votes: int | None


def diff_fields(a: Copy, b: Copy) -> list[str]:
    """R15、R17：内容键相同的两条记录只可能在 helpfulness 与显示名上不同。"""
    out = []
    if a.helpfulness_raw != b.helpfulness_raw:
        out.append("helpfulness")
    if a.profile_name_raw != b.profile_name_raw:
        out.append("profile_name")
    return out


def assign_levels(copies: Sequence[Copy]) -> list[tuple[int, str, list[str]]]:
    """一个逻辑评论内的层次（R14–R16、R18、R19），返回 (record_no, match_level, diff_fields)，按 record_no 排序。
    代表记录 = 最小 record_no；每个 ASIN 上最早的记录是该 ASIN 的首条：代表以外的首条为 D3（差异相对代表）；
    同一 ASIN 的其余记录与该 ASIN 首条比较，原值全同为 D1，否则为 D2。"""
    ordered = sorted(copies, key=lambda c: c.record_no)
    rep = ordered[0]
    first_on: dict[str, Copy] = {}
    out = []
    for c in ordered:
        if c is rep:
            first_on[c.asin] = c
            out.append((c.record_no, "representative", []))
        elif c.asin not in first_on:
            first_on[c.asin] = c
            out.append((c.record_no, "D3_family_copy", diff_fields(c, rep)))
        else:
            d = diff_fields(c, first_on[c.asin])
            out.append((c.record_no, "D2_same_asin_variant" if d else "D1_exact_same_asin", d))
    return out


def pick_helpfulness(copies: Sequence[Copy]) -> tuple[int | None, int | None, int | None]:
    """R18：ok 或 zero_zero 的副本中总票数最大者（并列取较小 record_no）→ (有用票, 总票, record_no)；没有合格副本时全为 None。"""
    best: Copy | None = None
    for c in sorted(copies, key=lambda c: c.record_no):
        if c.helpfulness_status not in ("ok", "zero_zero") or c.total_votes is None:
            continue
        if best is None or c.total_votes > best.total_votes:
            best = c
    return (None, None, None) if best is None else (best.helpful_votes, best.total_votes, best.record_no)


def split_collisions(rows: Sequence[tuple[int, tuple]]) -> list[list[int]]:
    """R09：同一分组哈希下按完整原值（用户、评论日、评分、摘要、正文）拆组；返回按最小 record_no 排序的 record_no 子组。
    原值全同时只有一个子组；多于一个子组即哈希碰撞。"""
    groups: dict[tuple, list[int]] = {}
    for record_no, raw in sorted(rows):
        groups.setdefault(raw, []).append(record_no)
    return sorted(groups.values(), key=lambda g: g[0])


def logical_review_id(content_hash_value: bytes, fam_id: str, variant: int = 0) -> str:
    """R24：lr_ + sha256(内容键哈希的十六进制 || '|' || family_id) 前 20 位。哈希碰撞拆出的第 2 个起的子组
    在哈希输入末尾加 '|v<序号>'（序号按子组最小 record_no 排序，从 1 起），避免两条不同评论同 ID。"""
    raw = f"{content_hash_value.hex()}|{fam_id}" + (f"|v{variant}" if variant else "")
    return "lr_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------- 不合并的候选（R20、R21）

def word_set(text: str | None, pattern: str = DEFAULT_RULES.token_pattern) -> frozenset[str]:
    """R21：小写后按分词式取词集。默认分词式只认 ASCII 字母数字与撇号，非 ASCII 字符作为分隔。"""
    return frozenset(re.findall(pattern, (text or "").lower()))


def word_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """词集 Jaccard；两边都没有词时为 0（没有可比证据，不成候选）。"""
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def near_duplicate_pairs(items: Sequence[tuple[str, bytes, str | None]], threshold: float,
                         pattern: str = DEFAULT_RULES.token_pattern) -> list[tuple[str, str, float]]:
    """同一用户、同一范围内的逻辑评论 (logical_review_id, 正文 md5, 正文) 两两比较：正文不同且词集 Jaccard ≥ threshold
    的对 → (较小 ID, 较大 ID, 相似度，四位小数)。按 ID 排序，结果与输入顺序无关。"""
    sets = [(lid, md5, word_set(text, pattern)) for lid, md5, text in sorted(items, key=lambda x: x[0])]
    out = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            (a, ma, wa), (b, mb, wb) = sets[i], sets[j]
            if ma == mb:
                continue
            sim = word_jaccard(wa, wb)
            if sim >= threshold:
                out.append((a, b, round(sim, 4)))
    return out


def cross_family_groups(items: Iterable[tuple[str, bytes, str]]) -> list[list[str]]:
    """R20：同一用户的逻辑评论 (logical_review_id, 内容键哈希, family_id)，内容键相同而分属 ≥ 2 个家族的为一组。"""
    by_hash: dict[bytes, list[tuple[str, str]]] = {}
    for lid, h, fam in items:
        by_hash.setdefault(h, []).append((lid, fam))
    return sorted(sorted(lid for lid, _ in members) for members in by_hash.values()
                  if len({fam for _, fam in members}) >= 2)


def candidate_id(candidate_type: str, logical_ids: Sequence[str]) -> str:
    raw = candidate_type + "|" + ",".join(sorted(logical_ids))
    return "dc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 评分统计组与敏感性（R29、R31）

def stat_group_id(min_movie_id: str) -> str:
    """sg_ + sha256(组内最小 movie_id) 前 16 位（与 family_id 同一写法）。"""
    return "sg_" + hashlib.sha256(min_movie_id.encode("utf-8")).hexdigest()[:16]


def components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> dict[str, str]:
    """无向图的连通分量：节点 → 分量内最小节点（按码点）。边的端点不在 nodes 里时一并加入。结果与顺序无关。"""
    parent: dict[str, str] = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {n: find(n) for n in parent}


def sensitivity(items: dict[str, tuple[Decimal, date]], pairs: Iterable[tuple[str, str]]
                ) -> tuple[int, Decimal | None, int]:
    """R31：把每组近似评论（near_duplicate 对连成的分量）只计一次时的 (分母, 平均分, 去掉的条数)。
    items 为统计范围内的逻辑评论 ID → (评分, 评论日)；分量内保留评论日最早的一条（并列取较小 ID），其余不计。
    端点不在 items 里的对忽略（不属于该范围）。"""
    inside = [(a, b) for a, b in pairs if a in items and b in items]
    root = components(items, inside)
    keep: dict[str, str] = {}
    for lid in sorted(items, key=lambda x: (items[x][1], x)):
        keep.setdefault(root[lid], lid)
    kept = list(keep.values())
    if not kept:
        return 0, None, 0
    total = sum((items[k][0] for k in kept), Decimal(0))
    return len(kept), total / len(kept), len(items) - len(kept)
