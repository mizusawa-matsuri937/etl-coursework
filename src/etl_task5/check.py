"""任务 5 只读核对（spec AC14；与 DAG 的 verify 步骤共用 verify 与 problems）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task5.check                              # 默认抽取的最新人物映射版本
    python -m etl_task5.check --pmap pmap0001
输出一段 JSON；全部一致时退出码 0，否则 1。整个核对在只读事务里进行，不写任何表。

核对内容：
- 每个参与匹配商品的导演、演员名字都有提及，提及的状态与按页面字段重新抽取的结果一致（互斥且完整）；
- 每个 resolvable 提及恰好属于一个人物，placeholder、ambiguous 提及不属于任何人物；
- 没有人物关联两个 nconst，也没有两个人物共用一个 nconst；
- 每个自动外部 ID 满足 R12（途径一）或 R14–R15（途径二），一条提及至多一个自动外部 ID；
- 每个自动合并满足 R19（N1、角色有交集、无硬冲突、合作者佐证足够）；
- 每条 MoviePerson 有来源提及，来源数与 mention_count 一致，归属到作品的提及都有关系；
- 任务 4 的表未被写入：口径是任务 4 映射版本记录的数量与重新数出来的一致（task4 表无法在运行前后对比时的替代），
  并输出各 task4 表的行数，运行手册里在运行前后各取一次对比。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from .mentions import extract_product
from .rules import ruleset

T4_TABLES = ("product_edition", "field_assertion", "source_record", "imdb_title_key", "candidate", "merge_decision",
             "movie_identity", "mapping_version", "movie", "product_movie", "product_match_status", "mapping_event",
             "cluster_check", "human_decision")


def latest_pmap(conn: psycopg.Connection, extraction_id: str) -> str:
    row = conn.execute("SELECT person_mapping_version_id FROM task5.person_mapping_version WHERE extraction_id = %s "
                       "ORDER BY created_at DESC, person_mapping_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"抽取 {extraction_id} 还没有人物映射版本")
    return row[0]


def verify(conn: psycopg.Connection, pmap: str) -> dict[str, Any]:
    """只读核对一个人物映射版本。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return _verify(conn, pmap)


