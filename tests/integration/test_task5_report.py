"""任务 5 统计、实例、两个查询与只读核对（spec AC12；核对命令即 AC14 的检查代码）。

主夹具是 AC01–AC08 的商品与 IMDb 数据合在一起运行（人物提及、归并、MoviePerson 各夹具），外加两个同名导演的单品；
预期值按夹具手算或与库中直接数出的结果逐项对照。
"""

from __future__ import annotations

import csv
import hashlib
import json

import pytest
from task5 import links, people as base, resolve as resfx

from _task5_helpers import combined_inputs, inputs_for, make_world
from etl_task5 import check, query, report
from etl_task5.runner import execute_resolve

pytestmark = pytest.mark.integration


@pytest.fixture
def ran(conn, make_settings, tmp_path):
    products, imdb = combined_inputs()
    w = make_world(conn, make_settings, tmp_path, products, imdb)
    r = execute_resolve(conn, inputs_for(w), batch_size=7)
    assert r.status == "succeeded"
    return w, r.person_mapping_version_id


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


def pid_of(conn, pm, asin, raw):
    return one(conn, "SELECT mp.person_id FROM task5.mention_person mp JOIN task5.person_mention m USING (mention_key) "
                     "WHERE mp.person_mapping_version_id = %s AND m.asin = %s AND m.raw_value = %s ORDER BY m.mention_key "
                     "LIMIT 1", pm, asin, raw)


# ---------------------------------------------------------------- 统计

def test_summary_matches_the_database(conn, ran):
    w, pm = ran
    s = report.build_summary(conn, pm)
    mn, ba, ex, cw, pe, mp, al = (s[k] for k in ("mentions", "before_after", "external_ids", "cross_work", "people",
                                                 "movie_person", "aliases"))
    assert mn["total"] == one(conn, "SELECT count(*) FROM task5.person_mention")
    assert mn["by_status"] == dict(conn.execute("SELECT status, count(*) FROM task5.person_mention GROUP BY 1").fetchall())
    assert mn["by_role"] == dict(conn.execute("SELECT role_class, count(*) FROM task5.person_mention GROUP BY 1").fetchall())
    assert mn["other_role_rows"] == 3 and {x["field_key"] for x in mn["other_roles"]} == {"writers", "producers"}
    assert ba == {**ba, "resolvable_mentions": one(conn, "SELECT count(*) FROM task5.person_mention WHERE status = 'resolvable'"),
                  "raw_variants": one(conn, "SELECT count(DISTINCT raw_value) FROM task5.person_mention WHERE status = 'resolvable'"),
                  "strict_keys": one(conn, "SELECT count(DISTINCT strict_key) FROM task5.person_mention WHERE status = 'resolvable'"),
                  "groups": one(conn, "SELECT count(*) FROM task5.person_group"),
                  "persons": one(conn, "SELECT count(*) FROM task5.person")}
    assert ba["raw_variants"] >= ba["strict_keys"] and ba["groups"] >= ba["persons"]    # 同名的不同人使人物数可以多于严格键数
    assert ex["mentions_linked_by_route"] == dict(conn.execute(
        "SELECT c.route, count(DISTINCT c.mention_key) FROM task5.link_candidate c JOIN task5.link_decision d USING "
        "(link_candidate_id) WHERE d.tier = 'auto' GROUP BY 1").fetchall())
    assert sum(ex["tiers_by_route"].values()) == one(conn, "SELECT count(*) FROM task5.link_decision")
    assert ex["persons_with_external_id"] == one(conn, "SELECT count(*) FROM task5.person WHERE has_external_id")
    assert ex["source_kind"]["third_party_imdb"] + ex["source_kind"]["page_derived"] == ba["resolvable_mentions"]
    assert cw["candidates"] == one(conn, "SELECT count(*) FROM task5.person_candidate") == sum(cw["by_tier"].values())
    assert cw["iteration"] == [{"round": r, "new_auto_edges": a, "new_merged_persons": b, "blocked_edges": c} for r, a, b, c in
                               conn.execute("SELECT round, new_auto_edges, new_merged_persons, blocked_edges FROM "
                                            "task5.iteration_round ORDER BY round").fetchall()] and cw["rounds"] == len(cw["iteration"])
    assert {"different_external_id", "era_conflict"} <= set(cw["hard_conflicts"])
    assert pe["status"] == dict(conn.execute("SELECT status, count(*) FROM task5.person GROUP BY 1").fetchall())
    assert pe["review_pending_rate"] == round(pe["status"]["review_pending"] / pe["persons"], 6)
    assert pe["queue_rows"] > 0 and pe["queue_top"][0]["rank"] == 1 and pe["human_decisions"] == 0
    assert pe["human_review_coverage"] == 0 and pe["review_candidates"] >= pe["queue_rows"] - 5
    assert mp["by_role"] == dict(conn.execute("SELECT role, count(*) FROM task5.movie_person GROUP BY 1").fetchall())
    assert mp["relations"] == sum(mp["by_role"].values()) and mp["order_conflicts"] == 4
    assert mp["unattributed_mentions"] == one(conn, "SELECT count(*) FROM task5.mention_person WHERE attribution = 'unattributed'")
    assert mp["multi_role_persons"] >= 2          # Dir One（director + cast）与 Orson Dual（director + starring）
    assert {"reversed_order", "imdb_primary", "identical"} <= set(al["types"])


