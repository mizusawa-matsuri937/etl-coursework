"""任务 5 外部 ID（spec AC03：作品演职员，AC04：作品年表反查与任务 4 升级线索）。预期值按夹具手算（见 tests/fixtures/task5/README.md）。"""

from __future__ import annotations

import pytest
from task5 import links

from _task5_helpers import linked_status, make_world, run_links
from etl_task5 import external

pytestmark = pytest.mark.integration

X = "B0T5L00"


@pytest.fixture
def cw(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, links.credits_products(), links.CREDITS_IMDB)
    assert linked_status(conn, w) == {
        f"{X}A01": ("imdb_linked", "tt0001001"), f"{X}B01": ("imdb_linked", "tt0001002"),
        f"{X}C01": ("imdb_linked", "tt0001003"), f"{X}D01": ("page_only", None)}
    return w, run_links(conn, w, batch_size=2)


def _links(conn, route="credits"):
    """提及原值 → [(nconst, 等级, 判定档, 原因, 置信度, 是否跨角色)]。"""
    out: dict[str, list] = {}
    for raw, nc, level, tier, reason, conf, cross in conn.execute(
            "SELECT m.raw_value, c.nconst, c.level, d.tier, d.reason, d.confidence, c.cross_role "
            "FROM task5.link_candidate c JOIN task5.link_decision d USING (link_candidate_id) "
            "JOIN task5.person_mention m ON m.mention_key = c.mention_key WHERE c.route = %s "
            "ORDER BY m.mention_key, c.nconst", (route,)).fetchall():
        out.setdefault(raw, []).append((nc, level, tier, reason, float(conf), cross))
    return out


@pytest.mark.parametrize("raw, expected", [
    ("Ann Strict", [("nm0001005", "X1", "auto", "unique_in_credits", 0.95, False)]),                      # 严格键相同
    ("Bogart, Humphrey", [("nm0001002", "X1", "auto", "unique_in_credits", 0.95, False)]),                 # 倒序
    ("Alan Pakula", [("nm0001001", "X3", "auto", "unique_in_credits", 0.85, False)]),                      # 去中间缩写
    ("Tommy Jones", [("nm0001003", "X3", "auto", "unique_in_credits", 0.85, False)]),                      # 缺中间名
    ("Billy Bob Thorton", [("nm0001004", "X4", "review", "near_name", 0.0, False)]),                       # 拼写
    (f"Fran{links.BAD}ois Girard", [("nm0001006", "X4", "review", "near_name", 0.0, False)]),               # 替换字符
    ("Robert Young", [("nm0001007", "X1", "review", "multiple_in_credits", 0.0, False),
                      ("nm0001008", "X1", "review", "multiple_in_credits", 0.0, False)]),                  # 同作品两位
    ("Dee Cross", [("nm0001011", "X1", "review", "cross_role", 0.0, True)]),                               # 只在导演类别同名
])
def test_credits_name_levels_and_decisions(conn, cw, raw, expected):
    assert _links(conn)[raw] == expected


@pytest.mark.parametrize("raw", ["Andy Wachowski", "Zed Outside"])
def test_credits_no_candidate_for_renamed_or_homonym_outside_the_work(conn, cw, raw):
    """Wachowski 改名（页面旧名对 Lana/Lilly）与作品之外同名的 Zed Outside 都不经途径一关联。"""
    assert raw not in _links(conn)
    assert conn.execute("SELECT count(*) FROM task5.person_mention WHERE raw_value = %s", (raw,)).fetchone()[0] == 1


