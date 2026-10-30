"""任务 5 归并、迭代、人物与人工纠正（spec AC05：作品内与跨作品，AC06：迭代与特征开关，AC07：标识、别名、版本与复核）。预期值按夹具手算。"""

from __future__ import annotations

import csv

import pytest
from task5 import people as base_people, resolve as fx

from _task5_helpers import World, inputs_for, linked_status, make_world
from etl_task5 import review
from etl_task5.cluster import alias_types, diff_versions
from etl_task5.rules import DEFAULT_RULES, RULESETS
from etl_task5.runner import execute_resolve

pytestmark = pytest.mark.integration

R = "B0T5R00"


def run(conn, world: World, rules=None, **over):
    return execute_resolve(conn, inputs_for(world, **over), rules, batch_size=3)


def mk(conn, asin, raw):
    """商品里某个原始署名的提及键（夹具里每个商品内署名不重复）。"""
    return conn.execute("SELECT mention_key FROM task5.person_mention WHERE asin = %s AND raw_value = %s",
                        (asin, raw)).fetchone()[0]


def person(conn, pmap, mkey):
    return conn.execute("SELECT person_id FROM task5.mention_person WHERE person_mapping_version_id = %s AND "
                        "mention_key = %s", (pmap, mkey)).fetchone()[0]


def person_row(conn, pmap, pid):
    return conn.execute("SELECT status, has_external_id, mention_count, work_count FROM task5.person "
                        "WHERE person_mapping_version_id = %s AND person_id = %s", (pmap, pid)).fetchone()


def pair_decision(conn, a, b):
    """两个提及所在组的候选的最终判定 (档, 原因, 否决, 轮次)。"""
    ga = conn.execute("SELECT group_key FROM task5.group_member WHERE mention_key = %s", (a,)).fetchone()[0]
    gb = conn.execute("SELECT group_key FROM task5.group_member WHERE mention_key = %s", (b,)).fetchone()[0]
    lo, hi = sorted((ga, gb))
    return conn.execute(
        "SELECT d.tier, d.reason, d.veto, d.round FROM task5.person_candidate c JOIN task5.person_decision d "
        "USING (person_candidate_id) WHERE c.group_a = %s AND c.group_b = %s ORDER BY d.round DESC LIMIT 1",
        (lo, hi)).fetchone()


@pytest.fixture
def cases(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, fx.case_products(), fx.CASES_IMDB)
    status = linked_status(conn, w)
    linked = {a for a, (s, _) in status.items() if s == "imdb_linked"}
    assert linked == {f"{R}{x}" for x in ("E01", "E02", "F01", "G01", "G02")}, status
    return w, run(conn, w)


# ---------------------------------------------------------------- AC05

def test_ac05_same_work_two_versions_and_director_who_acts_are_one_person_each(conn, cases):
    w, r = cases
    pm = r.person_mapping_version_id
    a1, a2 = mk(conn, f"{R}A01", "Dana Same"), mk(conn, f"{R}A02", "Dana Same")
    assert person(conn, pm, a1) == person(conn, pm, a2)
    assert person_row(conn, pm, person(conn, pm, a1)) == ("page_only", False, 2, 1)
    d, a = mk(conn, f"{R}B01", "Orson Dual"), conn.execute(
        "SELECT mention_key FROM task5.person_mention WHERE asin = %s AND raw_value = 'Orson Dual' AND role_class = 'actor'",
        (f"{R}B01",)).fetchone()[0]
    assert person(conn, pm, d) == person(conn, pm, a) and d != a
    roles = conn.execute("SELECT role_classes FROM task5.person_group g JOIN task5.group_member m USING (group_key) "
                         "WHERE m.mention_key = %s", (d,)).fetchone()[0]
    assert roles == ["actor", "director"]
    assert conn.execute("SELECT basis FROM task5.mention_person WHERE mention_key = %s", (d,)).fetchone()[0] == "within_work"