def test_summary_counts_feature_switch_effects_and_upgrade_hints(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, resfx.switch_products(), base.IMDB)
    pm = execute_resolve(conn, inputs_for(w), batch_size=3).person_mapping_version_id
    s = report.build_summary(conn, pm)
    assert s["cross_work"]["feature_switches"] == {"rarity_would_change": 2, "publisher_would_change": 0}   # Rora Rare 与 Shared Mate 两对稀有名各有一位同名合作者


def test_summary_upgrade_hints_and_route_counts_on_the_filmography_fixture(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, links.film_products(), links.FILM_IMDB)
    pm = execute_resolve(conn, inputs_for(w), batch_size=3).person_mapping_version_id
    ex = report.build_summary(conn, pm)["external_ids"]
    assert ex["upgrade_hints"] == one(conn, "SELECT count(*) FROM task5.t4_upgrade_hint") == 1
    assert ex["upgrade_hint_examples"][0]["mention_count"] == 2
    assert ex["mentions_linked_by_route"]["filmography"] == 5                  # U01、U02、U08 两位、K01 第 2 个成员
    assert ex["tiers_by_route"]["filmography/review"] == 5 and ex["merges_by_route"]["co_credit"] >= 0


# ---------------------------------------------------------------- 实例

def test_examples_list_alias_merges_and_non_merges_with_evidence_and_source_locators(conn, ran):
    w, pm = ran
    ex = report.examples(conn, pm)
    names = {g["canonical_name"] for g in ex["alias_merges"]}
    assert {"Humphrey Bogart", "Michael Curtiz"} <= names
    bogart = next(g for g in ex["alias_merges"] if g["canonical_name"] == "Humphrey Bogart")
    assert bogart["external_id"]["nconst"] == "nm0000302" and bogart["external_id"]["name_line_no"]
    assert {a["alias"] for a in bogart["aliases"]} == {"Bogart, Humphrey", "Humphrey Bogart"}
    assert any("reversed_order" in a["types"] for a in bogart["aliases"])
    assert all(m["mention_key"] and m["asin"] and "final_url" in m and "snapshot_path" in m for g in ex["alias_merges"]
               for m in g["mentions"])
    assert len(ex["not_merged"]) == 5
    vetoes = {v for g in ex["not_merged"] for v in g["veto"]}
    assert {"different_external_id", "era_conflict"} <= vetoes                  # 硬冲突的实例排在前面
    for g in ex["not_merged"]:
        assert g["left"]["person_id"] != g["right"]["person_id"] and g["left"]["mention_key"] and g["right"]["asin"]
        assert g["tier"] in ("review", "reject") and g["annotation"] is None   # 没有用户标注
    text = report.render_examples(ex)
    assert "Humphrey Bogart" in text and "未标注" in text and "different_external_id" in text


def test_examples_put_user_annotated_pairs_first_and_state_the_conclusion(conn, ran):
    w, pm = ran
    a = one(conn, "SELECT mention_key FROM task5.person_mention WHERE asin = 'B0T5R00C01' AND raw_value = 'Sam Solo'")
    b = one(conn, "SELECT mention_key FROM task5.person_mention WHERE asin = 'B0T5R00C02' AND raw_value = 'Sam Solo'")
    conn.execute("INSERT INTO task5.eval_set (eval_set_id, extraction_id, person_mapping_version_id, rule_version, strata) "
                 "VALUES ('es1', %s, %s, 't5r1', '{}')", (w.fx.eid, pm))
    conn.execute("INSERT INTO task5.eval_pair (eval_set_id, pair_id, stratum, left_mention, right_mention, left_group, "
                 "right_group, left_person, right_person, predicted_same) VALUES ('es1', 'p1', 'S4', %s, %s, 'g1', 'g2', "
                 "'x', 'y', false)", (a, b))
    conn.execute("INSERT INTO task5.eval_label (eval_set_id, pair_id, label, annotator, import_file_sha256) "
                 "VALUES ('es1', 'p1', 'different', 'tester', %s)", ("0" * 64,))
    ex = report.examples(conn, pm)
    first = ex["not_merged"][0]
    assert {first["left"]["mention_key"], first["right"]["mention_key"]} == {a, b}
    assert first["annotation"]["label"] == "different" and first["annotation"]["annotator"] == "tester"
    assert "different（tester）" in report.render_examples(ex)