def test_credits_unlinked_product_has_no_candidates_and_every_decision_has_evidence(conn, cw):
    assert conn.execute("SELECT count(*) FROM task5.link_candidate WHERE mention_key LIKE %s",
                        (f"{X}D01:%",)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task5.link_candidate").fetchone()[0] == 13
    assert conn.execute("SELECT count(*) FROM task5.link_decision").fetchone()[0] == 13       # 未采用的也有决策行
    tiers = dict(conn.execute("SELECT tier, count(*) FROM task5.link_decision GROUP BY 1").fetchall())
    assert tiers == {"auto": 8, "review": 5}
    row = conn.execute("SELECT c.route, c.tconst, c.level, c.evidence, d.rule_version, d.thresholds, c.work_movie_id "
                       "FROM task5.link_candidate c JOIN task5.link_decision d USING (link_candidate_id) "
                       "JOIN task5.person_mention m USING (mention_key) WHERE m.raw_value = 'Tommy Jones'").fetchone()
    assert row[:3] == ("credits", "tt0001001", "X3") and row[4] == "t5r1" and row[5]["conf_x3"] == 0.85
    assert row[3]["table"] == "title_principal" and row[3]["ordinal"] == 2 and row[3]["credit_line_no"] >= 2
    assert row[3]["imdb_name"] == "Tommy Lee Jones" and row[3]["name_line_no"] >= 2 and row[6].startswith("mv_")


def test_credits_at_most_one_auto_per_mention(conn, cw):
    assert conn.execute("SELECT count(*) FROM (SELECT c.mention_key FROM task5.link_candidate c "
                        "JOIN task5.link_decision d USING (link_candidate_id) WHERE d.tier = 'auto' "
                        "GROUP BY 1 HAVING count(*) > 1) x").fetchone()[0] == 0


def test_credits_batch_rerun_adds_nothing(conn, cw):
    w, ctx = cw
    before = conn.execute("SELECT count(*) FROM task5.link_decision").fetchone()[0]
    assert external.link_credits(conn, ctx, batch_size=2).entities == 0
    assert conn.execute("SELECT count(*) FROM task5.link_decision").fetchone()[0] == before


# ---------------------------------------------------------------- AC04

F = "B0T5F00"


@pytest.fixture
def fw(conn, make_settings, tmp_path):
    w = make_world(conn, make_settings, tmp_path, links.film_products(), links.FILM_IMDB)
    status = linked_status(conn, w)
    assert status[f"{F}V01"] == ("imdb_linked", "tt0002001") and status[f"{F}V02"] == ("imdb_linked", "tt0002002")
    assert all(status[f"{F}U0{i}"][0] == "page_only" for i in range(1, 9))
    return w


def _film(conn):
    """商品 → [(原值, nconst, tconst, 判定档, 原因, 置信度, 标题键来源)]。"""
    out: dict[str, list] = {}
    for asin, raw, nc, tconst, tier, reason, conf, src in conn.execute(
            "SELECT m.asin, m.raw_value, c.nconst, c.tconst, d.tier, d.reason, d.confidence, c.title_key_source "
            "FROM task5.link_candidate c JOIN task5.link_decision d USING (link_candidate_id) "
            "JOIN task5.person_mention m ON m.mention_key = c.mention_key WHERE c.route = 'filmography' "
            "ORDER BY m.mention_key, c.nconst, c.tconst").fetchall():
        out.setdefault(asin, []).append((raw, nc, tconst, tier, reason, float(conf), src))
    return out


def test_filmography_unique_title_hit_links_to_the_verified_person(conn, fw):
    run_links(conn, fw, batch_size=3)
    film = _film(conn)
    assert film[f"{F}U01"] == [("Ava Verified", "nm0002001", "tt0002011", "auto", "filmography_title_match", 0.85,
                                "primary")]
    row = conn.execute("SELECT c.evidence, c.anchor_link_decision_id, c.matched_title_key, c.level, c.work_movie_id "
                       "FROM task5.link_candidate c WHERE c.route = 'filmography' AND c.tconst = 'tt0002011'").fetchone()
    assert row[0]["imdb_start_year"] == 1985 and row[0]["title_year"] == 1985 and row[0]["title_key_line_no"] >= 2
    assert row[2] == "lost voyage" and row[3] == "X1" and row[4].startswith("mv_")
    anchor = conn.execute("SELECT c.route, c.nconst, d.tier FROM task5.link_decision d JOIN task5.link_candidate c "
                          "USING (link_candidate_id) WHERE d.link_decision_id = %s", (row[1],)).fetchone()
    assert anchor == ("credits", "nm0002001", "auto")                         # N 是在途径一里核实的


def test_filmography_alias_only_title_match_has_lower_confidence(conn, fw):
    run_links(conn, fw)
    assert _film(conn)[f"{F}U02"] == [("Ava Verified", "nm0002001", "tt0002012", "auto", "filmography_title_match",
                                       0.80, "aka")]


def test_filmography_two_same_title_works_or_two_verified_people_are_review(conn, fw):
    run_links(conn, fw)
    film = _film(conn)
    assert [(r[1], r[2], r[3], r[4]) for r in film[f"{F}U03"]] == [
        ("nm0002001", "tt0002013", "review", "filmography_ambiguous"),
        ("nm0002001", "tt0002014", "review", "filmography_ambiguous")]
    assert [(r[1], r[2], r[3], r[4]) for r in film[f"{F}U04"]] == [      # 两个同名已核实人物的年表都命中
        ("nm0002001", "tt0002015", "review", "filmography_ambiguous"),
        ("nm0002003", "tt0002016", "review", "filmography_ambiguous")]


def test_filmography_never_verified_or_other_role_person_is_not_linked(conn, fw):
    run_links(conn, fw)
    film = _film(conn)
    assert f"{F}U05" not in film                    # Nora 只在 IMDb 里有年表，从未在别处核实
    assert f"{F}U06" not in film                    # 页面写导演，已核实的 Ava 只以演员身份核实
    assert conn.execute("SELECT count(*) FROM task5.person_mention WHERE asin = %s AND status = 'resolvable'",
                        (f"{F}U06",)).fetchone()[0] == 2


def test_filmography_year_three_apart_is_not_auto(conn, fw):
    run_links(conn, fw)
    assert [(r[3], r[4]) for r in _film(conn)[f"{F}U07"]] == [("review", "filmography_year_conflict")]


def test_filmography_two_mentions_on_one_work_write_a_t4_upgrade_hint_and_change_nothing_in_task4(conn, fw):
    tables = ["product_edition", "field_assertion", "source_record", "candidate", "merge_decision", "movie",
              "product_movie", "mapping_version", "product_match_status", "mapping_event", "cluster_check"]
    snap = lambda: {t: conn.execute(f"SELECT count(*), md5(string_agg(x::text, '|' ORDER BY x::text)) "
                                    f"FROM task4.{t} x").fetchone() for t in tables}
    before = snap()
    run_links(conn, fw)
    assert snap() == before                                                   # 任务 4 的表行数与内容不变
    assert [(r[1], r[2], r[3]) for r in _film(conn)[f"{F}U08"]] == [
        ("nm0002001", "tt0002020", "auto"), ("nm0002002", "tt0002020", "auto")]
    hints = conn.execute("SELECT asin, member_ordinal, tconst, mention_count, cardinality(link_decision_ids) "
                         "FROM task5.t4_upgrade_hint").fetchall()
    assert hints == [(f"{F}U08", 0, "tt0002020", 2, 2)]                       # 只有两位演员都指向同一作品的商品


def test_filmography_collection_member_title_hit_is_attributed_to_the_member(conn, fw):
    run_links(conn, fw)
    rows = _film(conn)[f"{F}K01"]
    assert [(r[1], r[2], r[3]) for r in rows] == [("nm0002001", "tt0002021", "auto")]
    ev = conn.execute("SELECT evidence FROM task5.link_candidate WHERE tconst = 'tt0002021' AND route = 'filmography'"
                      ).fetchone()[0]
    assert ev["member_ordinal"] == 2                                          # 命中第二个成员 Quiet Hall


def test_filmography_runs_after_credits_and_records_the_verified_set_hash(conn, fw):
    from _task5_helpers import start5
    from etl_task5 import mentions
    from etl_task5.run import Task5InputError
    ctx = start5(conn, fw)
    mentions.extract_mentions(conn, ctx, batch_size=50)
    with pytest.raises(Task5InputError, match="途径一"):
        external.link_filmography(conn, ctx, batch_size=50)
    external.link_credits(conn, ctx, batch_size=50)
    external.link_filmography(conn, ctx, batch_size=50)
    h = conn.execute("SELECT params->>'verified_hash' FROM task5.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0]
    assert h == external.verified_hash(external.verified_set(conn, ctx)) and len(h) == 16
    verified = external.verified_set(conn, ctx)
    assert verified[("actor", "ava verified")] == ["nm0002001", "nm0002003"]
    assert verified[("actor", "bea verified")] == ["nm0002002"]
    assert ("director", "ava verified") not in verified
