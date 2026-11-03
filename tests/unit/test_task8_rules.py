"""任务 8 规则层的纯函数（spec AC14；AC02 的 Python 判定部分）。预期值手写，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task8_rules.py
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from etl_task8.rules import (BUCKETS, DEFAULT_RULES, Copy, assign_levels, bucket, candidate_id, content_hash,
                             content_key, cross_family_groups, family_components, family_id, assemble_families,
                             helpfulness_status, jaccard, logical_review_id, near_duplicate_pairs, partition_buckets,
                             pick_helpfulness, score_text, signature, split_collisions, word_jaccard, word_set)
from etl_task8.run import RunInputs, short_key

D = date(2007, 6, 25)


# ---------------------------------------------------------------- AC02：helpfulness 状态

@pytest.mark.parametrize("raw, expected", [
    ("3/5", ("ok", 3, 5)),
    ("0/0", ("zero_zero", 0, 0)),
    ("8/1", ("numerator_exceeds_denominator", None, None)),
    ("-1/2", ("negative", None, None)),
    ("abc", ("unparseable", None, None)),
    (None, ("missing", None, None)),
    ("-0/5", ("ok", 0, 5)),                                       # 与任务 1 一致：-0 不是负数
    ("1/2/3", ("unparseable", None, None)),
    (" 1/2", ("unparseable", None, None)),
    ("99999999999999999999/99999999999999999999", ("unparseable", None, None)),   # 超出 int64（任务 1 的 out_of_range）
    ("5/0", ("numerator_exceeds_denominator", None, None)),
])
def test_ac02_helpfulness_status(raw, expected):
    assert helpfulness_status(raw) == expected


# ---------------------------------------------------------------- AC14：内容键（R09）

def test_ac14_content_key_uses_raw_values_only():
    base = content_key("U1", D, Decimal("5.0"), "Great", "Loved it.")
    assert content_key("U1", D, Decimal("5"), "Great", "Loved it.") == base             # 规范 Decimal：5.0 与 5 相同
    assert content_key("U1", D, Decimal("5.0"), "Great", "loved it.") != base           # K1 只差大小写
    assert content_key("U1", D, Decimal("5.0"), "Great", "Loved it. ") != base          # K2 只差末尾空格
    assert content_key("U1", D, Decimal("5.0"), "GREAT", "Loved it.") != base           # K3 同正文不同摘要
    assert content_key("U1", D, Decimal("5.0"), None, "Loved it.") != content_key("U1", D, Decimal("5.0"), "", "Loved it.")
    assert content_key("U1", date(2007, 6, 26), Decimal("5.0"), "Great", "Loved it.") != base
    assert content_key("U2", D, Decimal("5.0"), "Great", "Loved it.") != base


def test_ac14_content_key_length_prefix_is_unambiguous():
    # 没有长度前缀时 "ab" + "c" 与 "a" + "bc" 拼出同一个串
    assert content_key("U1", D, Decimal(4), "ab", "c") != content_key("U1", D, Decimal(4), "a", "bc")
    assert content_key("U1", D, Decimal(4), "1:a", "b") != content_key("U1", D, Decimal(4), "1", ":ab")
    assert content_key("U1", D, Decimal(4), "café", "x") == "2:U110:2007-06-251:44:café1:x".encode()   # 长度按字符计


def test_ac14_score_text_and_hash():
    assert (score_text(Decimal("3.0")), score_text(Decimal("4.50")), score_text(Decimal("1")), score_text(None)) == \
        ("3", "4.5", "1", None)
    h = content_hash("U1", D, Decimal("5.0"), "S", "T")
    assert h == hashlib.sha256(content_key("U1", D, Decimal("5.0"), "S", "T")).digest() and len(h) == 32


# ---------------------------------------------------------------- AC14：分区桶

def test_ac14_buckets_partition_every_bucket_once():
    assert all(0 <= bucket(v) < BUCKETS for v in ("A141HP4LYPWMSR", "", None, "Ünicode"))
    assert bucket("A141HP4LYPWMSR") == bucket("A141HP4LYPWMSR") and bucket(None) == bucket("")
    for n in (1, 3, 32):
        parts = [partition_buckets(p, n) for p in range(n)]
        assert sorted(b for part in parts for b in part) == list(range(BUCKETS))
    with pytest.raises(ValueError):
        partition_buckets(32, 32)
    with pytest.raises(ValueError):
        partition_buckets(0, 0)


# ---------------------------------------------------------------- AC14：签名与 Jaccard 连通分量（R12、R13、R24）

def _keys(prefix: str, n: int) -> list[bytes]:
    return [hashlib.sha256(f"{prefix}{i}".encode()).digest() for i in range(n)]


def test_ac14_signature_is_set_based():
    a, b = _keys("k", 3), _keys("k", 3)
    assert signature(a) == signature(reversed(b)) == signature(a + a[:1])
    assert signature(a) != signature(a[:2])


def _ac04_sets() -> dict[str, list[bytes]]:
    """AC04 的评论集合：A、B 相同 10 条；C 是 A 的 6 条（J = 0.6）；D 与 A 共 1 条；E 无共享；F、G 各 1 条且相同。"""
    a = _keys("a", 10)
    return {"A": a, "B": list(a), "C": a[:6], "D": a[:1] + _keys("d", 2), "E": _keys("e", 4),
            "F": _keys("f", 1), "G": _keys("f", 1)}


def _families(sets: dict[str, list[bytes]], threshold: float):
    asin_sig = {asin: signature(keys) for asin, keys in sets.items()}
    sizes = {asin_sig[a]: len(set(k)) for a, k in sets.items()}
    by_sig: dict[bytes, set[bytes]] = {asin_sig[a]: set(k) for a, k in sets.items()}
    sigs = sorted(by_sig)
    overlaps = [(x, y, len(by_sig[x] & by_sig[y])) for i, x in enumerate(sigs) for y in sigs[i + 1:]
                if by_sig[x] & by_sig[y]]
    root_of, edges = family_components(sizes, overlaps, threshold)
    return assemble_families(asin_sig, sizes, root_of, edges, DEFAULT_RULES.weak_support_max)


def test_ac14_family_components_threshold():
    fams = {f.asins: f for f in _families(_ac04_sets(), 0.5)}
    assert set(fams) == {("A", "B", "C"), ("D",), ("E",), ("F", "G")}
    assert fams[("A", "B", "C")].family_id == family_id("A") == "rf_" + hashlib.sha256(b"A").hexdigest()[:16]
    assert [round(e.jaccard, 4) for e in fams[("A", "B", "C")].edges] == [0.6]
    assert fams[("F", "G")].weak_support and not fams[("A", "B", "C")].weak_support
    assert fams[("D",)].max_review_count == 3 and not fams[("D",)].weak_support
    assert {f.asins for f in _families(_ac04_sets(), 0.7)} == {("A", "B"), ("C",), ("D",), ("E",), ("F", "G")}


def test_ac14_jaccard_edges():
    assert jaccard(6, 10, 6) == 0.6 and jaccard(0, 0, 0) == 0.0 and jaccard(1, 1, 1) == 1.0
    single = _families({"X": _keys("x", 1)}, 0.5)                  # 单条评论的家族
    assert [(f.asins, f.weak_support, f.max_review_count) for f in single] == [(("X",), True, 1)]


# ---------------------------------------------------------------- AC14：层次、代表记录与 helpfulness（R14–R19）

def _c(no, asin, help_raw="1/2", profile="Ann", status="ok", hv=1, tv=2) -> Copy:
    return Copy(no, asin, help_raw, profile, status, hv, tv)


def test_ac14_assign_levels():
    copies = [_c(9, "A"), _c(3, "A"), _c(5, "A", help_raw="3/4", hv=3, tv=4), _c(7, "B", profile="Ann B"),
              _c(8, "B"), _c(11, "B", profile="Ann B")]
    assert assign_levels(copies) == [
        (3, "representative", []),
        (5, "D2_same_asin_variant", ["helpfulness"]),
        (7, "D3_family_copy", ["profile_name"]),                 # B 的首条：相对代表的差异
        (8, "D2_same_asin_variant", ["profile_name"]),           # 同 ASIN 其余记录与 B 的首条比较
        (9, "D1_exact_same_asin", []),
        (11, "D1_exact_same_asin", []),
    ]
    assert assign_levels([_c(1, "A")]) == [(1, "representative", [])]


def test_ac14_pick_helpfulness():
    assert pick_helpfulness([_c(1, "A", "1/2", hv=1, tv=2), _c(2, "A", "3/4", hv=3, tv=4)]) == (3, 4, 2)
    assert pick_helpfulness([_c(4, "A", hv=1, tv=4), _c(2, "B", hv=2, tv=4)]) == (2, 4, 2)          # 并列取较小 record_no
    assert pick_helpfulness([_c(1, "A", "0/0", status="zero_zero", hv=0, tv=0)]) == (0, 0, 1)
    assert pick_helpfulness([_c(1, "A", "8/1", status="numerator_exceeds_denominator", hv=None, tv=None),
                             _c(2, "A", None, status="missing", hv=None, tv=None)]) == (None, None, None)


def test_ac14_split_collisions_and_ids():
    raw1, raw2 = ("U1", D, Decimal("5.0"), "S", "text one"), ("U1", D, Decimal("5"), "S", "text two")
    assert split_collisions([(4, raw2), (2, raw1), (3, ("U1", D, Decimal("5"), "S", "text one"))]) == [[2, 3], [4]]
    assert split_collisions([(1, raw1)]) == [[1]]
    h = content_hash(*raw1)
    lid = logical_review_id(h, "rf_0123456789abcdef")
    assert lid == "lr_" + hashlib.sha256(f"{h.hex()}|rf_0123456789abcdef".encode()).hexdigest()[:20]
    assert lid == logical_review_id(h, "rf_0123456789abcdef") and lid.startswith("lr_") and len(lid) == 23
    assert logical_review_id(h, "rf_0123456789abcdef", 1) != lid
    assert logical_review_id(h, "rf_fedcba9876543210") != lid


# ---------------------------------------------------------------- AC14：词集 Jaccard 与候选（R20、R21）

WORDS = "the film was a wonderful journey through history with great acting and beautiful sets".split()   # 14 个不同的词


def test_ac14_word_jaccard():
    assert word_set("It's GREAT, it's great!") == frozenset({"it's", "great"})
    assert word_set("café crème") == frozenset({"caf", "cr", "me"})             # 默认分词式只认 ASCII（R21）
    assert word_set("") == frozenset() and word_set(None) == frozenset()
    assert word_jaccard(frozenset(), frozenset()) == 0.0
    assert round(word_jaccard(word_set(" ".join(WORDS)), word_set(" ".join(WORDS[:-1]))), 4) == 0.9286


def test_ac14_near_duplicate_pairs():
    full, minus1 = " ".join(WORDS), " ".join(WORDS[:-1])                      # 13/14 = 0.9286
    six = " ".join(WORDS[:6] + ["x1", "x2"])                                 # 与 other 的 Jaccard 6/10 = 0.6
    other = " ".join(WORDS[:6] + ["y1", "y2"])
    md5 = lambda t: hashlib.md5(t.encode()).digest()                        # noqa: E731
    items = [("lr_b", md5(minus1), minus1), ("lr_a", md5(full), full), ("lr_c", md5(full), full)]
    assert near_duplicate_pairs(items, 0.9) == [("lr_a", "lr_b", 0.9286), ("lr_b", "lr_c", 0.9286)]   # 同正文的 a、c 不比较
    assert word_jaccard(word_set(six), word_set(other)) == 0.6
    assert near_duplicate_pairs([("lr_a", md5(six), six), ("lr_b", md5(other), other)], 0.9) == []


def test_ac14_cross_family_groups_and_candidate_id():
    h1, h2 = b"\x01" * 32, b"\x02" * 32
    items = [("lr_2", h1, "rf_b"), ("lr_1", h1, "rf_a"), ("lr_3", h2, "rf_a"), ("lr_4", h2, "rf_a")]
    assert cross_family_groups(items) == [["lr_1", "lr_2"]]
    assert candidate_id("near_duplicate", ["lr_b", "lr_a"]) == candidate_id("near_duplicate", ["lr_a", "lr_b"])
    assert candidate_id("near_duplicate", ["lr_a", "lr_b"]) != candidate_id("cross_family_same_content", ["lr_a", "lr_b"])


# ---------------------------------------------------------------- 阶段键（R34 的纯函数部分）

def test_stage_keys_nest_and_change_only_downstream():
    i = RunInputs(extraction_id="e" * 64)
    other = DEFAULT_RULES.with_version("t8x", family_jaccard=0.7)
    clean, family, dedup = (i.full_stage_key(s) for s in ("review_clean", "review_family", "review_dedup"))
    assert family.startswith(clean + "|") and dedup.startswith(family + "|")
    assert i.full_stage_key("review_clean", other) == clean
    assert i.full_stage_key("review_family", other) != family and i.full_stage_key("review_dedup", other) != dedup
    assert short_key("review_clean", clean).startswith("c") and len(short_key("review_clean", clean)) == 16
    assert i.key() == "e" * 64 + "|t3r2|cv0003|t8r1"


# ---------------------------------------------------------------- 统计组与敏感性（R29、R31 的纯函数部分）

def test_ac08_stat_group_components_and_ids():
    from etl_task8.rules import components, stat_group_id
    root = components(["m3", "m1", "m2", "m5"], [("m2", "m1"), ("m4", "m3")])
    assert root == {"m1": "m1", "m2": "m1", "m3": "m3", "m4": "m3", "m5": "m5"}   # 端点不在节点里时一并加入
    assert stat_group_id("m1") == "sg_" + hashlib.sha256(b"m1").hexdigest()[:16]


def test_ac08_sensitivity_keeps_earliest_of_each_near_duplicate_component():
    from etl_task8.rules import sensitivity
    items = {"a": (Decimal(5), date(2007, 1, 1)), "b": (Decimal(3), date(2007, 1, 1)),
             "c": (Decimal(1), date(2007, 1, 3)), "d": (Decimal(4), date(2007, 1, 2))}
    assert sensitivity(items, [("a", "c"), ("c", "d")]) == (2, Decimal(4), 2)     # {a,c,d} 只留最早的 a：(5+3)/2
    assert sensitivity(items, [("a", "x")]) == (4, Decimal("3.25"), 0)           # 端点不在范围里的对忽略
    assert sensitivity({}, []) == (0, None, 0)
