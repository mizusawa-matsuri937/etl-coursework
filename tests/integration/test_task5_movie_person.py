"""任务 5 MoviePerson（spec AC08：角色、署名顺序、来源、合集归属）。预期值按夹具手算，见 tests/fixtures/task5/README.md。"""

from __future__ import annotations

import pytest
from task5 import movie_person as mpfx, people as base_people, resolve as resolve_fx

from _task5_helpers import linked_status, make_world, inputs_for
from etl_task5 import movie_person
from etl_task5.runner import execute_resolve

pytestmark = pytest.mark.integration

P5 = "B0T5P00"


def run(conn, world, **kw):
    return execute_resolve(conn, inputs_for(world), batch_size=3, **kw)


def pid_of(conn, pmap, asin, raw):
    return conn.execute("SELECT mp.person_id FROM task5.mention_person mp JOIN task5.person_mention m "
                        "ON m.mention_key = mp.mention_key WHERE mp.person_mapping_version_id = %s AND m.asin = %s "
                        "AND m.raw_value = %s ORDER BY m.mention_key LIMIT 1", (pmap, asin, raw)).fetchone()[0]


def relations(conn, pmap, movie, pid):
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT role, credit_order, order_conflict, source_labels, mention_count FROM task5.movie_person "
        "WHERE person_mapping_version_id = %s AND movie_id = %s AND person_id = %s", (pmap, movie, pid)).fetchall()}


@pytest.fixture
def mp(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, mpfx.mp_products(), mpfx.MP_IMDB)
    st = linked_status(conn, w)
    assert st[f"{P5}W01"][0] == st[f"{P5}W02"][0] == "imdb_linked" and st[f"{P5}W01"][1] == "tt0005001", st
    r = run(conn, w)
    movie = dict(conn.execute("SELECT tconst, movie_id FROM task4.movie WHERE mapping_version_id = %s AND "
                              "tconst IS NOT NULL", (w.mapping,)).fetchall())
    return w, r, movie["tt0005001"], movie["tt0005002"]


def test_ac08_byline_actor_has_starring_and_cast_and_detail_only_actor_has_only_cast(conn, mp):
    w, r, m1, _ = mp
    pm = r.person_mapping_version_id
    ann, cy = pid_of(conn, pm, f"{P5}W01", "Ann Star"), pid_of(conn, pm, f"{P5}W01", "Cy Support")
    assert set(relations(conn, pm, m1, ann)) == {"starring", "cast"}
    assert set(relations(conn, pm, m1, cy)) == {"cast"}


def test_ac08_director_who_also_acts_has_two_roles_in_the_same_work(conn, mp):
    w, r, m1, _ = mp
    pm = r.person_mapping_version_id
    dir1 = pid_of(conn, pm, f"{P5}W01", "Dir One")
    rel = relations(conn, pm, m1, dir1)
    assert set(rel) == {"director", "cast"}
    assert rel["director"][0] == 1 and rel["cast"][0] is None            # 详情要点来源：署名顺序 NULL
    assert conn.execute("SELECT multi_role FROM (SELECT count(DISTINCT CASE WHEN role = 'starring' THEN 'cast' "
                        "ELSE role END) > 1 AS multi_role FROM task5.movie_person WHERE person_mapping_version_id = %s "
                        "AND person_id = %s GROUP BY movie_id) t", (pm, dir1)).fetchone()[0]
    assert r.counts["movie_person"]["multi_role_persons"] == 1


def test_ac08_credit_order_comes_from_byline_rank_and_detail_bullets_only_keeps_the_list_position(conn, mp):
    w, r, m1, _ = mp
    pm = r.person_mapping_version_id
    cy = pid_of(conn, pm, f"{P5}W01", "Cy Support")
    assert relations(conn, pm, m1, cy)["cast"][0] is None
    pos = conn.execute("SELECT s.byline_rank, s.list_position FROM task5.movie_person_source s WHERE "
                       "s.person_mapping_version_id = %s AND s.person_id = %s AND s.role = 'cast'", (pm, cy)).fetchall()
    assert pos == [(None, 3)]                                           # 详情要点第 3 位，只作定位
    dir1 = pid_of(conn, pm, f"{P5}W01", "Dir One")
    assert relations(conn, pm, m1, dir1)["director"][:2] == (1, False)  # 三个来源的署名顺序都是 1，无冲突


def test_ac08_order_conflict_across_versions_takes_the_minimum_and_is_flagged(conn, mp):
    w, r, m1, _ = mp
    pm = r.person_mapping_version_id
    ann, bea = pid_of(conn, pm, f"{P5}W01", "Ann Star"), pid_of(conn, pm, f"{P5}W01", "Bea Lead")
    rel = relations(conn, pm, m1, ann)
    assert rel["starring"][:2] == (1, True) and rel["cast"][:2] == (1, True)    # W01 第 1、W02 第 2、K01 第 1
    assert relations(conn, pm, m1, bea)["starring"][:2] == (1, True)           # W01 第 2、W02 第 1
    cy = pid_of(conn, pm, f"{P5}W01", "Cy Support")
    assert relations(conn, pm, m1, cy)["cast"][1] is False
    issues = conn.execute("SELECT count(*) FROM task5.quality_issue WHERE issue_code = 'order_conflict'").fetchone()[0]
    assert issues == 4 == r.counts["movie_person"]["order_conflicts"]          # Ann、Bea 各 starring 与 cast