# ---------------------------------------------------------------- 两个查询

def test_query_person_returns_raw_signatures_aliases_decision_chain_and_works(conn, ran):
    w, pm = ran
    pid = pid_of(conn, pm, "B0T5M00A01", "Bogart, Humphrey")
    out = query.query_person(conn, pm, pid)
    assert out["found"] and out["canonical_name"] == "Humphrey Bogart" and out["status"] == "external_verified"
    assert out["external_id"]["nconst"] == "nm0000302" and out["external_id"]["imdb_url"].endswith("/nm0000302/")
    assert {a["alias"] for a in out["aliases"]} == {"Bogart, Humphrey", "Humphrey Bogart"}
    raws = {m["raw"] for m in out["mentions"]}
    assert raws == {"Bogart, Humphrey", "Humphrey Bogart"} and out["mentions_total"] == len(out["mentions"]) == 3
    assert all(m["url"].startswith("https://www.amazon.com/dp/") and m["section"] and m["role"] for m in out["mentions"])
    chain = out["decision_chain"]
    assert chain["external_id_decisions"] and {d["route"] for d in chain["external_id_decisions"]} == {"credits"}
    assert all(d["tier"] == "auto" and d["tconst"] == "tt0000301" for d in chain["external_id_decisions"])
    assert chain["within_work_groups"] and chain["human_decisions"] == []
    works = {x["tconst"]: x for x in out["works"]}
    assert set(works) == {"tt0000301"} and {r["role"] for r in works["tt0000301"]["roles"]} == {"starring", "cast"}
    assert {a["asin"] for a in works["tt0000301"]["asins"]} == {"B0T5M00A01", "B0T5M00A02"}
    assert query.query_person(conn, pm, "ps_0000000000000000") == {
        "person_mapping_version_id": pm, "person_id": "ps_0000000000000000", "found": False}


def test_query_person_shows_the_cross_work_merge_decision_with_its_round(conn, ran):
    w, pm = ran
    cy = pid_of(conn, pm, "B0T5R00D01", "Cy Match")
    out = query.query_person(conn, pm, cy)
    edges = out["decision_chain"]["cross_work_auto_decisions"]
    assert edges and all(e["tier"] == "auto" and e["reason"] == "co_credit" and e["round"] >= 1 for e in edges)
    assert out["status"] == "page_only" and out["work_count"] == 2 and {m["basis"] for m in out["mentions"]} == {"co_credit"}


def test_query_director_lists_same_name_people_separately_and_matches_reversed_names(conn, ran):
    w, pm = ran
    twin = query.query_director(conn, pm, "Twin Dir")
    assert twin["matched_persons"] == len(twin["persons"]) == 2
    assert len({p["person_id"] for p in twin["persons"]}) == 2
    assert all(len(p["works"]) == 1 and p["works"][0]["roles"][0]["role"] == "director" and p["status"] == "review_pending"
               for p in twin["persons"])
    assert {p["works"][0]["asins"][0]["asin"] for p in twin["persons"]} == {"B0T5Q00A01", "B0T5Q00A02"}
    curtiz = query.query_director(conn, pm, "Michael Curtiz")              # 署名行写 “Curtiz, Michael”
    assert curtiz["matched_persons"] == 1
    p = curtiz["persons"][0]
    assert p["external_id"]["nconst"] == "nm0000301" and {a["alias"] for a in p["aliases"]} >= {"Curtiz, Michael", "Michael Curtiz"}
    assert p["works"][0]["tconst"] == "tt0000301" and {m["raw"] for m in p["director_mentions"]} == {"Curtiz, Michael", "Michael Curtiz"}
    assert query.query_director(conn, pm, "Nobody Here")["persons"] == []


def test_command_line_entry_points_print_json(conn, ran, capsys):
    w, pm = ran
    assert query.main(["director", "Twin Dir", "--pmap", pm]) == 0
    assert json.loads(capsys.readouterr().out)["matched_persons"] == 2
    assert check.main(["--pmap", pm]) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []
    assert report.main(["summary", "--pmap", pm]) == 0
    assert json.loads(capsys.readouterr().out)["person_mapping_version_id"] == pm


# ---------------------------------------------------------------- 导出

