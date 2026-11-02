"""任务 7 规则层的纯函数（spec R13、R15–R17、R20–R21、R29–R30、R33、R34–R35、R39、R42、R45 的规则层部分，AC16；AC20 的写语句静态检查）。
预期值手写，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from etl_task4.normalize import title_key
from etl_task7.rules import (CONFLICT_TYPES, CONTROLLED_GENRES, DEFAULT_RULES, DIFF_CLASSES, PageRuntime, PageTitle,
                             RuleSet, YearInputs, choose_billing_order, choose_page_title, classify_genre_term,
                             classify_runtime_diff, classify_shared_pair, clean_genre_term, clean_page_display_title,
                             decide_halt, decide_movie_rating, decide_publisher, decide_release_year,
                             decide_runtime_without_imdb, discs_from_packaging, has_pal, imdb_confidence,
                             normalize_mpaa, pal_runtime, publisher_root, publishers_compatible, ruleset,
                             runtime_signal_lit, signal_tokens, split_multi_person, title_match_class, title_signal,
                             titles_compatible, worst_diff_class)

R = DEFAULT_RULES
ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 规则集与签名（R50–R51 的规则层部分）

def test_ac16_ruleset_registered_and_signatures_are_independent():
    assert ruleset("t7r1") is R and R.version == "t7r1"
    with pytest.raises(ValueError, match="未登记"):
        ruleset("t7r99")
    assert (R.conf_t4_link, R.conf_t5_hint, R.conf_t6_cooccur) == (0.95, 0.80, 0.75)      # R14
    assert R.reliability("imdb_t5_hint") == 0.80
    # 只改片长容差：只有 scalar 的签名变化；改发行商后缀：只有 edition 的签名变化；改信号阈值：只有 link 变化
    base = {g: R.signature(g) for g in ("edition", "link", "scalar", "multivalue")}
    assert len(set(base.values())) == 4
    tol = R.with_version("t7rx", runtime_tol_small=3)
    assert tol.signature("scalar") != base["scalar"] and tol.signature("edition") == base["edition"]
    assert tol.signature("link") == base["link"] and tol.signature("multivalue") == base["multivalue"]
    pub = R.with_version("t7ry", publisher_suffixes=("home video",))
    assert pub.signature("edition") != base["edition"] and pub.signature("scalar") == base["scalar"]
    sig = R.with_version("t7rz", runtime_gap=20)
    assert sig.signature("link") != base["link"] and sig.signature("edition") == base["edition"]
    assert R.thresholds()["runtime_gap"] == 15 and "version" not in R.thresholds()


def test_ac16_conflict_types_match_sql_check_constraint():
    """R13：冲突类型闭集与 sql/601 两张选择表的 CHECK 完全一致。"""
    text = (ROOT / "sql" / "601_task7_schema.sql").read_text(encoding="utf-8")
    blocks = re.findall(r"conflict_type\s+text NOT NULL CHECK \(conflict_type IN \((.*?)\)\)", text, re.S)
    assert len(blocks) == 2
    for block in blocks:
        assert tuple(re.findall(r"'(\w+)'", block)) == CONFLICT_TYPES
    assert len(CONFLICT_TYPES) == 16 and len(set(CONFLICT_TYPES)) == 16


# ---------------------------------------------------------------- 标题清洁化与归一（R20–R21）

@pytest.mark.parametrize("raw, clean", [
    ("Seven Samurai (Criterion Collection) [DVD]", "Seven Samurai (Criterion Collection)"),   # 品牌括号保留
    ("The Kid (1921) [DVD]", "The Kid"),
    ("The Kid [VHS]", "The Kid"),
    ("Emma (1996)", "Emma"),                                                                  # 单实词 + 括号年份
    ("Ben-Hur (Widescreen Edition) [DVD]", "Ben-Hur"),
    ("Gladiator (Full Screen)", "Gladiator"),
    ("Heat (Collector's Edition)", "Heat"),
    ("Zorro (Bilingual)", "Zorro"),
    ("Show Vol. 2 (Collector's Edition) [DVD]", "Show Vol. 2"),                               # 卷号保留
    ("Star Trek: The Motion Picture [Blu-ray]", "Star Trek: The Motion Picture"),             # 副标题保留
    ("Departed, The", "Departed, The"),
    ("[VHS]", "[VHS]"),                                                                       # 全被去掉时保留原值，不产生空标题
])
def test_ac16_clean_page_display_title(raw, clean):
    assert clean_page_display_title(raw) == clean


@pytest.mark.parametrize("key, tokens", [
    ("emma vhs", ["emma"]),                      # 载体词
    ("departed the", ["departed"]),              # 尾部冠词（“Departed, The”）
    ("the departed", ["departed"]),
    ("matrix dvd", ["matrix"]),
    ("matrix blu ray", ["matrix"]),
    ("matrix hd dvd", ["matrix"]),
    ("dvd", ["dvd"]),                            # 只剩载体词时保留原词
    ("the", ["the"]),
    ("lord of the rings", ["lord", "of", "the", "rings"]),
])
def test_ac16_signal_tokens_strip_carriers_and_edge_articles(key, tokens):
    assert signal_tokens(key) == tokens


@pytest.mark.parametrize("page, imdb, state", [
    (["emma vhs"], ["emma"], "ok"),                                  # L8：载体词不亮 T
    (["departed the"], ["departed"], "ok"),                          # L8：冠词后置不亮 T
    (["star trek"], ["star trek the motion picture"], "sub"),        # 双向实词子集（≥2 实词）
    (["red shadow"], ["crimson ninja"], "bad"),
    (["wallace"], ["wallace and gromit"], "bad"),                    # 单实词不是子集（假阳性保护）
    (["wallace", "red shadow"], ["wallace and gromit", "red shadow"], "ok"),   # 任一对相等即 ok
    ([], ["emma"], "na"),
    (["emma"], [], "na"),
])
def test_ac16_title_signal(page, imdb, state):
    assert title_signal(page, imdb) == state


def test_ac16_title_match_class_same_alias_other():
    k = lambda t: title_key(t)  # noqa: E731
    assert title_match_class("Seven Samurai", "Seven Samurai", "Shichinin no samurai", []) == "same"
    assert title_match_class("Shichinin no samurai", "Seven Samurai", "Shichinin no samurai", []) == "alias"
    assert title_match_class("Los siete samurais", "Seven Samurai", None, ["Los siete samurais"]) == "alias"
    assert title_match_class("Seven Samurai (Criterion Collection)", "Seven Samurai", None, []) == "alias"   # 实词子集
    assert title_match_class("Wrong Trousers", "Curse of the Were-Rabbit", None, []) == "other"
    assert title_match_class("Wallace", "Wallace and Gromit", None, []) == "other"                    # 单实词不算子集
    assert k("Seven Samurai") == "seven samurai"


def test_ac16_title_match_class_uses_task4_normalized_key():
    """R20：任务 4 规范键命中 IMDb primary、original 或 aka 时至少为 alias；same 的条件不放宽；单实词子集仍不算。"""
    raw = "300 Limited Collectors Edition and Digital Copy"                 # 清洁标题保留了没见过的括号说明
    assert title_match_class(raw, "300", None, []) == "other"                # 只看清洁标题：无关
    assert title_match_class(raw, "300", None, [], page_key="300") == "alias"
    assert title_match_class(raw, "Three Hundred", "300", [], page_key="300") == "alias"
    assert title_match_class(raw, "Three Hundred", None, ["300"], page_key="300") == "alias"
    assert title_match_class("Seven Samurai (Criterion Collection)", "Seven Samurai", None, [],
                             page_key="seven samurai") == "alias"            # AC03 M1 不变
    assert title_match_class("Seven Samurai", "Seven Samurai", None, [], page_key="seven samurai") == "same"
    assert title_match_class(raw, "Other Film", None, [], page_key="300") == "other"
    assert title_match_class("Wallace (X)", "Wallace and Gromit", None, [], page_key="wallace") == "other"


@pytest.mark.parametrize("a, b, ok", [
    ("The Kid", "Kid, The", False),                      # 冠词位置不同不属于规范键一致（键含 the）
    ("The Kid", "Kid", True),                            # 规范键去前导冠词后相同
    ("Star Trek", "Star Trek: The Motion Picture", True),    # 互为副标题
    ("Star Trek: Wrath of Khan", "Star Trek: The Motion Picture", False),   # 副标题各不相同
    ("Rocky", "Rocky II", False),                        # 续集编号不同
    ("Show Vol. 1", "Show", True),                       # 卷号包含
    ("Red Shadow", "Crimson Ninja", False),
])
def test_ac16_titles_compatible(a, b, ok):
    assert titles_compatible(a, b) is ok


def test_ac16_choose_page_title_frequency_tie_and_distinct():
    t = lambda asin, clean: PageTitle(asin, clean)  # noqa: E731
    assert choose_page_title([t("B2", "The Kid"), t("B1", "The Kid")]) == ("The Kid", "same")
    # 频次最高的清洁写法胜出
    best, kind = choose_page_title([t("A1", "Star Trek"), t("A2", "Star Trek: The Motion Picture"),
                                    t("A3", "Star Trek: The Motion Picture")])
    assert (best, kind) == ("Star Trek: The Motion Picture", "alias")
    # 平票取最小 ASIN 的写法
    best, _ = choose_page_title([t("A2", "Star Trek"), t("A1", "Star Trek: The Motion Picture")])
    assert best == "Star Trek: The Motion Picture"
    assert choose_page_title([t("B1", "Red Shadow"), t("B2", "Crimson Ninja")]) == (None, "distinct")
    assert choose_page_title([]) == (None, "none")


# ---------------------------------------------------------------- 内容分级（R33）

@pytest.mark.parametrize("raw, rating", [
    ("PG", "PG"), ("PG (Parental Guidance Suggested)", "PG"), ("pg-13", "PG-13"), ("PG-13 (Parents Strongly Cautioned)", "PG-13"),
    ("R (Restricted)", "R"), ("G (General Audience)", "G"), ("NC-17 (Adults Only)", "NC-17"),
    ("X (Mature Audiences Only)", "X"), ("NR", "NR/Unrated"), ("NR (Not Rated)", "NR/Unrated"),
    ("Unrated", "NR/Unrated"), ("Unrated (Not Rated)", "NR/Unrated"), ("Not Rated", "NR/Unrated"),
    ("  R  ", "R"),
    ("Approved", None), ("TV-14", None), ("Rated Z", None), ("", None), (None, None),
])
def test_ac16_normalize_mpaa(raw, rating):
    assert normalize_mpaa(raw) == rating


def test_ac16_decide_movie_rating():
    d = decide_movie_rating(["R", "NR/Unrated", "NR/Unrated"])
    assert (d.status, d.value, d.conflict_type) == ("resolved", "R", "unrated_vs_rated")
    d = decide_movie_rating(["PG", "PG"])
    assert (d.status, d.value, d.conflict_type) == ("resolved", "PG", "none")
    d = decide_movie_rating(["NR/Unrated"])
    assert (d.status, d.value) == ("resolved", "NR/Unrated")
    d = decide_movie_rating(["G", "PG"])
    assert (d.status, d.value, d.conflict_type) == ("conflict_unresolved", None, "intra_source_tie_unresolved")
    d = decide_movie_rating(["G", "PG", "NR/Unrated"])           # 两种具体分级，未评级不能把冲突“救”回来
    assert d.status == "conflict_unresolved"


# ---------------------------------------------------------------- 片长（R29–R32）

def test_ac16_pal_runtime_and_has_pal():
    assert pal_runtime(100) == 96 and pal_runtime(90) == 86 and pal_runtime(105) == 101 and pal_runtime(95) == 91
    assert has_pal(["NTSC", "PAL"]) and has_pal(["pal"]) and has_pal(["PAL/SECAM"])
    assert not has_pal(["NTSC"]) and not has_pal([]) and not has_pal(["palace"])


@pytest.mark.parametrize("page, imdb, kw, cls, formula", [
    (100, 100, {}, "exact", False),
    (101, 100, {}, "tolerance_1_2", False),
    (98, 100, {}, "tolerance_1_2", False),
    (96, 100, {"has_pal_evidence": True}, "pal_evidence", True),      # 同时有证据与公式吻合
    (96, 100, {}, "variance_3_5", True),                              # 只有公式吻合：不归为 PAL，记 pal_formula_match
    (104, 100, {"has_pal_evidence": True}, "variance_3_5", False),    # 只有 PAL 断言、公式不吻合
    (105, 100, {}, "variance_3_5", False),
    (125, 100, {"cut_types": ["directors cut"]}, "cut_or_multidisc", False),
    (150, 100, {"discs": 2}, "cut_or_multidisc", False),              # 多碟且 ≥1.5 倍
    (150, 100, {"discs": 1}, "unexplained_gt15", False),
    (106, 100, {}, "unexplained_6_15", False),
    (115, 100, {}, "unexplained_6_15", False),
    (116, 100, {}, "unexplained_gt15", False),
    (135, 90, {}, "unexplained_gt15", False),
    (103, 100, {"cut_types": ["unrated"]}, "variance_3_5", False),    # 差值 ≤5 时剪辑版标记不改类别
])
def test_ac16_classify_runtime_diff(page, imdb, kw, cls, formula):
    assert classify_runtime_diff(page, imdb, **kw) == (cls, formula)
    assert cls in DIFF_CLASSES


def test_ac16_runtime_signal_and_worst_class():
    assert runtime_signal_lit(116, 100) and not runtime_signal_lit(115, 100)
    assert not runtime_signal_lit(135, 100, cut_types=["extended"])           # 剪辑版不亮
    assert not runtime_signal_lit(160, 100, discs=2)                           # 多碟花絮版不亮
    assert runtime_signal_lit(160, 100, discs=1)
    assert worst_diff_class(["exact", "tolerance_1_2", "cut_or_multidisc", "pal_evidence"]) == "cut_or_multidisc"
    assert worst_diff_class(["exact", "unexplained_6_15", "unexplained_gt15"]) == "unexplained_gt15"
    assert worst_diff_class([]) is None


def test_ac16_decide_runtime_without_imdb():
    d = decide_runtime_without_imdb([PageRuntime("A", 92), PageRuntime("B", 94)])        # 极差 2：平票取较小值
    assert (d.status, d.value, d.conflict_type) == ("resolved", 92, "minor_tolerance")
    d = decide_runtime_without_imdb([PageRuntime("A", 92), PageRuntime("B", 94), PageRuntime("C", 94)])
    assert d.value == 94                                                                  # 众数
    d = decide_runtime_without_imdb([PageRuntime("A", 85), PageRuntime("B", 118)])       # R4：无标记，无法区分
    assert (d.status, d.value, d.conflict_type) == ("conflict_unresolved", None, "intra_source_tie_unresolved")
    d = decide_runtime_without_imdb([PageRuntime("A", 100), PageRuntime("B", 100), PageRuntime("C", 130, True)])
    assert (d.status, d.value, d.conflict_type) == ("resolved", 100, "framerate_or_edition")
    d = decide_runtime_without_imdb([PageRuntime("A", 100)])
    assert (d.value, d.conflict_type) == (100, "none")
    d = decide_runtime_without_imdb([])
    assert d.status == "missing"


# ---------------------------------------------------------------- 上映年份决策表与关联评估（R17、R24–R27）

@pytest.mark.parametrize("inp, status, year, ctype, rule, issue", [
    # Y1 强关联：IMDb 1985、页面 1986
    (YearInputs(1985, page_years=(1986,)), "resolved", 1985, "minor_tolerance", "YR2", None),
    # Y2 强关联：1954 与 1998，导演兼容
    (YearInputs(1954, page_years=(1998,), sig_year="bad", sig_director="ok"), "resolved", 1954,
     "cross_source_override", "YR4", "cross_source_override"),
    # Y3 强关联：Y、D、RT 同时成立
    (YearInputs(1954, page_years=(1998,), sig_year="bad", sig_director="bad", sig_runtime="bad"),
     "conflict_unresolved", None, "severe_mismatch_unresolved", "YR3", "severe_mismatch_unresolved"),
    # Y4 弱匹配已熔断
    (YearInputs(1991, weak=True, halted=True, page_years=(2010,), sig_year="bad"),
     "conflict_unresolved", None, "cross_source_mismatch_halt", "YR7", None),
    # Y5 弱匹配未熔断，导演兼容
    (YearInputs(1991, weak=True, page_years=(2010,), sig_year="bad", sig_director="ok"), "resolved", 1991,
     "cross_source_override", "YR5", "cross_source_override"),
    # Y5 的另一支：存在差 ≤15 分钟的片长
    (YearInputs(1991, weak=True, page_years=(2010,), sig_year="bad", close_runtime=True), "resolved", 1991,
     "cross_source_override", "YR5", "cross_source_override"),
    # Y6 弱匹配，年份差 5，无导演、无片长
    (YearInputs(1991, weak=True, page_years=(1996,), sig_year="bad"), "conflict_unresolved", None,
     "severe_mismatch_unresolved", "YR6", "severe_mismatch_unresolved"),
    # Y7 无 IMDb：页面 1971 与 1975
    (YearInputs(None, page_years=(1971, 1975)), "conflict_unresolved", None, "intra_source_tie_unresolved", "YR9", None),
    # Y8 无任何候选
    (YearInputs(None), "missing", None, "none", "YR10", None),
    # 一致、单一来源、只有 IMDb
    (YearInputs(1999, page_years=(1999, 1999)), "resolved", 1999, "none", "YR1", None),
    (YearInputs(1999), "resolved", 1999, "none", "YR1", None),
    (YearInputs(None, page_years=(1971,)), "resolved", 1971, "none", "YR8", None),
    # 强关联：只是评论或发售日证据比 IMDb 早 2 年以上（没有页面年份）：Y 亮，导演兼容 → 采用 IMDb
    (YearInputs(2005, sig_year="bad", sig_director="ok"), "resolved", 2005, "cross_source_override", "YR4",
     "cross_source_override"),
])
def test_ac16_decide_release_year(inp, status, year, ctype, rule, issue):
    d = decide_release_year(inp)
    assert (d.status, d.year, d.conflict_type, d.rule, d.issue) == (status, year, ctype, rule, issue)
    assert ctype in CONFLICT_TYPES


@pytest.mark.parametrize("weak, y, rt, d, t, halted", [
    (True, True, True, False, False, True),     # L1：Y ∧ RT
    (True, True, False, True, False, True),     # L2：Y ∧ D
    (True, False, True, False, True, True),     # L3：T ∧ RT
    (True, True, False, False, True, True),     # Y ∧ T
    (True, False, False, True, True, True),     # T ∧ D
    (True, False, False, True, False, False),   # L4 型：只亮 D
    (True, False, True, False, False, False),   # L5：只亮 RT
    (True, False, True, True, False, False),    # RT ∧ D 没有 Y 或 T：不熔断
    (True, True, False, False, False, False),   # 只亮 Y
    (True, False, False, False, True, False),   # 只亮 T
    (False, True, True, True, True, False),     # L6：强关联永不熔断
])
def test_ac16_decide_halt_formula(weak, y, rt, d, t, halted):
    assert decide_halt(weak, y, rt, d, t)[0] is halted
    assert (decide_halt(weak, y, rt, d, t)[1] is not None) is halted


def test_ac16_decide_halt_reason_lists_lit_signals():
    assert decide_halt(True, True, True, False, False) == (True, "Y+RT")
    assert decide_halt(True, False, True, True, True) == (True, "RT+D+T")


def test_ac16_classify_shared_pair():
    assert classify_shared_pair(True, False, "ok", "ok", "ok", "ok") == "false_positive_link"
    assert classify_shared_pair(False, True, "ok", "ok", "ok", "ok") == "false_positive_link"
    assert classify_shared_pair(False, False, "ok", "sub", "ok", "na") == "likely_duplicate_movie"
    assert classify_shared_pair(False, False, "ok", "ok", "bad", "ok") == "undetermined"      # 标题相等但导演不兼容
    assert classify_shared_pair(False, False, "bad", "ok", "ok", "ok") == "undetermined"


def test_ac16_imdb_confidence():
    assert imdb_confidence(0.95, R) == 0.95
    assert imdb_confidence(0.80, R, single_signal=True) == 0.70
    assert imdb_confidence(0.95, R, override=True) == 0.70
    assert imdb_confidence(0.75, R, single_signal=True, override=True) == 0.65


# ---------------------------------------------------------------- 发行商、碟数、多人串、署名顺序

def test_ac16_publisher_roots_and_compatibility():
    assert publisher_root("Warner Home Video") == "warner"
    assert publisher_root("Warner Bros.") == "warner bros"
    assert publisher_root("Universal Studios") == "universal"
    assert publisher_root("Home Video") == "home video"                      # 全是后缀时保留
    assert publishers_compatible("Warner Archive", "Warner") and publishers_compatible("A & E Entertainment", "A&E")
    assert not publishers_compatible("MGM", "Warner") and not publishers_compatible("Sony Pictures", "Universal Studios")


def test_ac16_decide_publisher():
    d = decide_publisher([("Warner Home Video", "detail_bullets"), ("Warner Bros.", "product_overview")])
    assert (d.value, d.conflict_type) == ("Warner Home Video", "formatting_or_alias") and d.superseded == (1,)
    d = decide_publisher([("Sony Pictures", "detail_bullets"), ("Universal Studios", "product_overview")])
    assert (d.value, d.conflict_type, d.rejected) == ("Sony Pictures", "intra_asin_publisher_conflict", (1,))
    d = decide_publisher([("Universal Studios", "product_overview"), ("Sony Pictures", "detail_bullets")])
    assert d.value == "Sony Pictures"                                        # 区域优先级，与出现顺序无关
    d = decide_publisher([("Warner Studios", "detail_bullets"), ("Warner Home Video", "product_overview"),
                          ("Warner Studios", "product_overview")])
    assert (d.value, d.conflict_type) == ("Warner Home Video", "formatting_or_alias")
    d = decide_publisher([("The Criterion Collection", "detail_bullets"), ("Criterion Collection", "product_overview")])
    assert (d.value, d.conflict_type) == ("The Criterion Collection", "formatting_or_alias")    # design 第 9 节实例
    d = decide_publisher([("Fox", "detail_bullets"), ("Fox", "product_overview")])
    assert (d.conflict_type, d.rule) == ("none", "PB1")
    assert decide_publisher([]).status == "missing"


@pytest.mark.parametrize("text, n", [
    ("2-Disc Set", 2), ("Two-Disc Special Edition", 2), ("3 Discs", 3), ("Single-Disc Edition", 1),
    ("Collector's Edition", None), ("", None), (None, None), ("Four Disc Box Set", 4),
])
def test_ac16_discs_from_packaging(text, n):
    assert discs_from_packaging(text) == n


@pytest.mark.parametrize("raw, parts", [
    ("Tomas Guiterrez Alea; Carlos Garcia Agraz", ["Tomas Guiterrez Alea", "Carlos Garcia Agraz"]),
    ("Tomas Guiterrez Alea; Carlos Garcia Agraz; and Jose Luis Garcia Agraz",
     ["Tomas Guiterrez Alea", "Carlos Garcia Agraz", "Jose Luis Garcia Agraz"]),
    ("Joe Smith and Jane Doe", ["Joe Smith", "Jane Doe"]),
    ("Joe Smith & Jane Doe", ["Joe Smith", "Jane Doe"]),
    ("Black and Blue Productions", ["Black", "Blue Productions"]),            # 拆分只是比较用，由对齐步骤决定是否取代原行
    ("John Doe", ["John Doe"]),
    ("Anderson", ["Anderson"]),
    ("Bogart, Humphrey", ["Bogart, Humphrey"]),                               # 逗号不属于多人串分隔符
])
def test_ac16_split_multi_person(raw, parts):
    assert split_multi_person(raw) == parts


def test_ac16_choose_billing_order():
    assert choose_billing_order([1, 2, 1]) == (1, {1: 2, 2: 1})              # 多数版本印刷一致
    assert choose_billing_order([2, 1]) == (1, {1: 1, 2: 1})                  # 平票取最小序号
    assert choose_billing_order([3]) == (3, {3: 1})


# ---------------------------------------------------------------- 类型受控词表与词面处置（R34–R35）

def test_ac16_controlled_vocabulary_has_30_words():
    assert len(CONTROLLED_GENRES) == 30 and len(set(CONTROLLED_GENRES)) == 30
    assert {"Anime", "Foreign", "Performing Arts", "LGBT", "Silent", "Sci-Fi", "Film-Noir"} <= set(CONTROLLED_GENRES)


@pytest.mark.parametrize("raw, term, disposition, genres", [
    ("Action & Adventure", "Action & Adventure", "mapped", ("Action", "Adventure")),
    ("Science Fiction & Fantasy see more", "Science Fiction & Fantasy", "mapped", ("Sci-Fi", "Fantasy")),
    ("faith_&_spirituality", "faith & spirituality", "unmapped", ()),
    ("Anime & Manga", "Anime & Manga", "mapped", ("Animation", "Anime")),
    ("Movies & TV", "Movies & TV", "noise", ()),
    ("DVD", "DVD", "noise", ()),
    ("3-6 Years", "3-6 Years", "noise", ()),
    ("Romantic Comedy", "Romantic Comedy", "mapped", ("Comedy", "Romance")),
    ("Kids & Family", "Kids & Family", "mapped", ("Family",)),
    ("Musicals & Performing Arts", "Musicals & Performing Arts", "mapped", ("Musical", "Performing Arts")),
    ("Spielberg", "Spielberg", "unmapped", ()),
    ("Rock", "Rock", "mapped", ("Music",)),
    ("Sci-Fi", "Sci-Fi", "mapped", ("Sci-Fi",)),
    ("Hardcore & Punk", "Hardcore & Punk", "mapped", ("Music",)),            # T7-08：开发池证据追加的音乐子类
    ("Album-Oriented Rock (AOR)", "Album-Oriented Rock (AOR)", "mapped", ("Music",)),
    ("comedy romance", "comedy romance", "mapped", ("Comedy", "Romance")),
    ("Sphygmomanometers", "Sphygmomanometers", "noise", ()),                  # 非影视零售目录词
    ("3-D", "3-D", "noise", ()),
    ("Halloween", "Halloween", "unmapped", ()),                               # 影视语义不明确的仍不猜测
    ("Soul", "Soul", "unmapped", ()),
])
def test_ac16_classify_genre_term(raw, term, disposition, genres):
    assert classify_genre_term(raw) == (term, disposition, genres)
    assert set(genres) <= set(CONTROLLED_GENRES)


def test_ac16_clean_genre_term():
    assert clean_genre_term("  Drama see more ") == "Drama"
    assert clean_genre_term("foreign_films") == "foreign films"


# ---------------------------------------------------------------- AC20：写语句目标只在 task7

_WRITE = [re.compile(p, re.I) for p in (
    r"INSERT\s+INTO\s+([\w.]+)", r"UPDATE\s+([\w.]+)\s+SET", r"DELETE\s+FROM\s+([\w.]+)",
    r"TRUNCATE\s+(?:TABLE\s+)?([\w.]+)", r"\bCOPY\s+([\w.]+)\s*\(", r"copy_rows\(\s*conn\s*,\s*\"([\w.]+)\"",
    r"ALTER\s+TABLE\s+([\w.]+)", r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|SEQUENCE)\s+([\w.]+)",
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+\w+\s+ON\s+([\w.]+)", r"DROP\s+(?:TABLE|SCHEMA)\s+(?:IF\s+EXISTS\s+)?([\w.]+)")]


def _targets(text: str) -> set[str]:
    return {m.group(1) for pat in _WRITE for m in pat.finditer(text)}


def test_ac20_write_statement_targets_only_in_task7():
    """R56：src/etl_task7 的写语句目标全部在 schema task7；迁移 601 只建 task7 对象，并只向 public.schema_migration 登记。"""
    seen: set[str] = set()
    for path in sorted((ROOT / "src" / "etl_task7").glob("*.py")):
        targets = _targets(path.read_text(encoding="utf-8"))
        seen |= targets
        assert all(t.startswith("task7.") for t in targets), (path.name, sorted(targets))
    assert {"task7.run", "task7.field_candidate", "task7.edition_stage_result", "task7.link_assessment",
            "task7.movie_scalar_stage_result"} <= seen              # 检查本身有效：确实扫到了写入目标
    sql = _targets((ROOT / "sql" / "601_task7_schema.sql").read_text(encoding="utf-8"))
    assert sql and all(t.startswith("task7.") or t == "public.schema_migration" for t in sql), sorted(sql)
    assert "public.schema_migration" in sql
    # 上游表只以 REFERENCES 出现，不出现在写语句里
    text = (ROOT / "sql" / "601_task7_schema.sql").read_text(encoding="utf-8")
    assert re.search(r"REFERENCES\s+task6\.release_version", text)
    assert not re.search(r"(?:ALTER|CREATE)\s+TABLE\s+(?:public|task[1-6]|imdb)\.", text)