def test_ac05_same_name_without_shared_collaborator_stays_two_review_pending_people(conn, cases):
    w, r = cases
    pm = r.person_mapping_version_id
    a, b = mk(conn, f"{R}C01", "Sam Solo"), mk(conn, f"{R}C02", "Sam Solo")
    pa, pb = person(conn, pm, a), person(conn, pm, b)
    assert pa != pb and person_row(conn, pm, pa)[0] == person_row(conn, pm, pb)[0] == "review_pending"
    assert pair_decision(conn, a, b)[:3] == ("review", "same_name_no_corroboration", [])
    assert conn.execute("SELECT pending_candidates FROM task5.person WHERE person_mapping_version_id = %s AND "
                        "person_id = %s", (pm, pa)).fetchone()[0] == 1


def test_ac05_shared_collaborators_merge_the_same_name_actor(conn, cases):
    w, r = cases
    pm = r.person_mapping_version_id
    for raw in ("Cy Match", "Co Star", "Co Two"):
        a, b = mk(conn, f"{R}D01", raw), mk(conn, f"{R}D02", raw)
        assert person(conn, pm, a) == person(conn, pm, b), raw
        assert pair_decision(conn, a, b)[:2] == ("auto", "co_credit")
        assert conn.execute("SELECT basis FROM task5.mention_person WHERE mention_key = %s", (a,)).fetchone()[0] == "co_credit"
    assert person_row(conn, pm, person(conn, pm, mk(conn, f"{R}D01", "Cy Match")))[2:] == (2, 2)


def test_ac05_different_external_ids_and_era_conflict_are_hard_vetoes(conn, cases):
    w, r = cases
    pm = r.person_mapping_version_id
    a, b = mk(conn, f"{R}E01", "Robert Young"), mk(conn, f"{R}E02", "Robert Young")
    assert person(conn, pm, a) != person(conn, pm, b)
    assert person_row(conn, pm, person(conn, pm, a))[1] and person_row(conn, pm, person(conn, pm, b))[1]
    tier, reason, veto, _ = pair_decision(conn, a, b)
    assert (tier, reason, veto) == ("reject", "different_external_id", ["different_external_id"])
    late, early = mk(conn, f"{R}F01", "Late Born"), mk(conn, f"{R}F02", "Late Born")
    assert person(conn, pm, late) != person(conn, pm, early)
    assert pair_decision(conn, late, early)[:3] == ("reject", "era_conflict", ["era_conflict"])


def test_ac05_chain_never_produces_a_person_with_two_external_ids(conn, cases):
    w, r = cases
    pm = r.person_mapping_version_id
    y, z, x = (mk(conn, f"{R}{c}", "Chain Name") for c in ("G01", "G02", "G03"))
    py, pz, px = (person(conn, pm, m) for m in (y, z, x))
    assert py != pz and px in (py, pz)                                    # X 只并入其中一个 nconst，另一条边被拦下
    assert pair_decision(conn, y, z)[:2] == ("reject", "different_external_id")
    blocked = conn.execute("SELECT conflict, action, resolved_by FROM task5.cluster_check WHERE "
                           "person_mapping_version_id = %s", (pm,)).fetchall()
    assert blocked == [("two_external_ids", "edge_blocked", "system")]
    assert conn.execute("SELECT count(*) FROM (SELECT person_id FROM task5.person_external_id WHERE "
                        "person_mapping_version_id = %s GROUP BY 1 HAVING count(*) > 1) x", (pm,)).fetchone()[0] == 0
    edge = conn.execute("SELECT d.tier, d.reason FROM task5.person_decision d WHERE d.reason = 'edge_blocked'").fetchall()
    assert edge == [("review", "edge_blocked")]                                   # 被拦下的边按复核计，不进簇
    assert conn.execute("SELECT count(*) FROM task5.person_mapping_version WHERE person_mapping_version_id = %s "
                        "AND counts->>'rounds' IS NOT NULL", (pm,)).fetchone()[0] == 1


def test_ac05_initial_and_full_first_name_are_not_merged(conn, cases):
    w, r = cases
    pm = r.person_mapping_version_id
    a, b = mk(conn, f"{R}H01", "J. Smith"), mk(conn, f"{R}H02", "John Smith")
    assert person(conn, pm, a) != person(conn, pm, b)
    assert pair_decision(conn, a, b)[:2] == ("reject", "weak_name_only")


