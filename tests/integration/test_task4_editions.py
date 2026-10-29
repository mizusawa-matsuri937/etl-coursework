"""任务 4 商品版本与字段断言（spec AC01、AC03/AC04 的落库部分、AC06 的拆分部分）。"""

from __future__ import annotations

import pytest
from task4 import imdb_files as ifx

from _task4_helpers import P, build, inputs_for, load_imdb, reset_task4, standard_products
from etl_task4 import editions
from etl_task4.run import Task4InputError, check_inputs, start_run

pytestmark = pytest.mark.integration


@pytest.fixture
def world(conn, make_settings, tmp_path):
    fx = build(conn, make_settings, tmp_path, standard_products())
    reset_task4(conn)  # 先清 imdb/task4（build 已建表），再装 IMDb 快照
    sid = load_imdb(conn, tmp_path, ifx.BASIC)
    ctx = start_run(conn, inputs_for(fx, sid))
    return fx, ctx


def _scope(conn, eid):
    return dict(conn.execute("SELECT asin, match_scope FROM task4.product_edition WHERE extraction_id = %s",
                             (eid,)).fetchall())


def test_product_edition_one_row_per_asin_with_asin_as_ids(conn, world):
    fx, ctx = world
    assert check_inputs(conn, ctx.inputs) == {"manifest_rows": 12, "classification_rows": 12}
    assert editions.build_editions(conn, ctx) == 12
    rows = conn.execute("SELECT asin, product_kind FROM task4.product_edition WHERE extraction_id = %s",
                        (fx.eid,)).fetchall()
    assert len(rows) == 12 and {r[0] for r in rows} == {p.asin for p in fx.products}
    assert conn.execute("SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s AND "
                        "t3_rule_version = 't3r2'", (fx.eid,)).fetchone()[0] == 12
    assert editions.build_editions(conn, ctx) == 0        # 重复运行不新增


