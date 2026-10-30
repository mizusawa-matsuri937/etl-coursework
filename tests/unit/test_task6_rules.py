"""任务 6 规则层的纯函数（spec R09–R16、R21–R23 的规则层部分，AC08 的择优与分歧）。预期值手写。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py
"""

from __future__ import annotations

import pytest

from etl_task6.rules import (CandBrief, DEFAULT_RULES, MediaEvidence, ReviewEvidence, check_media_evidence,
                             check_review_evidence, choose_candidates, cooccur_number_guard, cooccur_title_ok,
                             content_words, evaluate_disagreement, evidence_confidence, merge_bounds, number_tokens,
                             ruleset, top_review_year)

R = DEFAULT_RULES


def _cands(*specs: tuple[str, str, int]) -> list[CandBrief]:
    """(source, source_key, year) → CandBrief，candidate_id 递增（ID 小的代表断言/序号小）。"""
    return [CandBrief(i + 1, s, k, y) for i, (s, k, y) in enumerate(specs)]


# ---------------------------------------------------------------- 择优与状态（R14–R15）

def test_ruleset_registered_and_unknown_version_fails():
    assert ruleset("t6r1") is R and R.version == "t6r1"
    assert (R.conf_t4_link, R.conf_t5_hint, R.conf_cooccur, R.conf_cooccur_tiebreak, R.conf_title_year) == \
        (0.95, 0.80, 0.75, 0.70, 0.60)
    assert (R.year_disagreement_tolerance, R.cooccur_director_points, R.cooccur_actor_points,
            R.cooccur_min_score) == (1, 2, 1, 2)
    assert (R.review_year_min_votes, R.review_year_min_share, R.b3_days, R.min_review_year) == (2, 0.60, 365, 1997)
    assert R.with_version("t6rx", b3_days=90).b3_days == 90 and R.b3_days == 365
    with pytest.raises(ValueError, match="未登记"):
        ruleset("t6r99")


def test_confidence_of_sources_and_tiebreak_flag():
    assert R.confidence_of("imdb_t4_link") == 0.95
    assert R.confidence_of("imdb_t6_cooccur", ()) == 0.75
    assert R.confidence_of("imdb_t6_cooccur", ("review_year_tiebreak",)) == 0.70
    with pytest.raises(ValueError):
        R.confidence_of("unknown_source")


def test_choose_follows_source_order():
    out = choose_candidates(_cands(("amazon_title_year", "A1:5", 1990), ("imdb_t4_link", "tt1", 1999)),
                            multiple_tconst_hint=False, cooccur_ambiguous=False)
    assert out.status == "verified" and out.adopted.value_year == 1999
    assert [c.candidate_id for c in out.considered] == [1, 2]        # 按候选 ID 排序
    out = choose_candidates(_cands(("imdb_t5_hint", "tt2", 1975), ("imdb_t6_cooccur", "tt3", 1962)),
                            multiple_tconst_hint=False, cooccur_ambiguous=False)
    assert out.status == "hint_matched" and out.adopted.value_year == 1975
    out = choose_candidates(_cands(("amazon_title_year", "A1:5", 1985), ("imdb_t6_cooccur", "tt3", 1962)),
                            multiple_tconst_hint=False, cooccur_ambiguous=False)
    assert out.status == "cooccur_matched" and out.adopted.value_year == 1962


def test_choose_title_year_needs_consensus_and_takes_smallest_id():
    out = choose_candidates(_cands(("amazon_title_year", "A1:7", 1985), ("amazon_title_year", "A2:8", 1985)),
                            multiple_tconst_hint=False, cooccur_ambiguous=False)
    assert out.status == "page_reported" and out.adopted.candidate_id == 1
    out = choose_candidates(_cands(("amazon_title_year", "A1:7", 1985), ("amazon_title_year", "A2:8", 1987)),
                            multiple_tconst_hint=False, cooccur_ambiguous=False)
    assert out.status == "missing" and out.missing_reason == "title_years_disagree"