def test_ac08_every_relation_lists_all_of_its_source_mentions_and_labels(conn, mp):
    w, r, m1, _ = mp
    pm = r.person_mapping_version_id
    ann = pid_of(conn, pm, f"{P5}W01", "Ann Star")
    rel = relations(conn, pm, m1, ann)
    assert rel["starring"][2] == ["byline:Actor", "collection:B0T5P00K01"] and rel["starring"][3] == 3
    assert rel["cast"][2] == ["byline:Actor", "collection:B0T5P00K01", "detail_bullets:Actors"] and rel["cast"][3] == 4
    srcs = conn.execute("SELECT asin FROM task5.movie_person_source WHERE person_mapping_version_id = %s AND "
                        "movie_id = %s AND person_id = %s AND role = 'cast' ORDER BY asin, mention_key",
                        (pm, m1, ann)).fetchall()
    assert [s[0] for s in srcs] == [f"{P5}K01", f"{P5}W01", f"{P5}W01", f"{P5}W02"]
    # 每条关系至少一个来源，来源提及都属于该人物
    assert conn.execute("SELECT count(*) FROM task5.movie_person m WHERE person_mapping_version_id = %s AND NOT EXISTS "
                        "(SELECT 1 FROM task5.movie_person_source s WHERE s.person_mapping_version_id = m.person_mapping_version_id "
                        "AND s.movie_id = m.movie_id AND s.person_id = m.person_id AND s.role = m.role)", (pm,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task5.movie_person m WHERE person_mapping_version_id = %s AND "
                        "mention_count <> (SELECT count(*) FROM task5.movie_person_source s WHERE "
                        "s.person_mapping_version_id = m.person_mapping_version_id AND s.movie_id = m.movie_id AND "
                        "s.person_id = m.person_id AND s.role = m.role)", (pm,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task5.movie_person_source s JOIN task5.mention_person mp ON "
                        "mp.person_mapping_version_id = s.person_mapping_version_id AND mp.mention_key = s.mention_key "
                        "WHERE s.person_mapping_version_id = %s AND mp.person_id <> s.person_id", (pm,)).fetchone()[0] == 0


def test_ac08_collection_actor_linked_to_a_member_goes_to_that_member_and_the_other_is_unattributed(conn, mp):
    w, r, m1, m2 = mp
    pm = r.person_mapping_version_id
    ann_in_k = conn.execute("SELECT mp.person_id, mp.attribution FROM task5.mention_person mp JOIN task5.person_mention m "
                            "USING (mention_key) WHERE mp.person_mapping_version_id = %s AND m.asin = %s AND "
                            "m.raw_value = 'Ann Star'", (pm, f"{P5}K01")).fetchone()
    assert ann_in_k[1] == "movie" and ann_in_k[0] == pid_of(conn, pm, f"{P5}W01", "Ann Star")
    dir2 = pid_of(conn, pm, f"{P5}K01", "Dir Two")
    rel = relations(conn, pm, m2, dir2)
    assert set(rel) == {"director"} and rel["director"][0] == 2 and "collection:B0T5P00K01" in rel["director"][2]
    zed, status = conn.execute("SELECT mp.person_id, mp.attribution FROM task5.mention_person mp JOIN task5.person_mention m "
                               "USING (mention_key) WHERE mp.person_mapping_version_id = %s AND m.raw_value = 'Zed Nobody'",
                               (pm,)).fetchone()
    assert status == "unattributed"
    assert conn.execute("SELECT count(*) FROM task5.person WHERE person_mapping_version_id = %s AND person_id = %s",
                        (pm, zed)).fetchone()[0] == 1                                    # 人物照常建立
    assert conn.execute("SELECT count(*) FROM task5.movie_person WHERE person_mapping_version_id = %s AND person_id = %s",
                        (pm, zed)).fetchone()[0] == 0                                    # 但没有关系
    assert r.counts["movie_person"]["unattributed_mentions"] == 1


def test_ac08_every_attributed_mention_has_a_relation_and_unattributed_or_other_mentions_have_none(conn, mp):
    w, r, *_ = mp
    pm = r.person_mapping_version_id
    n_attr = conn.execute("SELECT count(*) FROM task5.mention_person WHERE person_mapping_version_id = %s AND "
                          "attribution = 'movie'", (pm,)).fetchone()[0]
    in_sources = conn.execute("SELECT count(DISTINCT mention_key) FROM task5.movie_person_source WHERE "
                              "person_mapping_version_id = %s", (pm,)).fetchone()[0]
    assert in_sources == n_attr
    assert conn.execute("SELECT count(*) FROM task5.movie_person_source s JOIN task5.mention_person mp ON mp.mention_key = "
                        "s.mention_key AND mp.person_mapping_version_id = s.person_mapping_version_id WHERE "
                        "s.person_mapping_version_id = %s AND mp.attribution <> 'movie'", (pm,)).fetchone()[0] == 0


def test_ac08_second_run_is_reused_and_building_again_writes_nothing(conn, mp):
    w, r, *_ = mp
    pm = r.person_mapping_version_id
    before = conn.execute("SELECT (SELECT count(*) FROM task5.movie_person), (SELECT count(*) FROM task5.movie_person_source), "
                          "(SELECT count(*) FROM task5.quality_issue)").fetchone()
    again = run(conn, w)
    assert again.status == "reused" and again.person_mapping_version_id == pm
    out = movie_person.build_movie_person(conn, pm)
    assert out["reused"] is True and out["relations"] == r.counts["movie_person"]["relations"]
    assert conn.execute("SELECT (SELECT count(*) FROM task5.movie_person), (SELECT count(*) FROM task5.movie_person_source), "
                        "(SELECT count(*) FROM task5.quality_issue)").fetchone() == before


def test_ac08_failure_before_commit_leaves_no_movie_person_rows_and_the_rerun_completes(conn, mp):
    w, r, *_ = mp
    pm = r.person_mapping_version_id
    conn.execute("DELETE FROM task5.movie_person_source")
    conn.execute("DELETE FROM task5.movie_person")
    conn.execute("UPDATE task5.person_mapping_version SET counts = counts - 'movie_person'")

    def boom(stage, n):
        if stage == "movie_person":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        movie_person.build_movie_person(conn, pm, before_commit=boom)
    assert conn.execute("SELECT (SELECT count(*) FROM task5.movie_person), (SELECT count(*) FROM task5.movie_person_source)"
                        ).fetchone() == (0, 0)
    assert not movie_person.built(conn, pm)
    out = movie_person.build_movie_person(conn, pm)
    assert out["reused"] is False and out["relations"] == r.counts["movie_person"]["relations"]
    assert movie_person.built(conn, pm)


# ---------------------------------------------------------------- AC01、AC05 的夹具

def test_ac08_on_the_ac01_fixture_byline_actors_get_starring_and_cast_and_curtiz_directs(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path)
    r = run(conn, w)
    pm = r.person_mapping_version_id
    movie = conn.execute("SELECT movie_id FROM task4.movie WHERE mapping_version_id = %s AND tconst = 'tt0000301'",
                         (w.mapping,)).fetchone()[0]
    got = {raw: set(relations(conn, pm, movie, pid_of(conn, pm, "B0T5M00A01", raw)))
           for raw in ("Bogart, Humphrey", "Ingrid Bergman", "Claude Rains", "Curtiz, Michael")}
    assert got == {"Bogart, Humphrey": {"starring", "cast"}, "Ingrid Bergman": {"starring", "cast"},
                   "Claude Rains": {"cast"}, "Curtiz, Michael": {"director"}}
    assert relations(conn, pm, movie, pid_of(conn, pm, "B0T5M00A01", "Bogart, Humphrey"))["starring"][0] == 1
    # 合集 K01：Ridley Scott 归到 Alien、Cameron 归到 Aliens；Weaver 两个成员都有（review）与 Lance Henriksen 都是 unattributed
    unattr = {x[0] for x in conn.execute(
        "SELECT m.raw_value FROM task5.mention_person mp JOIN task5.person_mention m USING (mention_key) WHERE "
        "mp.person_mapping_version_id = %s AND mp.attribution = 'unattributed' AND m.asin = 'B0T5M00K01'", (pm,))}
    assert unattr == {"Sigourney Weaver", "Lance Henriksen"}
    assert r.counts["movie_person"]["unattributed_mentions"] == 2


def test_ac08_on_the_ac05_fixture_director_who_acts_in_the_same_work_has_two_roles(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, resolve_fx.case_products(), resolve_fx.CASES_IMDB)
    r = run(conn, w)
    pm = r.person_mapping_version_id
    movie = conn.execute("SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = 'B0T5R00B01' "
                         "AND relation_type = 'edition'", (w.mapping,)).fetchone()[0]
    rel = relations(conn, pm, movie, pid_of(conn, pm, "B0T5R00B01", "Orson Dual"))
    assert set(rel) == {"director", "starring", "cast"}
    assert rel["director"][0] == 1 and rel["starring"][0] == 1
    assert r.counts["movie_person"]["multi_role_persons"] >= 1
    assert base_people.products()          # 夹具模块可导入（AC01 的夹具在上一个用例使用）