def _verify(conn: psycopg.Connection, pmap: str) -> dict[str, Any]:
    m = conn.execute(
        "SELECT extraction_id, t4_mapping_version_id, parser_version, norm_version, rule_version, imdb_snapshot_id, "
        "human_set_hash, counts FROM task5.person_mapping_version WHERE person_mapping_version_id = %s", (pmap,)).fetchone()
    if m is None:
        raise RuntimeError(f"人物映射版本 {pmap} 不存在")
    eid, t4, pv, nv, rv, sid, human, counts = m
    rules = ruleset(rv)
    link_key = "|".join((t4, pv, nv, rv, sid))             # 外部 ID、组与候选的阶段键
    out: dict[str, Any] = {"person_mapping_version_id": pmap, "extraction_id": eid, "t4_mapping_version_id": t4,
                           "parser_version": pv, "norm_version": nv, "rule_version": rv, "imdb_snapshot_id": sid,
                           "human_set_hash": human}
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    t3 = one("SELECT t3_rule_version FROM task4.mapping_version WHERE mapping_version_id = %s", t4)

    # 提及：覆盖与状态
    asins = [r[0] for r in conn.execute(
        "SELECT asin FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s AND "
        "match_scope = 'matched' ORDER BY asin COLLATE \"C\"", (eid, t3)).fetchall()]
    out["matched_products"] = len(asins)
    missing = extra = mismatch = expected_total = 0
    for k in range(0, len(asins), 1000):
        chunk = asins[k:k + 1000]
        fields: dict[str, list] = {a: [] for a in chunk}
        for asin, section, ordinal, label, value, key in conn.execute(
                "SELECT asin, section, ordinal, label_raw, value_raw, field_key FROM task3.page_field WHERE "
                "extraction_id = %s AND parser_version = %s AND asin = ANY(%s) ORDER BY asin, section, ordinal",
                (eid, pv, chunk)).fetchall():
            fields[asin].append((section, ordinal, label, value, key))
        expected = {}
        for asin in chunk:
            found, _ = extract_product(asin, fields[asin])
            expected.update({x.key: x.status for x in found})
        actual = dict(conn.execute(
            "SELECT mention_key, status FROM task5.person_mention WHERE extraction_id = %s AND parser_version = %s "
            "AND norm_version = %s AND asin = ANY(%s)", (eid, pv, nv, chunk)).fetchall())
        expected_total += len(expected)
        missing += len(expected.keys() - actual.keys())
        extra += len(actual.keys() - expected.keys())
        mismatch += sum(1 for key, st in expected.items() if key in actual and actual[key] != st)
    out["mentions_expected"] = expected_total
    out["mentions_missing"], out["mentions_extra"], out["mentions_status_mismatch"] = missing, extra, mismatch
    out["mentions_by_status"] = dict(conn.execute(
        "SELECT status, count(*) FROM task5.person_mention WHERE extraction_id = %s AND parser_version = %s AND "
        "norm_version = %s GROUP BY 1 ORDER BY 1", (eid, pv, nv)).fetchall())
    scope = (eid, pv, nv)
    out["resolvable_without_person"] = one(
        "SELECT count(*) FROM task5.person_mention m WHERE m.extraction_id = %s AND m.parser_version = %s AND "
        "m.norm_version = %s AND m.status = 'resolvable' AND NOT EXISTS (SELECT 1 FROM task5.mention_person mp WHERE "
        "mp.person_mapping_version_id = %s AND mp.mention_key = m.mention_key)", *scope, pmap)
    out["non_resolvable_with_person"] = one(
        "SELECT count(*) FROM task5.mention_person mp JOIN task5.person_mention m ON m.extraction_id = mp.extraction_id "
        "AND m.parser_version = mp.parser_version AND m.norm_version = mp.norm_version AND m.mention_key = mp.mention_key "
        "WHERE mp.person_mapping_version_id = %s AND m.status <> 'resolvable'", pmap)
    out["mention_person_rows"] = one("SELECT count(*) FROM task5.mention_person WHERE person_mapping_version_id = %s", pmap)
    out["persons"] = one("SELECT count(*) FROM task5.person WHERE person_mapping_version_id = %s", pmap)
    out["persons_without_mentions"] = one(
        "SELECT count(*) FROM task5.person p WHERE p.person_mapping_version_id = %s AND NOT EXISTS (SELECT 1 FROM "
        "task5.mention_person mp WHERE mp.person_mapping_version_id = p.person_mapping_version_id AND "
        "mp.person_id = p.person_id)", pmap)

    # 外部 ID：每人至多一个 nconst、一个 nconst 至多一个人物
    out["persons_with_two_nconst"] = one(
        "SELECT count(*) FROM (SELECT mp.person_id FROM task5.mention_person mp JOIN task5.link_candidate c ON "
        "c.extraction_id = mp.extraction_id AND c.stage_key = %s AND c.mention_key = mp.mention_key JOIN "
        "task5.link_decision d ON d.link_candidate_id = c.link_candidate_id AND d.rule_version = %s AND d.tier = 'auto' "
        "WHERE mp.person_mapping_version_id = %s GROUP BY mp.person_id HAVING count(DISTINCT c.nconst) > 1) t",
        link_key, rv, pmap)
    out["nconst_shared_by_persons"] = one(
        "SELECT count(*) FROM (SELECT external_id FROM task5.person_external_id WHERE person_mapping_version_id = %s "
        "GROUP BY external_id HAVING count(*) > 1) t", pmap)
    out["external_id_rows"] = one("SELECT count(*) FROM task5.person_external_id WHERE person_mapping_version_id = %s", pmap)
    out["persons_with_external_flag"] = one("SELECT count(*) FROM task5.person WHERE person_mapping_version_id = %s AND "
                                            "has_external_id", pmap)
    out["mentions_with_two_auto_links"] = one(
        "SELECT count(*) FROM (SELECT c.mention_key FROM task5.link_candidate c JOIN task5.link_decision d ON "
        "d.link_candidate_id = c.link_candidate_id AND d.rule_version = %s AND d.tier = 'auto' WHERE c.extraction_id = %s "
        "AND c.stage_key = %s GROUP BY c.mention_key HAVING count(*) > 1) t", rv, eid, link_key)
    base = ("FROM task5.link_candidate c JOIN task5.link_decision d ON d.link_candidate_id = c.link_candidate_id "
            "AND d.rule_version = %s AND d.tier = 'auto' WHERE c.extraction_id = %s AND c.stage_key = %s")
    out["auto_links_credits"] = one(f"SELECT count(*) {base} AND c.route = 'credits'", rv, eid, link_key)
    out["auto_links_credits_violating_r12"] = one(
        f"""SELECT count(*) {base} AND c.route = 'credits' AND (c.level = 'X4' OR c.cross_role
            OR (SELECT count(*) FROM task5.link_candidate c2 WHERE c2.extraction_id = c.extraction_id
                AND c2.stage_key = c.stage_key AND c2.route = 'credits' AND c2.mention_key = c.mention_key
                AND c2.tconst = c.tconst AND NOT c2.cross_role) <> 1
            OR (SELECT count(DISTINCT c3.tconst) FROM task5.link_candidate c3 WHERE c3.extraction_id = c.extraction_id
                AND c3.stage_key = c.stage_key AND c3.route = 'credits' AND c3.mention_key = c.mention_key) <> 1)""",
        rv, eid, link_key)
    out["auto_links_filmography"] = one(f"SELECT count(*) {base} AND c.route = 'filmography'", rv, eid, link_key)
    out["auto_links_filmography_violating_r14"] = one(
        f"""SELECT count(*) {base} AND c.route = 'filmography' AND (c.level <> 'X1' OR c.anchor_link_decision_id IS NULL
            OR NOT EXISTS (SELECT 1 FROM task5.link_decision a JOIN task5.link_candidate ac ON ac.link_candidate_id =
                           a.link_candidate_id WHERE a.link_decision_id = c.anchor_link_decision_id AND a.tier = 'auto'
                           AND ac.route = 'credits' AND ac.nconst = c.nconst)
            OR (SELECT count(*) FROM task5.link_candidate c2 JOIN task5.link_decision d2 ON d2.link_candidate_id =
                c2.link_candidate_id AND d2.rule_version = d.rule_version WHERE c2.extraction_id = c.extraction_id
                AND c2.stage_key = c.stage_key AND c2.route = 'filmography' AND c2.mention_key = c.mention_key
                AND d2.tier <> 'reject') <> 1
            OR ((c.evidence->>'imdb_start_year') IS NOT NULL AND (c.evidence->>'title_year') IS NOT NULL
                AND abs((c.evidence->>'imdb_start_year')::int - (c.evidence->>'title_year')::int) > %s))""",
        rv, eid, link_key, rules.year_tolerance)

    # 自动合并：R19
    auto_pairs = bad_pairs = 0
    for cid, reason, veto, feats in conn.execute(
            "SELECT c.person_candidate_id, d.reason, d.veto, d.features FROM task5.person_candidate c JOIN "
            "task5.person_decision d USING (person_candidate_id) WHERE c.extraction_id = %s AND c.stage_key = %s AND "
            "d.rule_version = %s AND d.tier = 'auto' AND d.round = (SELECT max(x.round) FROM task5.person_decision x "
            "WHERE x.person_candidate_id = c.person_candidate_id AND x.rule_version = d.rule_version)",
            (eid, link_key, rv)).fetchall():
        auto_pairs += 1
        collab = feats.get("collaborators", {})
        w = rules.w_cluster * len(collab.get("cluster", [])) + rules.w_same_name * len(collab.get("same_name", []))
        if rules.use_publisher and feats.get("publishers", {}).get("overlap"):
            w += 1
        w = min(w, rules.w_max)
        base_ok = not veto and feats.get("n_level") == "N1" and feats.get("roles", {}).get("overlap")
        ok = base_ok and ((reason == "co_credit" and w >= rules.auto_w_min)
                          or (reason == "rare_name_co_credit" and rules.use_rarity and w >= 1
                              and len(collab.get("same_name", [])) >= 1 and feats.get("rarity", {}).get("rare_both")))
        bad_pairs += 0 if ok else 1
    out["auto_pair_decisions"], out["auto_pair_violating_r19"] = auto_pairs, bad_pairs
    out["candidates_without_decision"] = one(
        "SELECT count(*) FROM task5.person_candidate c WHERE c.extraction_id = %s AND c.stage_key = %s AND NOT EXISTS "
        "(SELECT 1 FROM task5.person_decision d WHERE d.person_candidate_id = c.person_candidate_id AND "
        "d.rule_version = %s)", eid, link_key, rv)
    out["rounds"] = [dict(zip(("round", "new_auto_edges", "new_merged_persons", "blocked_edges"), r)) for r in
                     conn.execute("SELECT round, new_auto_edges, new_merged_persons, blocked_edges FROM "
                                  "task5.iteration_round WHERE person_mapping_version_id = %s ORDER BY round", (pmap,))]

    # MoviePerson
    out["movie_person_built"] = "movie_person" in counts
    out["movie_person_rows"] = one("SELECT count(*) FROM task5.movie_person WHERE person_mapping_version_id = %s", pmap)
    out["movie_person_without_source"] = one(
        "SELECT count(*) FROM task5.movie_person m WHERE m.person_mapping_version_id = %s AND (SELECT count(*) FROM "
        "task5.movie_person_source s WHERE s.person_mapping_version_id = m.person_mapping_version_id AND "
        "s.movie_id = m.movie_id AND s.person_id = m.person_id AND s.role = m.role) <> m.mention_count", pmap)
    out["attributed_mentions"] = one("SELECT count(*) FROM task5.mention_person WHERE person_mapping_version_id = %s AND "
                                     "attribution = 'movie'", pmap)
    out["attributed_mentions_with_source"] = one(
        "SELECT count(DISTINCT s.mention_key) FROM task5.movie_person_source s WHERE s.person_mapping_version_id = %s", pmap)

    # 任务 4 的表未被写入：映射版本记录的数量与重数一致
    recount = {
        "movies_with_editions": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                    "edition_count > 0", t4),
        "multi_product_movies": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                    "edition_count > 1", t4),
        "review_pending_products": one("SELECT count(*) FROM task4.product_match_status WHERE mapping_version_id = %s "
                                       "AND status = 'review_pending'", t4),
        "imdb_linked_movies": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                  "status = 'imdb_linked'", t4),
        "page_only_movies": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                "status = 'page_only'", t4),
        "contains_relations": one("SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %s AND "
                                  "relation_type = 'contains'", t4)}
    t4_counts = one("SELECT counts FROM task4.mapping_version WHERE mapping_version_id = %s", t4)
    out["t4_counts_recorded"] = {k: t4_counts.get(k) for k in recount}
    out["t4_counts_recounted"] = recount
    out["t4_table_rows"] = {t: one(f"SELECT count(*) FROM task4.{t}") for t in T4_TABLES}
    out["migrations"] = [r[0] for r in conn.execute("SELECT version FROM public.schema_migration ORDER BY version")]
    return out