def test_choose_missing_reasons_priority():
    plain = choose_candidates([], multiple_tconst_hint=False, cooccur_ambiguous=False)
    assert (plain.status, plain.missing_reason) == ("missing", "no_candidate")
    hint = choose_candidates([], multiple_tconst_hint=True, cooccur_ambiguous=True)
    assert hint.missing_reason == "hint_ambiguous"                   # 线索自相矛盾优先于共现不唯一
    cooc = choose_candidates([], multiple_tconst_hint=False, cooccur_ambiguous=True)
    assert cooc.missing_reason == "cooccur_ambiguous"
    title = choose_candidates(_cands(("amazon_title_year", "A1:7", 1985), ("amazon_title_year", "A2:8", 1987)),
                              multiple_tconst_hint=False, cooccur_ambiguous=False)
    assert title.missing_reason == "title_years_disagree"


# ---------------------------------------------------------------- 分歧（R16）

def test_disagreement_one_year_counted_and_beyond_tolerance_flagged():
    choice = choose_candidates(_cands(("imdb_t4_link", "tt1", 1990), ("amazon_title_year", "A1:5", 1991)),
                               multiple_tconst_hint=False, cooccur_ambiguous=False)
    dis = evaluate_disagreement(choice, R)
    assert dis.year_diff_1 == (2,) and not dis.year_disagreements and not dis.tconst_disagreement
    choice = choose_candidates(_cands(("imdb_t4_link", "tt1", 1990), ("amazon_title_year", "A1:5", 1994)),
                               multiple_tconst_hint=False, cooccur_ambiguous=False)
    dis = evaluate_disagreement(choice, R)
    assert dis.year_disagreements == ({"candidate_id": 2, "source": "amazon_title_year", "year": 1994, "diff": 4},)


def test_disagreement_tconst_between_hint_and_cooccur_only():
    choice = choose_candidates(_cands(("imdb_t5_hint", "tt1", 2001), ("imdb_t6_cooccur", "tt2", 1962)),
                               multiple_tconst_hint=False, cooccur_ambiguous=False)
    dis = evaluate_disagreement(choice, R)
    assert dis.tconst_disagreement == ("tt2",)                       # 采用 tt1，另一个 IMDb 来源的 tt2 记分歧
    choice = choose_candidates(_cands(("imdb_t4_link", "tt1", 1999), ("amazon_title_year", "A1:5", 1999)),
                               multiple_tconst_hint=False, cooccur_ambiguous=False)
    dis = evaluate_disagreement(choice, R)
    assert not dis.tconst_disagreement                               # 标题年份来源没有 tconst，不参与


# ---------------------------------------------------------------- 共现反查的标题与编号条件（R09）

def test_content_words_strip_stopwords_and_title_subset():
    assert content_words("treasure hunt island walt disney film classics vhs") == \
        {"treasure", "hunt", "island", "walt", "disney", "film", "classics"}
    assert cooccur_title_ok("treasure hunt island", "treasure hunt island walt disney film classics vhs")
    assert not cooccur_title_ok("treasure hunt island", "treasure hunt")   # IMDb 实词必须全部出现
    assert not cooccur_title_ok("vhs", "some movie vhs")                  # IMDb 侧全是停用词：实词为空，不通过


def test_ac21_single_word_title_key_and_min_length_guard():
    # 单实词标题键仅允许与同样只有 1 个实词的另一方标题键精确相等匹配
    assert cooccur_title_ok("legend", "the legend dvd")
    assert not cooccur_title_ok("legend", "new legend of shaolin")
    assert not cooccur_title_ok("wallace", "wallace and gromit the wrong trousers")
    assert not cooccur_title_ok("beatles", "beatles biggest secrets")
    assert not cooccur_title_ok("gambler", "kenny rogers the gambler the adventure continues")
    # ≥2 个实词且字符数 ≥6 的标题键按子集匹配
    assert cooccur_title_ok("puppet master 6", "puppet master 6 curse of the puppet master")
    # ≥2 个实词但实词总字符数 < 6 时拒绝子集匹配
    assert not cooccur_title_ok("go up", "go up the mountain")


