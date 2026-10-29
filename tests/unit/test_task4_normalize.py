"""任务 4 标准化（spec AC03、AC04，及 AC06 的拆分部分）：标题键、说明分类与标题年份、IMDb 别名键、姓名键、片长、合集成员。
预期按 spec 手写。"""

from __future__ import annotations

import pytest

from etl_task4.normalize import (PLACEHOLDER_NAMES, classify_segment, genre_tags, name_keys, parse_people,
                                 release_date_iso, runtime_minutes, split_collection, split_title, strict_name,
                                 title_key)

# (原始标题, 标题键, 标题年份, 说明段 [(原文, 主类别)])
TITLE_CASES = [
    ("Casablanca (Ultimate Collector's Edition)", "casablanca", None,
     [("Ultimate Collector's Edition", "packaging")]),
    ("Casablanca [DVD]", "casablanca", None, [("DVD", "media")]),
    ("The Omen (2006)", "omen", 2006, [("2006", "year")]),
    ("Rocky II [VHS]", "rocky 2", None, [("VHS", "media")]),
    ("Cinderella (Two-Disc Diamond Edition Blu-ray/DVD Combo in DVD Packaging) (Spanish Version)", "cinderella",
     None, [("Two-Disc Diamond Edition Blu-ray/DVD Combo in DVD Packaging", "media"), ("Spanish Version", "language")]),
    ("Meet John Doe (UK PAL Region 0)", "meet john doe", None, [("UK PAL Region 0", "region")]),
    ("Pride & Prejudice", "pride and prejudice", None, []),
    ("Superbit™ Air Force One", "air force one", None, [("Superbit™", "brand")]),
]


@pytest.mark.parametrize("raw, key, year, descriptors", TITLE_CASES, ids=[c[0][:30] for c in TITLE_CASES])
def test_title_key_year_and_descriptors(raw, key, year, descriptors):
    parts = split_title(raw)
    assert parts.key == key and parts.title_year == year
    assert [(d.raw, d.category) for d in parts.descriptors] == descriptors
    assert parts.raw == raw                                        # 原始标题没有被改动


def test_title_same_work_different_editions_share_a_key():
    keys = {split_title(t).key for t in ("Casablanca", "Casablanca [DVD]", "Casablanca (Ultimate Collector's Edition)")}
    assert keys == {"casablanca"}
    assert split_title("Pride & Prejudice").key == split_title("Pride and Prejudice").key


def test_title_sequel_number_and_subtitle_stay_in_the_key():
    assert title_key("Rocky") != title_key("Rocky II") == "rocky 2"
    assert title_key("Star Trek II: The Wrath of Khan") == "star trek 2 the wrath of khan"
    assert title_key("Mission: Impossible") == "mission impossible"
    assert title_key("Henry V") == "henry 5"
    assert title_key("I Am Legend") == "i am legend"              # 单独的 I 不转换
    assert title_key("V for Vendetta") == "v for vendetta"          # 罗马数字只在末尾或冒号、连字符前转换


def test_title_leading_article_punctuation_and_accents():
    assert title_key("The Lord of the Rings: The Two Towers") == "lord of the rings the two towers"
    assert title_key("Don't Look Now") == "dont look now"
    assert title_key("Spider-Man") == title_key("Spider Man") == "spider man"
    assert title_key("Amélie") == "amelie"
    assert title_key("  Mr. & Mrs.   Smith ") == "mr and mrs smith"
    assert title_key("千と千尋の神隠し") == "千と千尋の神隠し"        # 非拉丁文字不被破坏


def test_title_imdb_alias_key_is_accent_insensitive():
    assert title_key("À bout de souffle") == "bout de souffle"
    assert title_key("A bout de souffle") == title_key("À bout de souffle")


def test_title_dash_and_comma_tails_are_removed_only_when_fully_classified():
    assert split_title("Casablanca - Widescreen Edition").key == "casablanca"
    assert split_title("Casablanca, Special Edition").key == "casablanca"
    assert split_title("Star Trek - The Motion Picture").key == "star trek the motion picture"
    assert split_title("Part 2: The DVD Experience").key == "part 2 the dvd experience"
    d = split_title("Casablanca - Digitally Remastered").descriptors
    assert [(x.raw, x.category, x.position) for x in d] == [("Digitally Remastered", "cut", "dash")]


def test_title_unknown_bracket_segment_is_removed_but_kept_as_unknown_descriptor():
    parts = split_title("Hamlet (Kenneth Branagh)")
    assert parts.key == "hamlet"
    assert [(d.raw, d.category) for d in parts.descriptors] == [("Kenneth Branagh", "unknown")]


def test_title_descriptor_attributes_feed_edition_fields():
    parts = split_title("Cinderella (Two-Disc Diamond Edition Blu-ray/DVD Combo in DVD Packaging) (Spanish Version)")
    attrs = {a for d in parts.descriptors for a in d.attributes}
    assert {("edition.media_format", "blu-ray"), ("edition.media_format", "dvd"), ("edition.language", "spanish")} <= attrs
    assert ("edition.packaging", "two-disc") in attrs and ("edition.packaging", "diamond edition") in attrs
    pal = {a for d in split_title("Meet John Doe (UK PAL Region 0)").descriptors for a in d.attributes}
    assert pal == {("edition.region", "region 0"), ("edition.video_system", "pal"), ("edition.region", "uk")}
    cut = {a for d in split_title("Blade Runner (Director's Cut) [Blu-ray]").descriptors for a in d.attributes}
    assert ("edition.cut_type", "directors cut") in cut and ("edition.media_format", "blu-ray") in cut


