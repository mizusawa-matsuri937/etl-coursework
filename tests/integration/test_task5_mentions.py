"""任务 5 人物提及（spec AC01）：范围、来源、占位署名、边界不确定的详情要点、分批恢复。预期值按夹具手算（见 tests/fixtures/task5/README.md）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from task5 import people

import _task4_helpers as t4h
from _task5_helpers import inputs_for, make_world, reset_task5, start5
from etl_task5 import mentions
from etl_task5.run import RunBusy, Task5InputError, check_inputs, extraction_lock, human_set_hash

pytestmark = pytest.mark.integration


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return make_world(conn, make_settings, tmp_path)


def _asin_counts(conn, eid):
    return dict(conn.execute("SELECT asin, count(*) FROM task5.person_mention WHERE extraction_id = %s GROUP BY asin",
                             (eid,)).fetchall())


def test_check_inputs_requires_mapping_of_this_extraction_and_loaded_snapshot(conn, world, make_settings, tmp_path):
    out = check_inputs(conn, inputs_for(world))
    assert out["matched_products"] == 8 and out["imdb_title_keys"] >= 4
    with pytest.raises(Task5InputError, match="映射版本 map9999 不存在"):
        check_inputs(conn, inputs_for(world, t4_mapping_version_id="map9999"))
    with pytest.raises(Task5InputError, match="IMDb 快照"):
        check_inputs(conn, inputs_for(world, imdb_snapshot_id="imdb_2099-01-01"))
    other = t4h.build(conn, make_settings, tmp_path, [t4h.P("B0T5900001", "movie", "Other", directors=("X Y",))],
                      name="b")
    wrong = inputs_for(world)
    with pytest.raises(Task5InputError, match="属于抽取"):
        check_inputs(conn, type(wrong)(**{**wrong.__dict__, "extraction_id": other.eid}))


def test_scope_only_matched_products_of_the_mapping_version(conn, world):
    ctx = start5(conn, world)
    stats = mentions.extract_mentions(conn, ctx, batch_size=3)
    assert stats.entities == 8 and stats.batches == 3 and stats.remaining == 0
    assert _asin_counts(conn, world.fx.eid) == {
        "B0T5M00A01": 6, "B0T5M00A02": 3, "B0T5M00B01": 7, "B0T5M00C01": 2, "B0T5M00D01": 3, "B0T5M00E01": 6,
        "B0T5M00F01": 1, "B0T5M00K01": 4}                       # 剧集 T01 没有提及，合集 K01 有
    assert conn.execute("SELECT count(*) FROM task5.person_mention WHERE asin = 'B0T5M00T01'").fetchone()[0] == 0
    kinds = dict(conn.execute("SELECT asin, product_kind FROM task5.person_mention GROUP BY 1, 2").fetchall())
    assert kinds["B0T5M00K01"] == "collection" and kinds["B0T5M00A01"] == "movie"
    assert dict(conn.execute("SELECT status, count(*) FROM task5.person_mention GROUP BY 1").fetchall()) == {
        "resolvable": 26, "placeholder": 2, "ambiguous": 4}
    assert dict(conn.execute("SELECT role_class, count(*) FROM task5.person_mention GROUP BY 1").fetchall()) == {
        "director": 8, "actor": 24}


def test_scope_reads_the_given_mapping_version_not_another_one(conn, world):
    """旧映射版本（任务 3 规则 t3r1，手工登记）把剧集商品当电影：读它时有提及，读当前版本时没有。"""
    eid = world.fx.eid
    conn.execute(
        "INSERT INTO task4.product_edition (extraction_id, t3_rule_version, asin, product_kind, basis_level, "
        "match_scope, parser_version, content_hash, final_url, fetched_at, run_id) "
        "SELECT extraction_id, 't3r1', asin, CASE WHEN product_kind = 'tv_series' THEN 'movie' ELSE product_kind END, "
        "basis_level, CASE WHEN product_kind = 'tv_series' THEN 'matched' ELSE match_scope END, parser_version, "
        "content_hash, final_url, fetched_at, run_id FROM task4.product_edition WHERE extraction_id = %s", (eid,))
    conn.execute(
        "INSERT INTO task4.mapping_version (mapping_version_id, extraction_id, run_id, input_key, t3_rule_version, "
        "parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, counts) "
        "SELECT 'map9001', extraction_id, run_id, input_key || '|old', 't3r1', parser_version, norm_version, "
        "rule_version, imdb_snapshot_id, human_set_hash, counts FROM task4.mapping_version "
        "WHERE mapping_version_id = %s", (world.mapping,))
    old = SimpleNamespace(mapping_version_id="map9001")
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    assert conn.execute("SELECT count(*) FROM task5.person_mention WHERE asin = 'B0T5M00T01'").fetchone()[0] == 0
    reset_task5_only_mentions(conn)
    ctx_old = start5(conn, world, t4_mapping_version_id=old.mapping_version_id)
    mentions.extract_mentions(conn, ctx_old, batch_size=10)
    assert _asin_counts(conn, eid)["B0T5M00T01"] == 2


def reset_task5_only_mentions(conn):
    conn.execute("TRUNCATE task5.person_mention, task5.entity_stage, task5.quality_issue, task5.run_batch CASCADE")


def test_source_each_name_is_one_mention_pointing_at_its_page_field(conn, world):
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    eid = world.fx.eid
    rows = conn.execute("SELECT mention_key, asin, section, page_ordinal, list_position, label_raw, raw_value, "
                        "role_class, is_byline, byline_rank FROM task5.person_mention WHERE extraction_id = %s "
                        "ORDER BY mention_key", (eid,)).fetchall()
    assert len(rows) == len({r[0] for r in rows}) == 32
    for key, asin, section, ordinal, pos, label, raw, role, is_byline, rank in rows:
        assert key == f"{asin}:{section}:{ordinal}:{pos}"
        value, field_label = conn.execute("SELECT value_raw, label_raw FROM task3.page_field WHERE extraction_id = %s "
                                          "AND asin = %s AND section = %s AND ordinal = %s",
                                          (eid, asin, section, ordinal)).fetchone()
        assert label == field_label and raw in value                        # 原值是页面字段的一部分
        assert is_byline == (section == "byline") and (rank is None) == (not is_byline)
    # 署名行每行一人（列表位置 1），byline_rank 是同标签的第几行
    a01 = {r[0]: r for r in rows if r[1] == "B0T5M00A01"}
    assert a01["B0T5M00A01:byline:1:1"][6:] == ("Curtiz, Michael", "director", True, 1)
    assert a01["B0T5M00A01:byline:3:1"][6:] == ("Ingrid Bergman", "actor", True, 2)
    # 详情要点逗号串：列表位置，且署名行与详情要点重复的名字各一条
    detail = [r for r in rows if r[1] == "B0T5M00A01" and r[2] == "detail_bullets"]
    assert [(r[4], r[6]) for r in detail] == [(1, "Claude Rains"), (2, "Humphrey Bogart"), (3, "Ingrid Bergman")]
    assert all(r[7] == "actor" and r[9] is None for r in detail)


def test_source_director_from_detail_only_without_byline_director(conn, world):
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    d = conn.execute("SELECT asin, section, raw_value FROM task5.person_mention WHERE role_class = 'director' "
                     "AND asin IN ('B0T5M00A01', 'B0T5M00D01') ORDER BY asin").fetchall()
    assert d == [("B0T5M00A01", "byline", "Curtiz, Michael"),        # 有署名行导演：不取详情要点的 Michael Curtiz
                 ("B0T5M00D01", "detail_bullets", "Sylvester Stallone")]


def test_source_t4_assertion_page_snapshot_and_other_roles_excluded(conn, world):
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    # 任务 4 断言：署名行都对得上；只有无署名行时任务 4 才为详情要点建断言（D01），其余留空
    linked = conn.execute("SELECT count(*) FROM task5.person_mention WHERE t4_assertion_id IS NOT NULL").fetchone()[0]
    assert linked == 21
    assert conn.execute("SELECT count(*) FROM task5.person_mention m JOIN task4.field_assertion a "
                        "ON a.assertion_id = m.t4_assertion_id WHERE a.entity_id <> m.asin "
                        "OR a.raw_value <> m.raw_value OR a.field_name <> m.role_class").fetchone()[0] == 0
    nulls = conn.execute("SELECT asin, count(*) FROM task5.person_mention WHERE t4_assertion_id IS NULL "
                         "GROUP BY 1 ORDER BY 1").fetchall()
    assert nulls == [("B0T5M00A01", 3), ("B0T5M00B01", 4), ("B0T5M00E01", 4)]
    # 页面快照来源记录：与任务 4 的标题断言同一条
    assert conn.execute("SELECT count(*) FROM task5.person_mention m JOIN task4.field_assertion t "
                        "ON t.entity_id = m.asin AND t.field_name = 'title' AND t.ordinal = 1 "
                        "JOIN task4.source_record s ON s.source_record_id = m.source_record_id "
                        "WHERE s.source_type = 'page' AND t.source_record_id = m.source_record_id").fetchone()[0] == 32
    # 编剧、制片只在页面字段里，不建提及
    assert conn.execute("SELECT count(*) FROM task5.person_mention WHERE raw_value IN ('Julius J. Epstein', "
                        "'Hal B. Wallis', 'Howard Koch')").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task3.page_field WHERE asin = 'B0T5M00A01' AND field_key IN "
                        "('writers', 'producers')").fetchone()[0] == 3


def test_placeholder_mentions_are_marked_and_not_resolvable(conn, world):
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    rows = conn.execute("SELECT mention_key, role_class FROM task5.person_mention WHERE status = 'placeholder' "
                        "ORDER BY 1").fetchall()
    assert rows == [("B0T5M00E01:byline:1:1", "director"), ("B0T5M00E01:byline:2:1", "actor")]


def test_ambiguous_comma_string_marks_every_name_and_records_an_issue(conn, world):
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    rows = conn.execute("SELECT raw_value, list_position, status FROM task5.person_mention "
                        "WHERE asin = 'B0T5M00E01' AND section = 'detail_bullets' ORDER BY list_position").fetchall()
    assert rows == [("Arlt", 1, "ambiguous"), ("Lewis", 2, "ambiguous"), ("Brown", 3, "ambiguous"),
                    ("Blair", 4, "ambiguous")]
    issues = conn.execute("SELECT entity_id, issue_code, detail FROM task5.quality_issue").fetchall()
    assert len(issues) == 1 and issues[0][:2] == ("B0T5M00E01", "ambiguous_names")
    assert issues[0][2]["value"] == "Arlt, Lewis, Brown, Blair"
    # 没有 resolvable 提及的名字不是人物候选：只有 resolvable 的提及才会被后续步骤使用
    assert conn.execute("SELECT count(*) FROM task5.person_mention WHERE status = 'resolvable' AND "
                        "asin = 'B0T5M00E01'").fetchone()[0] == 0


def test_name_parts_and_keys_are_stored_with_the_mention(conn, world):
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    row = conn.execute("SELECT display_name, strict_key, flags, raw_value FROM task5.person_mention "
                       "WHERE mention_key = 'B0T5M00A01:byline:2:1'").fetchone()
    assert row == ("Humphrey Bogart", "humphrey bogart", {"reversed": True}, "Bogart, Humphrey")
    row = conn.execute("SELECT disambiguator, strict_key FROM task5.person_mention WHERE asin = 'B0T5M00F01'").fetchone()
    assert row == ("III", "robert wilson")
    row = conn.execute("SELECT raw_value, suffix, strict_key FROM task5.person_mention WHERE asin = 'B0T5M00B01' "
                       "AND list_position = 2 AND section = 'detail_bullets'").fetchone()
    assert row == ("Robert Downey, Jr.", "Jr.", "robert downey jr")


def test_batches_resume_after_failure_without_duplicates(conn, world):
    ctx = start5(conn, world)

    def boom(stage, batch_no):
        if batch_no == 2:
            raise RuntimeError("注入故障")

    with pytest.raises(RuntimeError, match="注入故障"):
        mentions.extract_mentions(conn, ctx, batch_size=3, before_commit=boom)
    first = _asin_counts(conn, world.fx.eid)
    assert set(first) == {"B0T5M00A01", "B0T5M00A02", "B0T5M00B01"}              # 只有第 1 批
    assert conn.execute("SELECT count(*) FROM task5.entity_stage").fetchone()[0] == 3
    assert mentions.count_pending(conn, ctx) == 5
    stats = mentions.extract_mentions(conn, ctx, batch_size=3)
    assert stats.entities == 5 and stats.remaining == 0
    assert sum(_asin_counts(conn, world.fx.eid).values()) == 32
    assert conn.execute("SELECT count(*) FROM (SELECT mention_key FROM task5.person_mention "
                        "GROUP BY mention_key HAVING count(*) > 1) d").fetchone()[0] == 0
    # 第二次进入只补剩余，批号接着编；审计行数 = 提及行数
    assert conn.execute("SELECT sum(rows_written) FROM task5.run_batch").fetchone()[0] == 32
    assert mentions.extract_mentions(conn, ctx, batch_size=3).entities == 0


def test_mention_stage_key_ignores_rule_and_mapping_versions(conn, world):
    a = inputs_for(world)
    b = inputs_for(world, rule_version="t5r9")
    assert a.stage_key("mentions") == b.stage_key("mentions") and a.stage_key("decide") != b.stage_key("decide")
    assert a.key() != b.key() and a.human_set_hash == "none" == human_set_hash(conn, world.fx.eid)
    # 换规则版本再抽取：商品已有标记，不重复抽取
    mentions.extract_mentions(conn, start5(conn, world), batch_size=10)
    assert mentions.extract_mentions(conn, start5(conn, world, rule_version="t5r9"), batch_size=10).entities == 0
    assert conn.execute("SELECT count(*) FROM task5.person_mention").fetchone()[0] == 32


def test_second_runner_cannot_take_the_extraction_lock(conn, world, base_settings):
    from etl_task1.db import connect
    with extraction_lock(conn, world.fx.eid):
        with connect(base_settings) as other, pytest.raises(RunBusy):
            with extraction_lock(other, world.fx.eid):
                pass
    with connect(base_settings) as other, extraction_lock(other, world.fx.eid):
        pass