def test_number_tokens_and_guard():
    assert number_tokens("land before saga 8") == {8}                    # 罗马数字已在标题键转成阿拉伯数字
    assert number_tokens("film 2001") == set()                           # 四位年份不算编号
    assert not cooccur_number_guard("land before saga", "land before saga 8")
    assert cooccur_number_guard("land before saga 8", "land before saga 8 vhs")
    assert cooccur_number_guard("rocky", "rocky 2001")                   # 两边都没有编号时通过


# ---------------------------------------------------------------- 评论年份提及的计票门槛（R10 消歧）

def test_top_review_year_thresholds():
    assert top_review_year({1950: 3, 1990: 1}, R) == 1950                # 3 票、75%、无并列
    assert top_review_year({1950: 1}, R) is None                         # 票数不足 2
    assert top_review_year({1950: 2, 1972: 2}, R) is None                # 并列最高
    assert top_review_year({1950: 3, 1972: 2}, R) == 1950                # 恰好 60% 且无并列
    assert top_review_year({1950: 2, 1972: 3, 1990: 1}, R) is None       # 最高票占比 50% 不足
    assert top_review_year({}, R) is None


# ---------------------------------------------------------------- 上界条件与合并（R21–R23）

def _review(**over) -> ReviewEvidence:
    base = dict(earliest_time="2004-06-01", earliest_year=2004, second_time="2005-01-01", review_count=5,
                products_review_pending=False, has_edition_product=True, adopted_imdb_year=None,
                review_time_min="1997-08-20", review_time_max="2012-10-25")
    return ReviewEvidence(**{**base, **over})


def test_review_evidence_checks_b1_to_b5():
    assert all(check_review_evidence(_review(), R).values())
    assert not check_review_evidence(_review(products_review_pending=True), R)["B1"]
    assert not check_review_evidence(_review(has_edition_product=False), R)["B2"]
    assert not check_review_evidence(_review(second_time=None), R)["B3"]
    assert not check_review_evidence(_review(second_time="2006-06-02"), R)["B3"]   # 超过 365 天
    assert not check_review_evidence(_review(adopted_imdb_year=2005), R)["B4"]     # 最早评论早于 IMDb 年份
    assert check_review_evidence(_review(adopted_imdb_year=2004), R)["B4"]
    assert not check_review_evidence(_review(earliest_time="1996-12-31", earliest_year=1996), R)["B5"]


def test_media_evidence_checks_m1_to_m4():
    base = dict(value_time="2004-06-01", value_year=2004, jan_first_suspect=False,
                products_review_pending=False, adopted_imdb_year=None)
    assert all(check_media_evidence(MediaEvidence(**base)).values())
    assert not check_media_evidence(MediaEvidence(**{**base, "products_review_pending": True}))["M1"]
    assert not check_media_evidence(MediaEvidence(**{**base, "adopted_imdb_year": 2005}))["M4"]
    assert check_media_evidence(MediaEvidence(**{**base, "adopted_imdb_year": 2004}))["M4"]


def test_merge_bounds_takes_earliest_by_latest_instant():
    chosen = merge_bounds([("review", "2005-02-01", "day"), ("media_release", "2004-06-01", "day")])
    assert chosen == ("media_release", "2004-06-01", "day")
    # year 精度按当年 12-31 的最晚时刻比较：2001-06-01（day）早于 2001（year）
    chosen = merge_bounds([("media_release", "2001-01-01", "year"), ("review", "2001-06-01", "day")])
    assert chosen == ("review", "2001-06-01", "day")
    assert merge_bounds([]) is None


def test_evidence_confidence_bands():
    assert evidence_confidence("media_release", {}, R) == 0.7
    assert evidence_confidence("media_release", {}, R, jan_first_suspect=True) == 0.6
    assert evidence_confidence("review", {}, R) == 0.6
    assert evidence_confidence("review", {}, R, review_dense=False) == 0.4