def test_title_year_only_from_bracketed_four_digits():
    assert split_title("Labyrinth (1986 VHS)").title_year == 1986
    assert split_title("Blade Runner 2049").title_year is None
    assert split_title("2012").title_year is None and split_title("2012").key == "2012"
    cat, attrs, year, covered = classify_segment("1986 VHS")
    assert (cat, year, covered) == ("year", 1986, True) and ("edition.media_format", "vhs") in attrs


# ---------------------------------------------------------------- 姓名与片长（AC04）

def test_name_keys_strict_and_loose():
    assert name_keys("Lewis Arlt") == ("lewis arlt", "lewis arlt")
    strict, loose = name_keys("Hamilton S. Luske")
    assert (strict, loose) == ("hamilton s luske", "hamilton luske")
    assert name_keys("Hamilton Luske") == ("hamilton luske", "hamilton luske")
    assert strict != name_keys("Hamilton Luske")[0] and loose == name_keys("Hamilton Luske")[1]
    assert strict_name("Zoë O'Brien-Smith") == "zoe obrien smith"
    assert name_keys("J. R. R. Tolkien")[1] == "j tolkien"


def test_name_byline_rows_are_one_person_each_and_last_first_is_reversed():
    people = parse_people([(1, "Arlt, Lewis"), (2, "Brown, Blair")], [(1, "Arlt, Lewis, Brown, Blair")])
    assert [(p.strict, p.qualifier) for p in people] == [("lewis arlt", "reversed"), ("blair brown", "reversed")]
    assert [p.raw for p in people] == ["Arlt, Lewis", "Brown, Blair"]      # 原值保持页面写法
    # 有署名行时不使用详情要点拆分
    assert len(people) == 2


def test_name_detail_bullets_split_by_comma_only_without_byline():
    people = parse_people([], [(3, "Clyde Geronimi, Hamilton S. Luske, Wilfred Jackson")])
    assert [(p.strict, p.loose, p.qualifier, p.ordinal) for p in people] == [
        ("clyde geronimi", "clyde geronimi", "detail", 3),
        ("hamilton s luske", "hamilton luske", "detail", 3),
        ("wilfred jackson", "wilfred jackson", "detail", 3)]


def test_name_ambiguous_last_first_detail_string_is_weak():
    people = parse_people([], [(1, "Arlt, Lewis, Brown, Blair")])
    assert {p.qualifier for p in people} == {"name_ambiguous"} and len(people) == 4
    assert [p.qualifier for p in parse_people([], [(1, "Brandy")])] == ["detail"]    # 单名不判歧义


def test_name_placeholder_credits_do_not_take_part_in_comparison():
    for raw in ("Various", "Artist Not Provided", "N/A", "Unknown", "Not Available"):
        (p,) = parse_people([(1, raw)], [])
        assert (p.strict, p.loose, p.qualifier) == (None, None, "placeholder")
    assert "various" in PLACEHOLDER_NAMES
    (p,) = parse_people([], [(1, "Various")])
    assert p.qualifier == "placeholder"


def test_name_suffix_after_comma_is_not_a_last_first_pair():
    (p,) = parse_people([(1, "Sammy Davis, Jr.")], [])
    assert (p.strict, p.qualifier) == ("sammy davis jr", "byline")


def test_runtime_minutes_and_release_date():
    assert runtime_minutes("1 hour and 50 minutes") == 110
    assert runtime_minutes("90 minutes") == 90
    assert runtime_minutes("NR") is None
    assert release_date_iso("May 18, 1999") == "1999-05-18"
    assert release_date_iso("Sep 2, 2008") == "2008-09-02"
    assert release_date_iso("not a date") is None
    assert genre_tags("Action & Adventure, Drama/Comedy") == ["action & adventure", "drama", "comedy"]


# ---------------------------------------------------------------- 合集成员拆分（AC06 的拆分部分）

def test_collection_split_slash_with_feature_marker():
    s = split_collection("A / B (Double Feature) [DVD]")
    assert s is not None and s.mode == "slash" and s.members == ("A", "B") and s.valid


def test_collection_split_parenthesised_list():
    s = split_collection("The Alien Legacy (Alien / Aliens / Alien 3 / Alien: Resurrection)")
    assert s.mode == "paren_list" and s.valid
    assert s.members == ("Alien", "Aliens", "Alien 3", "Alien: Resurrection")
    assert [split_title(m).key for m in s.members] == ["alien", "aliens", "alien 3", "alien resurrection"]


def test_collection_split_feature_ampersand_and_unsplit_cases():
    s = split_collection("Edward II & All Over Me (Double Feature)")
    assert s.mode == "feature_amp" and s.members == ("Edward II", "All Over Me")
    assert split_collection("Hollywood Hoodlums Collection") is None
    assert split_collection("Serial Killers 4 Movie Pack") is None
    assert split_collection("Salt & Pepper") is None                      # 没有 Feature 标记，& 不是分隔
    assert split_collection("Casablanca (Blu-ray 3D / Blu-ray / DVD)") is None   # 版本说明不是成员


def test_collection_split_member_count_must_be_between_two_and_six():
    seven = " / ".join(f"Film {i}" for i in range(1, 8))
    s = split_collection(f"Big Box ({seven})")
    assert s.mode == "paren_list" and len(s.members) == 7 and not s.valid
    s = split_collection("A / B / C / D / E / F / G")
    assert s.mode == "slash" and len(s.members) == 7 and not s.valid