def test_match_scope_matched_not_applicable_and_no_page(conn, world):
    fx, ctx = world
    editions.build_editions(conn, ctx)
    scope = _scope(conn, fx.eid)
    assert sorted(scope.values()).count("matched") == 6
    assert sorted(scope.values()).count("not_applicable") == 4
    assert sorted(scope.values()).count("no_page") == 2
    assert {a for a, s in scope.items() if s == "not_applicable"} == {
        "B0T4000007", "B0T4000008", "B0T4000009", "B0T4000010"}
    assert {a for a, s in scope.items() if s == "no_page"} == {"B0T4000011", "B0T4000012"}
    # 无页面的行没有页面来源；有页面的带快照哈希、最终地址与采集时间
    assert conn.execute("SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s AND "
                        "(content_hash IS NULL) <> (match_scope = 'no_page')", (fx.eid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task4.product_edition WHERE match_scope <> 'no_page' AND "
                        "(final_url IS NULL OR fetched_at IS NULL)").fetchone()[0] == 0


def test_reads_only_the_given_extraction_and_rule_version(conn, make_settings, tmp_path):
    a = build(conn, make_settings, tmp_path, standard_products(), name="a")
    b = build(conn, make_settings, tmp_path, [P("B0T4900001", "movie", "Other", directors=("X Y",))], name="b")
    reset_task4(conn)
    sid = load_imdb(conn, tmp_path, ifx.BASIC)
    # 给 a 另造一个旧规则版本的分类：只读指定的 t3r2
    conn.execute("INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, category, "
                 "basis_level, rule_id, pending_reason, run_id) SELECT extraction_id, asin, 't3r1', input_group, "
                 "'pending', 'missing', 'NP1', 'x', run_id FROM task3.product_classification "
                 "WHERE extraction_id = %s AND input_group <> 'dp_page'", (a.eid,))
    ctx = start_run(conn, inputs_for(a, sid))
    editions.build_editions(conn, ctx)
    assert conn.execute("SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s",
                        (a.eid,)).fetchone()[0] == 12
    assert conn.execute("SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s",
                        (b.eid,)).fetchone()[0] == 0


def test_tv_series_and_unmatched_scopes_have_no_assertions(conn, world):
    fx, ctx = world
    editions.build_editions(conn, ctx)
    stats = editions.normalize_extraction(conn, ctx, batch_size=2)
    assert stats.entities == 6 and stats.batches == 3 and stats.remaining == 0
    rows = {r[0] for r in conn.execute("SELECT DISTINCT split_part(entity_id, '#', 1) FROM task4.field_assertion "
                                       "WHERE extraction_id = %s", (fx.eid,)).fetchall()}
    assert rows == {f"B0T40000{i:02d}" for i in range(1, 7)}
    assert conn.execute("SELECT count(*) FROM task4.field_assertion WHERE entity_id = 'B0T4000007'").fetchone()[0] == 0


def test_check_inputs_rejects_missing_snapshot_and_incomplete_classification(conn, world):
    fx, ctx = world
    with pytest.raises(Task4InputError, match="IMDb 快照"):
        check_inputs(conn, inputs_for(fx, "imdb_2099-01-01"))
    conn.execute("DELETE FROM task3.product_classification WHERE extraction_id = %s AND asin = 'B0T4000012'",
                 (fx.eid,))
    with pytest.raises(Task4InputError, match="任务 3 分类"):
        check_inputs(conn, ctx.inputs)


def _assertions(conn, eid, entity, field=None):
    sql = ("SELECT field_name, ordinal, raw_value, normalized_value, qualifier, rule_id, parent_assertion_id, "
           "assertion_id, source_locator, source_record_id FROM task4.field_assertion WHERE extraction_id = %s "
           "AND entity_id = %s")
    args = [eid, entity]
    if field:
        sql += " AND field_name = %s"
        args.append(field)
    return conn.execute(sql + " ORDER BY field_name, ordinal", args).fetchall()


def test_title_key_descriptor_and_original_title_are_stored_with_lineage(conn, world):
    fx, ctx = world
    editions.build_editions(conn, ctx)
    editions.normalize_extraction(conn, ctx, batch_size=10)
    (title,) = _assertions(conn, fx.eid, "B0T4000002", "title")
    assert title[2:4] == ("Casablanca (Ultimate Collector's Edition)", "casablanca")        # 原始标题未改动
    assert title[8] == {"section": "title", "ordinal": 1, "field_key": "title"}                # 指回任务 3 字段
    (desc,) = _assertions(conn, fx.eid, "B0T4000002", "descriptor")
    assert desc[2] == "Ultimate Collector's Edition" and desc[4] == "packaging" and desc[6] == title[7]
    (omen_year,) = _assertions(conn, fx.eid, "B0T4000004", "title_year")
    assert omen_year[3] == "2006"
    assert _assertions(conn, fx.eid, "B0T4000003", "title")[0][3] == "rocky 2"
    # 同一个页面快照只登记一次来源记录
    src = conn.execute("SELECT source_type, content_hash FROM task4.source_record sr JOIN task4.field_assertion fa "
                       "USING (source_record_id) WHERE fa.entity_id = 'B0T4000001' LIMIT 1").fetchone()
    assert src[0] == "page" and len(src[1]) == 64


def test_people_runtime_and_release_date_are_assertions_not_features(conn, make_settings, tmp_path):
    products = [P("B0T4100001", "movie", "Day Show", directors=("Arlt, Lewis", "Brown, Blair"),
                  directors_detail="Arlt, Lewis, Brown, Blair", actors=("Various",),
                  actors_detail="Clyde Geronimi, Hamilton S. Luske, Wilfred Jackson",
                  runtime="1 hour and 50 minutes", runtime_overview="1 hour and 51 minutes",
                  release_date="May 18, 1999", genre="Drama, Comedy"),
                P("B0T4100002", "movie", "Night Show", directors_detail="Clyde Geronimi, Hamilton S. Luske",
                  runtime="90 minutes")]
    fx = build(conn, make_settings, tmp_path, products)
    reset_task4(conn)
    ctx = start_run(conn, inputs_for(fx, load_imdb(conn, tmp_path, ifx.BASIC)))
    editions.build_editions(conn, ctx)
    editions.normalize_extraction(conn, ctx, batch_size=10)
    d = [(r[2], r[3], r[4]) for r in _assertions(conn, fx.eid, "B0T4100001", "director")]
    assert d == [("Arlt, Lewis", "lewis arlt", "reversed"), ("Brown, Blair", "blair brown", "reversed")]
    a = [(r[3], r[4]) for r in _assertions(conn, fx.eid, "B0T4100001", "actor")]
    assert a == [(None, "placeholder")]                                  # 占位署名不参与比较；有署名行时不拆详情要点
    d2 = [(r[3], r[4]) for r in _assertions(conn, fx.eid, "B0T4100002", "director")]
    assert d2 == [("clyde geronimi", "detail"), ("hamilton s luske", "detail")]
    loose = conn.execute("SELECT extra->>'loose' FROM task4.field_assertion WHERE entity_id = 'B0T4100002' AND "
                         "field_name = 'director' ORDER BY ordinal").fetchall()
    assert loose == [("clyde geronimi",), ("hamilton luske",)]
    rt = _assertions(conn, fx.eid, "B0T4100001", "runtime_min")
    assert [(r[3], r[4]) for r in rt] == [("110", "conflict"), ("111", "conflict")]      # 两个来源都存，标冲突
    assert _assertions(conn, fx.eid, "B0T4100002", "runtime_min")[0][3] == "90"
    rd = _assertions(conn, fx.eid, "B0T4100001", "edition.product_release_date")
    assert rd[0][2:4] == ("May 18, 1999", "1999-05-18")
    assert [r[3] for r in _assertions(conn, fx.eid, "B0T4100001", "genre")] == ["drama", "comedy"]
    # 发售日期只在版本属性里，不产生 title_year
    assert _assertions(conn, fx.eid, "B0T4100001", "title_year") == []
    issues = conn.execute("SELECT entity_id, issue_code FROM task4.quality_issue WHERE issue_code = 'name_ambiguous'"
                          ).fetchall()
    assert issues == []                                                   # 歧义只在没有署名行时出现


def test_name_ambiguous_detail_only_is_marked_and_reported(conn, make_settings, tmp_path):
    fx = build(conn, make_settings, tmp_path,
               [P("B0T4200001", "movie", "Solo", directors_detail="Arlt, Lewis, Brown, Blair")])
    reset_task4(conn)
    ctx = start_run(conn, inputs_for(fx, load_imdb(conn, tmp_path, ifx.BASIC)))
    editions.build_editions(conn, ctx)
    editions.normalize_extraction(conn, ctx, batch_size=10)
    assert {r[4] for r in _assertions(conn, fx.eid, "B0T4200001", "director")} == {"name_ambiguous"}
    assert conn.execute("SELECT count(*) FROM task4.quality_issue WHERE issue_code = 'name_ambiguous'").fetchone()[0] == 1


def test_missing_title_is_a_quality_issue_not_a_failure(conn, make_settings, tmp_path):
    fx = build(conn, make_settings, tmp_path, [P("B0T4300001", "movie", None, directors=("A B",))])
    reset_task4(conn)
    ctx = start_run(conn, inputs_for(fx, load_imdb(conn, tmp_path, ifx.BASIC)))
    editions.build_editions(conn, ctx)
    stats = editions.normalize_extraction(conn, ctx, batch_size=10)
    assert stats.entities == 1 and stats.issues == {"missing_title": 1}
    assert _assertions(conn, fx.eid, "B0T4300001", "title") == []


def test_collection_members_split_with_originals_ordinals_and_lineage(conn, world):
    fx, ctx = world
    editions.build_editions(conn, ctx)
    editions.normalize_extraction(conn, ctx, batch_size=10)
    members = conn.execute("SELECT entity_id, raw_value, normalized_value, qualifier, extra, parent_assertion_id "
                           "FROM task4.field_assertion WHERE entity_type = 'collection_member' AND "
                           "field_name = 'title' ORDER BY entity_id").fetchall()
    assert [(m[0], m[1], m[2], m[3]) for m in members] == [("B0T4000005#1", "A", "a", "slash"),
                                                           ("B0T4000005#2", "B", "b", "slash")]
    assert members[0][4]["member_ordinal"] == 1 and members[1][4]["members"] == 2
    parent = conn.execute("SELECT entity_id, field_name FROM task4.field_assertion WHERE assertion_id = %s",
                          (members[0][5],)).fetchone()
    assert parent == ("B0T4000005", "title")
    # 拆不出的合集：没有成员实体，记 collection_unsplit
    assert conn.execute("SELECT count(*) FROM task4.field_assertion WHERE entity_id LIKE 'B0T4000006#%'"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT entity_id, detail FROM task4.quality_issue WHERE issue_code = 'collection_unsplit'"
                        ).fetchall() == [("B0T4000006", {"reason": "no_delimiter"})]


def test_collection_with_parenthesised_list_gets_four_members(conn, make_settings, tmp_path):
    fx = build(conn, make_settings, tmp_path, [
        P("B0T4400001", "collection", "The Alien Legacy (Alien / Aliens / Alien 3 / Alien: Resurrection)")])
    reset_task4(conn)
    ctx = start_run(conn, inputs_for(fx, load_imdb(conn, tmp_path, ifx.BASIC)))
    editions.build_editions(conn, ctx)
    editions.normalize_extraction(conn, ctx, batch_size=10)
    rows = conn.execute("SELECT entity_id, raw_value, normalized_value FROM task4.field_assertion WHERE "
                        "entity_type = 'collection_member' AND field_name = 'title' ORDER BY entity_id").fetchall()
    assert rows == [("B0T4400001#1", "Alien", "alien"), ("B0T4400001#2", "Aliens", "aliens"),
                    ("B0T4400001#3", "Alien 3", "alien 3"), ("B0T4400001#4", "Alien: Resurrection", "alien resurrection")]


def test_normalize_is_resumable_and_idempotent(conn, world):
    fx, ctx = world
    editions.build_editions(conn, ctx)

    def boom(stage, batch_no):
        if batch_no == 2:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        editions.normalize_extraction(conn, ctx, batch_size=2, before_commit=boom)
    done = {r[0] for r in conn.execute("SELECT entity_id FROM task4.entity_stage WHERE stage = 'normalize'").fetchall()}
    assert done == {"B0T4000001", "B0T4000002"}                           # 只有第 1 批
    assert conn.execute("SELECT count(*) FROM task4.field_assertion WHERE split_part(entity_id, '#', 1) NOT IN "
                        "('B0T4000001', 'B0T4000002')").fetchone()[0] == 0   # 第 2 批没有部分行
    stats = editions.normalize_extraction(conn, ctx, batch_size=2)
    assert stats.entities == 4 and stats.remaining == 0
    total = conn.execute("SELECT count(*) FROM task4.field_assertion").fetchone()[0]
    again = editions.normalize_extraction(conn, ctx, batch_size=2)
    assert again.entities == 0 and conn.execute("SELECT count(*) FROM task4.field_assertion").fetchone()[0] == total