def test_export_files_match_the_database_have_recorded_hashes_and_no_html_or_review_text(conn, ran, tmp_path):
    w, pm = ran
    ex = report.examples(conn, pm)
    files = report.export_files(conn, pm, tmp_path / "exp", ex)
    by = {f["path"].split("/")[-1]: f for f in files}
    assert set(by) == {"persons.csv", "aliases.csv", "movie_person.csv", "decisions_sample.csv", "t4_upgrade_hints.csv",
                       "examples.md"}
    for name, f in by.items():
        data = (tmp_path / "exp" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["bytes"]
        text = data.decode()
        assert "<html" not in text.lower() and "<body" not in text.lower()
    assert by["persons.csv"]["rows"] == one(conn, "SELECT count(*) FROM task5.person")
    assert by["aliases.csv"]["rows"] == one(conn, "SELECT count(*) FROM task5.person_alias")
    assert by["movie_person.csv"]["rows"] == one(conn, "SELECT count(*) FROM task5.movie_person")
    rows = list(csv.DictReader((tmp_path / "exp" / "decisions_sample.csv").open(encoding="utf-8")))
    assert {r["kind"] for r in rows} == {"person", "external"} and len(rows) <= 200 * 6
    header = next(csv.reader((tmp_path / "exp" / "persons.csv").open(encoding="utf-8")))
    assert not {"review_text", "body", "summary"} & set(header)
    mp = list(csv.DictReader((tmp_path / "exp" / "movie_person.csv").open(encoding="utf-8")))
    assert any(r["role"] == "starring" and r["credit_order"] == "1" and r["order_conflict"] == "True" for r in mp)


# ---------------------------------------------------------------- 只读核对（AC14 的检查代码）

def test_verify_passes_on_a_consistent_run_and_does_not_write_task4(conn, ran):
    w, pm = ran
    before = check.verify(conn, pm)["t4_table_rows"]
    out = check.verify(conn, pm)
    assert check.problems(out) == [] and out["t4_table_rows"] == before
    assert out["mentions_missing"] == out["mentions_extra"] == out["mentions_status_mismatch"] == 0
    assert out["resolvable_without_person"] == 0 and out["movie_person_built"] and out["matched_products"] > 0
    assert out["auto_pair_decisions"] > 0 and out["auto_links_credits"] > 0
    assert out["migrations"][-1] == "401"


TAMPER = [
    ("DELETE FROM task5.mention_person WHERE mention_key = (SELECT min(mention_key) FROM task5.mention_person WHERE "
     "attribution = 'unattributed')", "resolvable_without_person"),
    ("UPDATE task5.person_mention SET status = 'ambiguous' WHERE mention_key = (SELECT min(mention_key) FROM "
     "task5.person_mention WHERE status = 'resolvable')", "mentions_status_mismatch"),
    ("UPDATE task5.link_candidate SET level = 'X4' WHERE link_candidate_id = (SELECT min(c.link_candidate_id) FROM "
     "task5.link_candidate c JOIN task5.link_decision d USING (link_candidate_id) WHERE d.tier = 'auto' AND c.route = 'credits')",
     "auto_links_credits_violating_r12"),
    ("UPDATE task5.person_decision SET features = jsonb_set(features, '{roles,overlap}', 'false') WHERE person_decision_id = "
     "(SELECT min(person_decision_id) FROM task5.person_decision WHERE tier = 'auto')", "auto_pair_violating_r19"),
    ("DELETE FROM task5.movie_person_source WHERE ctid = (SELECT min(ctid) FROM task5.movie_person_source)",
     "movie_person_without_source"),
    ("UPDATE task5.person_external_id SET external_id = (SELECT max(external_id) FROM task5.person_external_id) WHERE "
     "person_id = (SELECT min(person_id) FROM task5.person_external_id)", "nconst_shared_by_persons"),
]


@pytest.mark.parametrize("sql, key", TAMPER)
def test_verify_reports_a_tampered_database(conn, ran, sql, key):
    w, pm = ran
    conn.execute(sql)
    out = check.verify(conn, pm)
    assert out[key] > 0, out[key]
    assert check.problems(out)


def test_verify_checks_the_filmography_rule_r14_r15(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, links.film_products(), links.FILM_IMDB)
    pm = execute_resolve(conn, inputs_for(w), batch_size=3).person_mapping_version_id
    clean = check.verify(conn, pm)
    assert clean["auto_links_filmography"] == 5 and check.problems(clean) == []
    conn.execute("UPDATE task5.link_candidate SET evidence = jsonb_set(evidence, '{imdb_start_year}', '1900') WHERE "
                 "link_candidate_id = (SELECT min(c.link_candidate_id) FROM task5.link_candidate c JOIN task5.link_decision d "
                 "USING (link_candidate_id) WHERE c.route = 'filmography' AND d.tier = 'auto' AND "
                 "(c.evidence->>'title_year') IS NOT NULL)")
    assert check.verify(conn, pm)["auto_links_filmography_violating_r14"] == 1
    conn.execute("UPDATE task5.link_candidate SET anchor_link_decision_id = NULL WHERE route = 'filmography'")
    assert check.verify(conn, pm)["auto_links_filmography_violating_r14"] == 5