def test_ac05_every_resolvable_mention_has_exactly_one_person_and_every_candidate_a_decision(conn, cases):
    w, r = cases
    pm = r.person_mapping_version_id
    assert conn.execute("SELECT count(*) FROM task5.person_mention m WHERE status = 'resolvable' AND NOT EXISTS "
                        "(SELECT 1 FROM task5.mention_person p WHERE p.mention_key = m.mention_key AND "
                        "p.person_mapping_version_id = %s)", (pm,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task5.mention_person WHERE person_mapping_version_id = %s", (pm,)).fetchone()[0] \
        == conn.execute("SELECT count(*) FROM task5.person_mention WHERE status = 'resolvable'").fetchone()[0]
    assert conn.execute("SELECT count(*) FROM task5.person_candidate c WHERE NOT EXISTS (SELECT 1 FROM "
                        "task5.person_decision d WHERE d.person_candidate_id = c.person_candidate_id)").fetchone()[0] == 0
    counts = conn.execute("SELECT counts FROM task5.person_mapping_version WHERE person_mapping_version_id = %s",
                          (pm,)).fetchone()[0]
    assert counts["persons"] < counts["mentions_resolvable"] and counts["persons"] == conn.execute(
        "SELECT count(*) FROM task5.person WHERE person_mapping_version_id = %s", (pm,)).fetchone()[0]


# ---------------------------------------------------------------- AC06

def _iter_world(conn, make_settings, tmp_path, reverse=False):
    products = fx.iter_products()
    w = make_world(conn, make_settings, tmp_path, products[::-1] if reverse else products, fx.ITER_IMDB,
                   name="r" if reverse else "a")
    assert {s for s, _ in linked_status(conn, w).values()} == {"imdb_linked"}
    return w, run(conn, w)


def test_ac06_iteration_takes_three_rounds_and_b_merges_only_after_a_is_corroborated(conn, make_settings, tmp_path):
    w, r = _iter_world(conn, make_settings, tmp_path)
    pm = r.person_mapping_version_id
    rows = conn.execute("SELECT round, new_auto_edges, new_merged_persons, blocked_edges FROM task5.iteration_round "
                        "WHERE person_mapping_version_id = %s ORDER BY round", (pm,)).fetchall()
    assert rows == [(1, 2, 1, 0), (2, 1, 1, 0), (3, 0, 0, 0)]
    ap, aq, ar = (mk(conn, f"B0T5I00{c}01", "Ann Alpha") for c in "PQR")
    assert person(conn, pm, ap) == person(conn, pm, aq) == person(conn, pm, ar)
    bq, br = mk(conn, "B0T5I00Q01", "Bea Bee"), mk(conn, "B0T5I00R01", "Bea Bee")
    assert person(conn, pm, bq) == person(conn, pm, br) != person(conn, pm, ap)
    assert pair_decision(conn, aq, ar)[:2] == ("review", "same_name_no_corroboration")        # Q 与 R 只靠 B，第 1 轮不合并
    history = conn.execute(
        "SELECT d.round, d.tier, d.features->'collaborators' FROM task5.person_decision d JOIN task5.person_candidate c "
        "USING (person_candidate_id) JOIN task5.group_member ga ON ga.group_key = c.group_a "
        "JOIN task5.group_member gb ON gb.group_key = c.group_b WHERE ga.mention_key = %s AND gb.mention_key = %s "
        "ORDER BY d.round", (bq, br) if bq < br else (bq, br)).fetchall()
    assert [h[:2] for h in history] == [(1, "review"), (2, "auto")]                          # 判定档变化才新增一行
    assert history[0][2]["cluster"] == [] and len(history[1][2]["cluster"]) == 1             # 第 2 轮 A 成了已合成的共同合作者
    basis = dict(conn.execute("SELECT mention_key, basis || ':' || coalesce(round::text, '') FROM task5.mention_person "
                              "WHERE person_mapping_version_id = %s AND mention_key = ANY(%s)", (pm, [bq, br, aq])).fetchall())
    assert basis[bq] == basis[br] == "co_credit:2" and basis[aq] == "co_credit:1"


def test_ac06_two_runs_with_shuffled_input_give_identical_people_and_ids(conn, make_settings, tmp_path):
    def snapshot(pm):
        return conn.execute("SELECT mention_key, person_id FROM task5.mention_person WHERE person_mapping_version_id = %s "
                            "ORDER BY 1", (pm,)).fetchall()
    first = snapshot(_iter_world(conn, make_settings, tmp_path)[1].person_mapping_version_id)
    second = snapshot(_iter_world(conn, make_settings, tmp_path, reverse=True)[1].person_mapping_version_id)
    assert first == second and len(first) == 16


@pytest.fixture
def switch_rules():
    RULESETS["t5r2"] = DEFAULT_RULES.with_version("t5r2", use_rarity=True)
    RULESETS["t5r3"] = DEFAULT_RULES.with_version("t5r3", use_publisher=True)
    yield
    RULESETS.pop("t5r2"), RULESETS.pop("t5r3")


def test_ac06_features_are_recorded_with_switches_off_and_only_rare_name_pair_upgrades_when_on(
        conn, make_settings, tmp_path, switch_rules):
    w = make_world(conn, make_settings, tmp_path, fx.switch_products(), base_people.IMDB)
    off = run(conn, w).person_mapping_version_id
    a, b = mk(conn, "B0T5S00A01", "Rora Rare"), mk(conn, "B0T5S00A02", "Rora Rare")
    p, q = mk(conn, "B0T5S00B01", "Pam Pub"), mk(conn, "B0T5S00B02", "Pam Pub")
    assert person(conn, off, a) != person(conn, off, b) and person(conn, off, p) != person(conn, off, q)
    feats = conn.execute(
        "SELECT d.tier, d.features FROM task5.person_decision d JOIN task5.person_candidate c USING (person_candidate_id) "
        "JOIN task5.group_member ga ON ga.group_key = c.group_a WHERE ga.mention_key = %s AND d.rule_version = 't5r1'",
        (a,)).fetchone()
    assert feats[0] == "review" and feats[1]["rarity"]["rare_both"] is True
    assert feats[1]["rarity"]["a"] == {"page_works": 2, "imdb_people": 0}
    assert feats[1]["feature_switches"] == {"use_rarity": False, "use_publisher": False,
                                            "tier_if_enabled": {"rarity": "auto", "publisher": "review"}}
    pub = conn.execute(
        "SELECT d.score, d.features FROM task5.person_decision d JOIN task5.person_candidate c USING (person_candidate_id) "
        "JOIN task5.group_member ga ON ga.group_key = c.group_a WHERE ga.mention_key = %s AND d.rule_version = 't5r1'",
        (p,)).fetchone()
    assert pub[1]["publishers"]["overlap"] == ["pub house"] and float(pub[0]) == 4.0       # 关闭时发行商不计分
    on = run(conn, w, rule_version="t5r2").person_mapping_version_id                        # 稀有名 + 同名合作者
    assert person(conn, on, a) == person(conn, on, b) and person(conn, on, p) != person(conn, on, q)
    assert conn.execute(
        "SELECT d.tier, d.reason FROM task5.person_decision d JOIN task5.person_candidate c USING (person_candidate_id) "
        "JOIN task5.group_member ga ON ga.group_key = c.group_a WHERE ga.mention_key = %s AND d.rule_version = 't5r2'",
        (a,)).fetchone() == ("auto", "rare_name_co_credit")
    pub_on = run(conn, w, rule_version="t5r3").person_mapping_version_id                   # 发行商相同、没有合作者：仍不合并
    assert person(conn, pub_on, p) != person(conn, pub_on, q)
    score = conn.execute(
        "SELECT d.score FROM task5.person_decision d JOIN task5.person_candidate c USING (person_candidate_id) "
        "JOIN task5.group_member ga ON ga.group_key = c.group_a WHERE ga.mention_key = %s AND d.rule_version = 't5r3'",
        (p,)).fetchone()[0]
    assert float(score) == 5.0                                                              # 合作者分 +1


# ---------------------------------------------------------------- AC07

def test_ac07_person_ids_are_stable_opaque_and_a_second_run_is_reused(conn, cases):
    w, r = cases
    assert r.status == "succeeded"
    pm = r.person_mapping_version_id
    ids = [x[0] for x in conn.execute("SELECT person_id FROM task5.person WHERE person_mapping_version_id = %s", (pm,))]
    assert ids and all(i.startswith("ps_") and len(i) == 19 for i in ids)
    nconsts = [x[0] for x in conn.execute("SELECT external_id FROM task5.person_external_id")]
    assert not any(n in i or n[2:] in i for i in ids for n in nconsts)
    counts = lambda: [conn.execute(f"SELECT count(*) FROM task5.{t}").fetchone()[0] for t in (
        "person", "mention_person", "person_candidate", "person_decision", "link_decision", "person_alias")]
    before = counts()
    second = run(conn, w)
    assert (second.status, second.person_mapping_version_id) == ("reused", pm) and counts() == before
    anchors = dict(conn.execute("SELECT anchor_key, person_id FROM task5.person_identity").fetchall())
    assert anchors["imdb:nm0003011"] in ids
    assert all(anchors[k] == pid for k, pid in conn.execute(
        "SELECT anchor_key, person_id FROM task5.person WHERE person_mapping_version_id = %s", (pm,)).fetchall())


def test_ac07_aliases_list_every_raw_form_with_types_counts_and_the_imdb_primary_name(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path)
    pm = run(conn, w).person_mapping_version_id
    pid = conn.execute("SELECT person_id FROM task5.person_external_id WHERE person_mapping_version_id = %s AND "
                       "external_id = 'nm0000302'", (pm,)).fetchone()[0]
    rows = {a: (types, src, n) for a, types, src, n in conn.execute(
        "SELECT alias, alias_types, source, occurrences FROM task5.person_alias WHERE person_mapping_version_id = %s "
        "AND person_id = %s", (pm, pid)).fetchall()}
    assert rows == {"Bogart, Humphrey": (["reversed_order"], "page", 1),
                    "Humphrey Bogart": (["identical", "imdb_primary"], "page", 2)}
    tj = conn.execute("SELECT person_id, canonical_name, canonical_source FROM task5.person WHERE person_mapping_version_id "
                      "= %s AND anchor_key = 'imdb:nm0000306'", (pm,)).fetchone()
    assert tj[1:] == ("Tommy Lee Jones", "imdb:nm0000306")
    types = dict(conn.execute("SELECT alias, alias_types FROM task5.person_alias WHERE person_mapping_version_id = %s "
                              "AND person_id = %s", (pm, tj[0])).fetchall())
    assert types["Tommy Jones"] == ["middle_name"] and types["Tommy Lee Jones"] == ["identical", "imdb_primary"]
    # 页面人物（没有外部 ID）的规范姓名取最常见写法，并列取字典序最小
    page = conn.execute("SELECT canonical_name, canonical_source FROM task5.person WHERE NOT has_external_id AND "
                        "person_mapping_version_id = %s AND canonical_name = 'Sylvester Stallone'", (pm,)).fetchone()
    assert page[1].startswith("mention:B0T5M00D01")


@pytest.mark.parametrize("raw, canonical, expected", [
    ("Humphrey Bogart", "Humphrey Bogart", ["identical"]),
    ("humphrey bogart", "Humphrey Bogart", ["case_variant"]),
    ("Ciarán Hinds", "Ciaran Hinds", ["accent_variant"]),
    ("Bogart, Humphrey", "Humphrey Bogart", ["reversed_order"]),
    ("Alan J. Pakula", "Alan Pakula", ["middle_initial"]),
    ("Tommy Jones", "Tommy Lee Jones", ["middle_name"]),
    ("George 'Gabby' Hayes", "George Hayes", ["nickname"]),
    ("Yu-mi Kim", "Kim Yu-mi", ["token_order"]),
    ("Lon Chaney Jr.", "Lon Chaney", ["suffix"]),
    ("Fran�ois Girard", "François Girard", ["encoding_damaged", "other"]),
])
def test_ac07_alias_types(raw, canonical, expected):
    assert alias_types(raw, canonical) == expected


def test_ac07_queue_is_sorted_by_impact_and_import_creates_a_new_version_with_human_decisions(
        conn, cases, tmp_path):
    w, r = cases
    old = r.person_mapping_version_id
    path = tmp_path / "review_queue.csv"
    n = review.export_queue(conn, old, path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert n == len(rows) >= 2 and list(rows[0]) == list(review.COLUMNS)
    impacts = [int(x["impact"]) for x in rows]
    assert impacts == sorted(impacts, reverse=True) and [int(x["queue_rank"]) for x in rows] == list(range(1, n + 1))
    with pytest.raises(FileExistsError):
        review.export_queue(conn, old, path)
    solo = next(x for x in rows if x["left_raw"] == "Sam Solo")
    assert (solo["kind"], solo["reason"], solo["impact"]) == ("person", "same_name_no_corroboration", "1")
    solo["decision"], solo["note"] = "must_link", "same actor"
    with path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=review.COLUMNS)
        wr.writeheader()
        wr.writerows(rows)
    human = review.read_queue(path)
    assert len(human) == 1 and human[0].kind == "must_link"
    cy_a, cy_b = mk(conn, f"{R}D01", "Cy Match"), mk(conn, f"{R}D02", "Cy Match")
    human.append(review.HumanRow("cannot_link", cy_a, cy_b, "different people"))
    assert review.import_decisions(conn, w.fx.eid, human, annotator="tester", file_sha256="0" * 64) == 2
    assert {x[0] for x in conn.execute("SELECT set_hash FROM task5.human_decision")} != {"none", "pending"}
    new = run(conn, w).person_mapping_version_id
    assert new != old
    sa, sb = mk(conn, f"{R}C01", "Sam Solo"), mk(conn, f"{R}C02", "Sam Solo")
    assert person(conn, new, sa) == person(conn, new, sb) and person(conn, old, sa) != person(conn, old, sb)   # 原版本仍可查询
    assert person(conn, new, cy_a) != person(conn, new, cy_b) and person(conn, old, cy_a) == person(conn, old, cy_b)
    assert conn.execute("SELECT basis FROM task5.mention_person WHERE person_mapping_version_id = %s AND mention_key = %s",
                        (new, sa)).fetchone()[0] == "human"
    changed = {row[0]: row for row in diff_versions(conn, old, new)}
    assert sa in changed or sb in changed
    assert (cy_a in changed or cy_b in changed) and all(row[3] for row in changed.values())      # 每个变化都带原因
    assert conn.execute("SELECT parent_version_id FROM task5.person_mapping_version WHERE person_mapping_version_id = %s",
                        (new,)).fetchone()[0] == old
    assert conn.execute("SELECT count(*) FROM task5.mapping_event WHERE person_mapping_version_id = %s", (new,)).fetchone()[0] > 0


def test_ac07_must_link_of_two_different_external_ids_rejects_the_whole_file(conn, cases):
    w, r = cases
    a, b = mk(conn, f"{R}E01", "Robert Young"), mk(conn, f"{R}E02", "Robert Young")
    ok = review.HumanRow("must_link", mk(conn, f"{R}C01", "Sam Solo"), mk(conn, f"{R}C02", "Sam Solo"))
    bad = review.HumanRow("must_link", a, b)
    ghost = review.HumanRow("must_link", "B0T5R00C01:byline:9:9", a)
    for rows, pattern in (([ok, bad], "两个不同的 IMDb 人物"), ([ok, ghost], "不存在"),
                          ([ok, review.HumanRow("must_link", a, "imdb:nm9999999")], "nm9999999")):
        with pytest.raises(review.ReviewInputError, match=pattern):
            review.import_decisions(conn, w.fx.eid, rows, annotator="tester", file_sha256="1" * 64)
        assert conn.execute("SELECT count(*) FROM task5.human_decision").fetchone()[0] == 0     # 不写部分行