def problems(out: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for k, label in (("mentions_missing", "页面有名字但没有提及"), ("mentions_extra", "没有页面依据的提及"),
                     ("mentions_status_mismatch", "提及状态与重新抽取的结果不一致"),
                     ("resolvable_without_person", "没有人物的 resolvable 提及"),
                     ("non_resolvable_with_person", "placeholder 或 ambiguous 却属于人物的提及"),
                     ("persons_without_mentions", "没有提及的人物"),
                     ("persons_with_two_nconst", "关联了两个 nconst 的人物"),
                     ("nconst_shared_by_persons", "共用同一 nconst 的人物"),
                     ("mentions_with_two_auto_links", "有两个自动外部 ID 的提及"),
                     ("auto_links_credits_violating_r12", "不满足 R12 的途径一自动外部 ID"),
                     ("auto_links_filmography_violating_r14", "不满足 R14–R15 的途径二自动外部 ID"),
                     ("auto_pair_violating_r19", "不满足 R19 的自动合并"),
                     ("candidates_without_decision", "没有判定的跨作品候选"),
                     ("movie_person_without_source", "没有来源提及（或来源数不符）的 MoviePerson")):
        if out[k]:
            found.append(f"{label}：{out[k]}")
    if not out["movie_person_built"]:
        found.append("MoviePerson 尚未生成")
    if out["mention_person_rows"] != out["mentions_by_status"].get("resolvable", 0):
        found.append(f"提及归属 {out['mention_person_rows']} 行 ≠ resolvable 提及 {out['mentions_by_status'].get('resolvable', 0)}")
    if out["external_id_rows"] != out["persons_with_external_flag"]:
        found.append(f"外部 ID {out['external_id_rows']} 行 ≠ 有外部 ID 的人物 {out['persons_with_external_flag']}")
    if out["attributed_mentions"] != out["attributed_mentions_with_source"]:
        found.append(f"归属到作品的提及 {out['attributed_mentions']} 个，有 MoviePerson 来源的 "
                     f"{out['attributed_mentions_with_source']} 个")
    if out["t4_counts_recorded"] != out["t4_counts_recounted"]:
        found.append(f"任务 4 映射版本记录的数量与重数不符：{out['t4_counts_recorded']} ≠ {out['t4_counts_recounted']}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 5 只读核对")
    ap.add_argument("--extraction-id")
    ap.add_argument("--pmap")
    args = ap.parse_args(argv)
    with connect(load_settings()) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        pmap = args.pmap or latest_pmap(conn, eid)
        out = verify(conn, pmap)
    out["problems"] = problems(out)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 1 if out["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
