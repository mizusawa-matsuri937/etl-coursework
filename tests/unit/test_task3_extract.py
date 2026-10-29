"""任务 3 字段抽取（spec AC03）：只取证据区域、排除脚本与导航、标签映射、署名逐个保存。"""

from __future__ import annotations

from task3.pages import EXTRACT_EXPECTED, EXTRACT_PAGE, page

from etl_task3.extract import SECTIONS, clean, extract_fields, field_key


def test_extract_fields_match_fixture_exactly():
    fields, present = extract_fields(EXTRACT_PAGE)
    got = [(f.section, f.ordinal, f.label_raw, f.value_raw, f.field_key) for f in fields]
    assert got == EXTRACT_EXPECTED
    assert set(present) == set(SECTIONS)
    assert present["detail_bullets"] and present["product_overview"] and not present["product_details"]


def test_navigation_description_and_scripts_are_not_extracted():
    fields, _ = extract_fields(EXTRACT_PAGE)
    text = " ".join(f.value_raw for f in fields)
    for noise in ("Blu-ray", "Prime Video", "Television", "Double Feature", "Jane Roe", "PHYSICAL_TV_SERIES",
                  "function", "Season 1"):
        assert noise not in text


def test_unknown_label_kept_with_null_key_and_runtime_labels_share_key():
    fields, _ = extract_fields(EXTRACT_PAGE)
    unknown = [f for f in fields if f.label_raw == "Item model number"]
    assert len(unknown) == 1 and unknown[0].field_key is None and unknown[0].value_raw == "MX-1"
    assert {f.label_raw for f in fields if f.field_key == "runtime"} == {"Run time", "Runtime"}


def test_byline_roles_are_split_in_page_order_and_swatch_takes_selected_format():
    body = page("Film", crumbs=("Movies & TV",), byline=(("Ann Lee", "Director, Writer"), ("Bob Ray", "Actor"),
                                                          ("Cy Day", "Actor")), rated="R", fmt="DVD", swatch="Blu-ray")
    fields, _ = extract_fields(body)
    byline = [(f.ordinal, f.label_raw, f.value_raw, f.field_key) for f in fields if f.section == "byline"]
    assert byline == [(1, "Director", "Ann Lee", "director"), (2, "Writer", "Ann Lee", "writers"),
                      (3, "Actor", "Bob Ray", "actors"), (4, "Actor", "Cy Day", "actors"),
                      (5, "Rated", "R", "mpaa_rating"), (6, "Format", "DVD", "media_format")]
    assert [(f.value_raw, f.field_key) for f in fields if f.section == "format_swatch"] == [("Blu-ray", "media_format")]


def test_page_without_evidence_sections_reports_all_absent():
    fields, present = extract_fields(b"<html><body><div id='nav-main'>DVD Movies</div></body></html>")
    assert fields == [] and not any(present.values())


def test_clean_and_label_mapping():
    assert clean("  a‎ \n b‏ ") == "a b"
    assert field_key("detail_bullets", "Release date") == "product_release_date"
    assert field_key("detail_bullets", "UPC") == "upc"
    assert field_key("detail_bullets", "Edition") == "edition"
    assert field_key("byline", "Producer") == "producers"
    assert field_key("byline", "Narrator") == "contributor"
