"""任务 5 统计、导出与实例（spec R39、R40，AC12、AC16）。

口径见 design“统计与报告”“实例”。全部查询限定一个人物映射版本（它记录了抽取、任务 4 映射版本、规则版本与 IMDb 快照）；
导出不含评论正文与整页 HTML。IMDb 来源与页面来源分开统计（第三方不并入页面识别数）。

命令行（容器内，工作目录 /opt/etl）：
    python -m etl_task5.report summary                       # 打印统计 JSON（默认抽取的最新人物映射版本）
    python -m etl_task5.report examples                      # 打印别名合并与同名或近似名不合并的实例
    python -m etl_task5.report export --run-key manual       # 写 summary.json、report.md、CSV 与 examples.md
    python -m etl_task5.report diff <旧版本> <新版本>          # person_id 变化的提及与原因
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from . import review
from .check import latest_pmap
from .cluster import diff_versions

SUMMARY_VERSION = "task5-person-summary-1"
EXAMPLES_PER_KIND = 5
UNKNOWN_TOP = 50
DECISION_SAMPLE = 200
QUEUE_TOP = 20


def output_root(output_dir: Path) -> Path:
    return output_dir / "task5"


def run_dir(output_dir: Path, eid: str, pmap: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / pmap / run_key


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 6) if d else None


def pmap_row(conn: psycopg.Connection, pmap: str) -> dict[str, Any]:
    r = conn.execute(
        "SELECT v.extraction_id, v.parent_version_id, v.t4_mapping_version_id, v.parser_version, v.norm_version, "
        "v.rule_version, v.imdb_snapshot_id, v.human_set_hash, v.counts, v.run_id, v.created_at, m.t3_rule_version "
        "FROM task5.person_mapping_version v JOIN task4.mapping_version m ON m.mapping_version_id = "
        "v.t4_mapping_version_id WHERE v.person_mapping_version_id = %s", (pmap,)).fetchone()
    if r is None:
        raise ValueError(f"人物映射版本 {pmap} 不存在")
    keys = ("extraction_id", "parent_version_id", "t4_mapping_version_id", "parser_version", "norm_version",
            "rule_version", "imdb_snapshot_id", "human_set_hash", "counts", "run_id", "created_at", "t3_rule_version")
    d = {"person_mapping_version_id": pmap, **dict(zip(keys, r))}
    d["link_key"] = "|".join((d["t4_mapping_version_id"], d["parser_version"], d["norm_version"], d["rule_version"],
                              d["imdb_snapshot_id"]))
    return d


# 每个候选的最终判定（最大轮次那一行）
FINAL = """WITH fin AS (
    SELECT DISTINCT ON (d.person_candidate_id) d.person_candidate_id, c.group_a, c.group_b, c.generator, d.tier,
           d.reason, d.veto, d.score, d.confidence, d.round, d.features
    FROM task5.person_candidate c JOIN task5.person_decision d USING (person_candidate_id)
    WHERE c.extraction_id = %(eid)s AND c.stage_key = %(key)s AND d.rule_version = %(rv)s
    ORDER BY d.person_candidate_id, d.round DESC)"""


def build_summary(conn: psycopg.Connection, pmap: str) -> dict[str, Any]:
    m = pmap_row(conn, pmap)
    p = {"eid": m["extraction_id"], "pv": m["parser_version"], "nv": m["norm_version"], "rv": m["rule_version"],
         "key": m["link_key"], "v": pmap, "t4": m["t4_mapping_version_id"], "t3": m["t3_rule_version"],
         "run": m["run_id"]}
    counts = m["counts"]
    q = lambda sql: conn.execute(sql, p).fetchall()  # noqa: E731
    one = lambda sql: conn.execute(sql, p).fetchone()[0]  # noqa: E731

    # 提及
    mention_scope = "extraction_id = %(eid)s AND parser_version = %(pv)s AND norm_version = %(nv)s"
    by_status = dict(q(f"SELECT status, count(*) FROM task5.person_mention WHERE {mention_scope} GROUP BY 1 ORDER BY 1"))
    other = [{"section": s, "field_key": k, "label": lab, "rows": int(n)} for s, k, lab, n in conn.execute(
        "SELECT f.section, f.field_key, coalesce(f.label_raw, ''), count(*) FROM task3.page_field f JOIN "
        "task4.product_edition e ON e.extraction_id = f.extraction_id AND e.asin = f.asin AND e.t3_rule_version = %(t3)s "
        "AND e.match_scope = 'matched' WHERE f.extraction_id = %(eid)s AND f.parser_version = %(pv)s AND f.field_key IN "
        "('writers', 'producers', 'contributor') GROUP BY 1, 2, 3 ORDER BY 4 DESC, 1, 2, 3", p).fetchall()]
    mentions = {
        "total": sum(by_status.values()), "by_status": {k: int(v) for k, v in by_status.items()},
        "by_role": {k: int(v) for k, v in q(f"SELECT role_class, count(*) FROM task5.person_mention WHERE {mention_scope} "
                                            "GROUP BY 1 ORDER BY 1")},
        "by_section": {k: int(v) for k, v in q(f"SELECT section, count(*) FROM task5.person_mention WHERE {mention_scope} "
                                               "GROUP BY 1 ORDER BY 1")},
        "other_roles": other, "other_role_rows": sum(x["rows"] for x in other),
        "note": "其他角色署名（编剧、制片、Contributor）只按页面字段行数计数，不建提及（R03）；一行详情要点可含多个名字"}

    # 合并前后
    resolvable = int(counts["mentions_resolvable"])
    before_after = {"resolvable_mentions": resolvable, "raw_variants": int(counts["raw_variants"]),
                    "strict_keys": int(counts["strict_keys"]), "groups": int(counts["groups"]),
                    "persons": int(counts["persons"]), "merged_away": resolvable - int(counts["persons"]),
                    "reduction_rate": _ratio(resolvable - int(counts["persons"]), resolvable)}

    # 外部 ID（两条途径分开；IMDb 是第三方来源，不并入页面识别数）
    ld = ("FROM task5.link_candidate c JOIN task5.link_decision d ON d.link_candidate_id = c.link_candidate_id AND "
          "d.rule_version = %(rv)s WHERE c.extraction_id = %(eid)s AND c.stage_key = %(key)s")
    link_tiers = {f"{r}/{t}": int(n) for r, t, n in q(f"SELECT c.route, d.tier, count(*) {ld} GROUP BY 1, 2 ORDER BY 1, 2")}
    link_reasons = {f"{r}/{t}/{x}": int(n) for r, t, x, n in q(
        f"SELECT c.route, d.tier, d.reason, count(*) {ld} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")}
    linked_mentions = {r: int(n) for r, n in q(f"SELECT c.route, count(DISTINCT c.mention_key) {ld} AND d.tier = 'auto' "
                                               "GROUP BY 1 ORDER BY 1")}
    not_found = one(
        "SELECT count(*) FROM task5.person_mention m WHERE m.extraction_id = %(eid)s AND m.parser_version = %(pv)s AND "
        "m.norm_version = %(nv)s AND m.status = 'resolvable' AND EXISTS (SELECT 1 FROM task4.product_movie pm JOIN "
        "task4.movie mv ON mv.mapping_version_id = pm.mapping_version_id AND mv.movie_id = pm.movie_id WHERE "
        "pm.mapping_version_id = %(t4)s AND pm.asin = m.asin AND mv.status = 'imdb_linked') AND NOT EXISTS (SELECT 1 FROM "
        "task5.link_candidate c WHERE c.extraction_id = m.extraction_id AND c.stage_key = %(key)s AND c.mention_key = "
        "m.mention_key AND c.route = 'credits')")
    persons_ext = one("SELECT count(*) FROM task5.person WHERE person_mapping_version_id = %(v)s AND has_external_id")
    mentions_ext = one("SELECT count(*) FROM task5.mention_person mp JOIN task5.person p ON p.person_mapping_version_id = "
                       "mp.person_mapping_version_id AND p.person_id = mp.person_id WHERE mp.person_mapping_version_id = "
                       "%(v)s AND p.has_external_id")
    basis = {b: int(n) for b, n in q("SELECT basis, count(*) FROM task5.mention_person WHERE person_mapping_version_id = "
                                     "%(v)s GROUP BY 1 ORDER BY 1")}
    route_persons = {r or "none": int(n) for r, n in q("SELECT external_route, count(*) FROM task5.person WHERE "
                                                       "person_mapping_version_id = %(v)s GROUP BY 1 ORDER BY 1")}
    hints = [{"asin": a, "member_ordinal": mo, "tconst": t, "mention_count": int(n), "mention_keys": list(keys)}
             for a, mo, t, n, keys in conn.execute(
                 "SELECT asin, member_ordinal, tconst, mention_count, mention_keys FROM task5.t4_upgrade_hint WHERE "
                 "extraction_id = %(eid)s AND stage_key = %(key)s ORDER BY mention_count DESC, asin, tconst LIMIT 20",
                 p).fetchall()]
    external = {
        "tiers_by_route": link_tiers, "reasons_by_route": link_reasons, "mentions_linked_by_route": linked_mentions,
        "persons_with_external_id": int(persons_ext), "mentions_in_persons_with_external_id": int(mentions_ext),
        "persons_by_first_route": route_persons, "mentions_by_basis": basis,
        "merges_by_route": {"credits_filmography_same_nconst": int(counts["mentions_by_basis"].get("credits", 0)
                                                                   + counts["mentions_by_basis"].get("filmography", 0)),
                            "co_credit": int(counts["mentions_by_basis"].get("co_credit", 0))},
        "not_found_in_linked_works": int(not_found),
        "upgrade_hints": int(one("SELECT count(*) FROM task5.t4_upgrade_hint WHERE extraction_id = %(eid)s AND "
                                 "stage_key = %(key)s")),
        "upgrade_hint_examples": hints,
        "source_kind": {"page_derived": int(resolvable - mentions_ext), "third_party_imdb": int(mentions_ext),
                        "note": "IMDb 是第三方来源，外部 ID 覆盖与页面直接识别的数量分开统计"}}

    # 跨作品候选
    fin_q = lambda sel: conn.execute(f"{FINAL} {sel}", p).fetchall()  # noqa: E731
    cross = {
        "by_generator_tier": {f"{g}/{t}": int(n) for g, t, n in fin_q(
            "SELECT generator, tier, count(*) FROM fin GROUP BY 1, 2 ORDER BY 1, 2")},
        "by_tier": {t: int(n) for t, n in fin_q("SELECT tier, count(*) FROM fin GROUP BY 1 ORDER BY 1")},
        "by_tier_reason": {f"{t}/{r}": int(n) for t, r, n in fin_q(
            "SELECT tier, reason, count(*) FROM fin GROUP BY 1, 2 ORDER BY 1, 2")},
        "hard_conflicts": {k: int(n) for k, n in fin_q("SELECT x, count(*) FROM fin, unnest(veto) AS x GROUP BY 1 ORDER BY 1")},
        "feature_switches": {
            "rarity_would_change": int(fin_q("SELECT count(*) FROM fin WHERE features->'feature_switches'->'tier_if_enabled'"
                                             "->>'rarity' IS NOT NULL AND features->'feature_switches'->'tier_if_enabled'"
                                             "->>'rarity' <> tier")[0][0]),
            "publisher_would_change": int(fin_q("SELECT count(*) FROM fin WHERE features->'feature_switches'->"
                                                "'tier_if_enabled'->>'publisher' IS NOT NULL AND features->'feature_switches'"
                                                "->'tier_if_enabled'->>'publisher' <> tier")[0][0])},
        "block_overflow": {"blocks": int(one("SELECT count(*) FROM task5.quality_issue WHERE run_id = %(run)s AND "
                                             "issue_code = 'block_overflow'")),
                           "skipped": int(one("SELECT count(*) FROM task5.quality_issue WHERE run_id = %(run)s AND "
                                              "issue_code = 'block_overflow' AND detail->>'skipped' = 'true'"))},
        "cluster_checks": {f"{c}/{a}": int(n) for c, a, n in q(
            "SELECT conflict, action, count(*) FROM task5.cluster_check WHERE person_mapping_version_id = %(v)s "
            "GROUP BY 1, 2 ORDER BY 1, 2")},
        "iteration": [{"round": r, "new_auto_edges": a, "new_merged_persons": b, "blocked_edges": c} for r, a, b, c in q(
            "SELECT round, new_auto_edges, new_merged_persons, blocked_edges FROM task5.iteration_round WHERE "
            "person_mapping_version_id = %(v)s ORDER BY round")]}
    cross["rounds"] = len(cross["iteration"])
    cross["candidates"] = sum(cross["by_tier"].values())

    # 人物状态、待复核与人工复核
    status = {s: int(n) for s, n in q("SELECT status, count(*) FROM task5.person WHERE person_mapping_version_id = %(v)s "
                                      "GROUP BY 1 ORDER BY 1")}
    total_persons = sum(status.values())
    with_pending = int(one("SELECT count(*) FROM task5.person WHERE person_mapping_version_id = %(v)s AND "
                           "pending_candidates > 0"))
    queue = review.queue_rows(conn, pmap)
    reviewed_person, total_person_review = fin_q(
        "SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM task5.human_decision h WHERE h.extraction_id = %(eid)s AND "
        "((h.left_entity = split_part(f.group_a, '|', 2) AND h.right_entity = split_part(f.group_b, '|', 2)) OR "
        "(h.left_entity = split_part(f.group_b, '|', 2) AND h.right_entity = split_part(f.group_a, '|', 2))))), count(*) "
        "FROM fin f WHERE f.tier = 'review'")[0]
    reviewed_link, total_link_review = conn.execute(
        "SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM task5.human_decision h WHERE h.extraction_id = c.extraction_id "
        "AND h.left_entity = c.mention_key AND h.right_entity = 'imdb:' || c.nconst)), count(*) FROM "
        "task5.link_candidate c JOIN task5.link_decision d ON d.link_candidate_id = c.link_candidate_id AND "
        "d.rule_version = %(rv)s WHERE c.extraction_id = %(eid)s AND c.stage_key = %(key)s AND d.tier = 'review'",
        p).fetchone()
    review_total = int(total_person_review + total_link_review)
    people = {
        "status": status, "persons": total_persons,
        "review_pending_rate": _ratio(status.get("review_pending", 0), total_persons),
        "persons_with_pending_candidates": with_pending,
        "pending_candidate_rate": _ratio(with_pending, total_persons),
        "human_decisions": int(one("SELECT count(*) FROM task5.human_decision WHERE extraction_id = %(eid)s")),
        "human_set_hash": m["human_set_hash"], "review_candidates": review_total,
        "human_reviewed_candidates": int(reviewed_person + reviewed_link),
        "human_review_coverage": _ratio(int(reviewed_person + reviewed_link), review_total),
        "queue_rows": len(queue),
        "queue_top": [{"rank": i, "impact": r[0], "kind": r[3][0], "candidate_id": r[3][1], "left": r[3][4],
                       "right": r[3][5], "reason": r[3][12]} for i, r in enumerate(queue[:QUEUE_TOP], start=1)]}

    # MoviePerson
    mp = {
        "relations": int(one("SELECT count(*) FROM task5.movie_person WHERE person_mapping_version_id = %(v)s")),
        "sources": int(one("SELECT count(*) FROM task5.movie_person_source WHERE person_mapping_version_id = %(v)s")),
        "by_role": {k: int(v) for k, v in q("SELECT role, count(*) FROM task5.movie_person WHERE person_mapping_version_id "
                                            "= %(v)s GROUP BY 1 ORDER BY 1")},
        "by_source_section": {k: int(v) for k, v in q(
            "SELECT m.section, count(*) FROM task5.movie_person_source s JOIN task5.person_mention m ON m.extraction_id = "
            "%(eid)s AND m.parser_version = %(pv)s AND m.norm_version = %(nv)s AND m.mention_key = s.mention_key WHERE "
            "s.person_mapping_version_id = %(v)s GROUP BY 1 ORDER BY 1")},
        "order_conflicts": int(one("SELECT count(*) FROM task5.movie_person WHERE person_mapping_version_id = %(v)s AND "
                                   "order_conflict")),
        "multi_role_persons": int(counts.get("movie_person", {}).get("multi_role_persons", 0)),
        "unattributed_mentions": int(one("SELECT count(*) FROM task5.mention_person WHERE person_mapping_version_id = "
                                         "%(v)s AND attribution = 'unattributed'")),
        "note": "starring 与 cast 不算多角色；unattributed 的合集提及有人物、没有关系（R34）"}

    # 别名、未识别的括号段与称谓
    aliases = {
        "types": {k: int(v) for k, v in q("SELECT t, count(*) FROM task5.person_alias, unnest(alias_types) t WHERE "
                                          "person_mapping_version_id = %(v)s GROUP BY 1 ORDER BY 2 DESC, 1")},
        "rows": int(one("SELECT count(*) FROM task5.person_alias WHERE person_mapping_version_id = %(v)s")),
        "unknown_paren_top": [{"raw": r, "count": int(n)} for r, n in q(
            f"SELECT paren_text, count(*) FROM task5.person_mention WHERE {mention_scope} AND paren_text IS NOT NULL "
            f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {UNKNOWN_TOP}")],
        "honorific_top": [{"raw": r, "count": int(n)} for r, n in q(
            f"SELECT honorific, count(*) FROM task5.person_mention WHERE {mention_scope} AND honorific IS NOT NULL "
            f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {UNKNOWN_TOP}")]}
    return {"summary_version": SUMMARY_VERSION, "person_mapping_version_id": pmap, "parent_version_id": m["parent_version_id"],
            "extraction_id": m["extraction_id"], "t4_mapping_version_id": m["t4_mapping_version_id"],
            "parser_version": m["parser_version"], "norm_version": m["norm_version"], "rule_version": m["rule_version"],
            "imdb_snapshot_id": m["imdb_snapshot_id"], "mentions": mentions, "before_after": before_after,
            "external_ids": external, "cross_work": cross, "people": people, "movie_person": mp, "aliases": aliases}


# ---------------------------------------------------------------- 实例（R39）

def _labels(conn: psycopg.Connection, eid: str) -> list[dict[str, Any]]:
    """用户已标注的评价对（所有评价集，按标注时间），用来在实例里注明标注结论。"""
    rows = conn.execute(
        "SELECT p.left_mention, p.right_mention, l.label, l.annotator, l.note FROM task5.eval_label l JOIN "
        "task5.eval_pair p USING (eval_set_id, pair_id) JOIN task5.eval_set s USING (eval_set_id) "
        "WHERE s.extraction_id = %s ORDER BY l.labeled_at", (eid,)).fetchall()
    return [{"left": a, "right": b, "label": lab, "annotator": who, "note": note} for a, b, lab, who, note in rows]


def mention_briefs(conn: psycopg.Connection, m: dict[str, Any], keys: list[str]) -> dict[str, dict[str, Any]]:
    """提及的原始署名、页面位置、作品与源记录定位（页面地址、快照路径；归属到作品的才有作品标题与 tconst）。"""
    if not keys:
        return {}
    rows = conn.execute(
        "SELECT m.mention_key, m.raw_value, m.role_class, m.section, m.label_raw, m.list_position, m.asin, mv.display_title, "
        "mv.tconst, e.final_url, pp.snapshot_path, mp.basis, mp.round, mp.attribution, mp.person_id, mp.group_key "
        "FROM task5.person_mention m LEFT JOIN task5.mention_person mp ON mp.person_mapping_version_id = %s AND "
        "mp.mention_key = m.mention_key LEFT JOIN task5.person_group g ON g.extraction_id = m.extraction_id AND "
        "g.stage_key = %s AND g.group_key = mp.group_key LEFT JOIN task4.movie mv ON mv.mapping_version_id = %s AND "
        "mv.movie_id = g.movie_id LEFT JOIN task4.product_edition e ON e.extraction_id = m.extraction_id AND "
        "e.t3_rule_version = %s AND e.asin = m.asin LEFT JOIN task3.page_parse pp ON pp.extraction_id = m.extraction_id "
        "AND pp.asin = m.asin AND pp.parser_version = m.parser_version WHERE m.extraction_id = %s AND m.parser_version = %s "
        "AND m.norm_version = %s AND m.mention_key = ANY(%s)",
        (m["person_mapping_version_id"], m["link_key"], m["t4_mapping_version_id"], m["t3_rule_version"],
         m["extraction_id"], m["parser_version"], m["norm_version"], keys)).fetchall()
    cols = ("mention_key", "raw", "role", "section", "label", "list_position", "asin", "work_title", "tconst",
            "final_url", "snapshot_path", "basis", "round", "attribution", "person_id", "group_key")
    return {r[0]: dict(zip(cols, r)) for r in rows}


def _person_card(conn: psycopg.Connection, m: dict[str, Any], pid: str, limit: int = 6) -> dict[str, Any]:
    pm = m["person_mapping_version_id"]
    p = conn.execute("SELECT canonical_name, status, external_route, mention_count, work_count FROM task5.person WHERE "
                     "person_mapping_version_id = %s AND person_id = %s", (pm, pid)).fetchone()
    ext = conn.execute("SELECT external_id, snapshot_id, name_line_no, birth_year, death_year, basis_decision_ids FROM "
                       "task5.person_external_id WHERE person_mapping_version_id = %s AND person_id = %s", (pm, pid)).fetchone()
    aliases = [{"alias": a, "types": list(t), "source": s, "occurrences": n} for a, t, s, n in conn.execute(
        "SELECT alias, alias_types, source, occurrences FROM task5.person_alias WHERE person_mapping_version_id = %s AND "
        "person_id = %s ORDER BY occurrences DESC, alias", (pm, pid)).fetchall()]
    keys = [r[0] for r in conn.execute("SELECT mention_key FROM task5.mention_person WHERE person_mapping_version_id = %s "
                                       "AND person_id = %s ORDER BY mention_key COLLATE \"C\"", (pm, pid)).fetchall()]
    briefs = mention_briefs(conn, m, keys[:limit])
    return {"person_id": pid, "canonical_name": p[0], "status": p[1], "external_route": p[2], "mention_count": p[3],
            "work_count": p[4],
            "external_id": ({"nconst": ext[0], "imdb_url": f"https://www.imdb.com/name/{ext[0]}/", "snapshot": ext[1],
                             "name_line_no": ext[2], "birth_year": ext[3], "death_year": ext[4],
                             "basis_decision_ids": list(ext[5])} if ext else None),
            "aliases": aliases, "mentions_total": len(keys), "mentions": [briefs[k] for k in keys[:limit] if k in briefs]}


def examples(conn: psycopg.Connection, pmap: str, n: int = EXAMPLES_PER_KIND) -> dict[str, Any]:
    """至少 n 组别名合并和 n 组同名或近似名不合并的实例（数据不足时有多少给多少），每组附原始署名、作品、外部 ID 或冲突证据
    与源记录定位。选择是确定性的：已有用户标注的优先，其次严格键相同（同名）的，再其次有硬冲突的，最后按 md5 排序；标注结论只在用户标注过时才写。"""
    m = pmap_row(conn, pmap)
    p = {"eid": m["extraction_id"], "key": m["link_key"], "rv": m["rule_version"], "v": pmap}
    labels = _labels(conn, m["extraction_id"])

    # 别名合并：一个人物有两种以上的页面写法
    cands = conn.execute(
        "SELECT p.person_id, p.has_external_id FROM task5.person p WHERE p.person_mapping_version_id = %s AND "
        "(SELECT count(*) FROM task5.person_alias a WHERE a.person_mapping_version_id = p.person_mapping_version_id AND "
        "a.person_id = p.person_id AND a.source = 'page') >= 2 ORDER BY p.has_external_id DESC, md5(p.person_id) LIMIT 400",
        (pmap,)).fetchall()
    mention_sets = {pid: {r[0] for r in conn.execute("SELECT mention_key FROM task5.mention_person WHERE "
                                                     "person_mapping_version_id = %s AND person_id = %s", (pmap, pid))}
                    for pid, _ in cands[:400]}

    def annotation(pid: str) -> dict[str, Any] | None:
        for lab in reversed(labels):
            if lab["left"] in mention_sets.get(pid, ()) and lab["right"] in mention_sets.get(pid, ()):
                return lab
        return None

    ordered = sorted(cands, key=lambda c: (not (annotation(c[0]) or {}).get("label") == "same", not c[1]))
    merged = []
    for pid, _ in ordered[:n]:
        card = _person_card(conn, m, pid)
        merged.append({"kind": "alias_merge", **card, "annotation": annotation(pid)})

    # 同名或近似名不合并：判定为 review 或 reject、且双方不在同一人物
    rows = conn.execute(
        f"""{FINAL} SELECT f.person_candidate_id, f.tier, f.reason, f.veto, f.score, f.confidence, f.features,
                  split_part(f.group_a, '|', 2), split_part(f.group_b, '|', 2), pa.person_id, pb.person_id
            FROM fin f JOIN task5.mention_person pa ON pa.person_mapping_version_id = %(v)s AND
                 pa.mention_key = split_part(f.group_a, '|', 2)
            JOIN task5.mention_person pb ON pb.person_mapping_version_id = %(v)s AND
                 pb.mention_key = split_part(f.group_b, '|', 2)
            WHERE f.tier IN ('review', 'reject') AND pa.person_id <> pb.person_id
            ORDER BY (f.features->>'n_level' = 'N1') DESC, (f.tier = 'reject' AND cardinality(f.veto) > 0) DESC,
                     md5(f.person_candidate_id::text) LIMIT 800""", p).fetchall()

    def label_of(a: str, b: str) -> dict[str, Any] | None:
        for lab in reversed(labels):
            if {lab["left"], lab["right"]} == {a, b}:
                return lab
        return None

    def rank(r) -> tuple:
        lab = label_of(r[7], r[8])
        hard = bool(r[3]) and r[1] == "reject"
        return (not (lab and lab["label"] == "different"), lab is None, r[6].get("n_level") != "N1", not hard, r[1] != "reject")

    chosen = sorted(rows, key=rank)[:n]
    briefs = mention_briefs(conn, m, sorted({k for r in chosen for k in (r[7], r[8])}))
    unmerged = []
    for cid, tier, reason, veto, score, conf, feats, ka, kb, pa, pb in chosen:
        compact = {k: feats.get(k) for k in ("n_level", "roles", "external", "collaborators", "era", "disambiguator")}
        unmerged.append({"kind": "same_or_similar_name_not_merged", "person_candidate_id": cid, "tier": tier,
                         "reason": reason, "veto": list(veto), "score": float(score), "confidence": float(conf),
                         "features": compact, "annotation": label_of(ka, kb),
                         "left": {**briefs[ka], "person_id": pa, "nconst": (feats.get("external") or {}).get("a")},
                         "right": {**briefs[kb], "person_id": pb, "nconst": (feats.get("external") or {}).get("b")}})
    return {"person_mapping_version_id": pmap, "rule_version": m["rule_version"], "per_kind_target": n,
            "alias_merges": merged, "not_merged": unmerged,
            "note": "选择是确定性的：已由用户标注的优先，其次严格键相同（同名）的，再其次有硬冲突的，最后按 md5 排序；annotation 为空表示未标注"}


def render_examples(ex: dict[str, Any]) -> str:
    def ann(a: dict[str, Any] | None) -> str:
        return f"{a['label']}（{a['annotator']}）" if a else "未标注"

    def line(b: dict[str, Any]) -> str:
        return (f"`{b['mention_key']}` 原值 {b['raw']!r}（{b['role']}，{b['section']}:{b['label']}）；作品 "
                f"{b.get('work_title') or '未归属'}（{b.get('tconst') or '无 IMDb 关联'}）；页面 {b.get('final_url')}，"
                f"快照 `{b.get('snapshot_path')}`")

    lines = ["# 任务 5 实例", "", f"人物映射版本 {ex['person_mapping_version_id']}，规则 {ex['rule_version']}。{ex['note']}", "",
             "## 别名合并", ""]
    for i, g in enumerate(ex["alias_merges"], start=1):
        e = g["external_id"]
        lines += [f"### 合并 {i}：{g['canonical_name']}（`{g['person_id']}`，{g['status']}，{g['mention_count']} 条提及，"
                  f"{g['work_count']} 部作品）", "",
                  f"- 用户标注：{ann(g['annotation'])}",
                  f"- 外部 ID：" + (f"{e['nconst']}（{e['imdb_url']}；快照 {e['snapshot']} 第 {e['name_line_no']} 行；途径 "
                                    f"{g['external_route']}；判定 {e['basis_decision_ids']}）" if e else "无（页面内归并）"),
                  "- 别名：" + "；".join(f"{a['alias']!r} {a['types']}×{a['occurrences']}（{a['source']}）" for a in g["aliases"])]
        lines += [f"- {line(b)}；依据 {b['basis']}" for b in g["mentions"]]
        lines.append("")
    lines += ["## 同名或近似名不合并", ""]
    for i, g in enumerate(ex["not_merged"], start=1):
        lines += [f"### 未合并 {i}：{g['left']['raw']} / {g['right']['raw']}", "",
                  f"- 判定 {g['tier']}（{g['reason']}），硬冲突 {g['veto'] or '无'}，得分 {g['score']}，置信度 {g['confidence']}",
                  f"- 用户标注：{ann(g['annotation'])}",
                  f"- 外部 ID：{g['left']['nconst'] or '无'} / {g['right']['nconst'] or '无'}；证据 "
                  f"`{json.dumps(g['features'], ensure_ascii=False)}`",
                  f"- 左（`{g['left']['person_id']}`）：{line(g['left'])}",
                  f"- 右（`{g['right']['person_id']}`）：{line(g['right'])}", ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 导出（R40）

PERSONS_SQL = """SELECT p.person_id, p.status, p.canonical_name, p.canonical_source, p.has_external_id,
       e.external_id AS nconst, p.external_route, p.mention_count, p.work_count, p.pending_candidates
FROM task5.person p LEFT JOIN task5.person_external_id e ON e.person_mapping_version_id = p.person_mapping_version_id
AND e.person_id = p.person_id WHERE p.person_mapping_version_id = %(v)s ORDER BY p.person_id"""
ALIASES_SQL = """SELECT person_id, alias, array_to_string(alias_types, '|') AS alias_types, source, occurrences
FROM task5.person_alias WHERE person_mapping_version_id = %(v)s ORDER BY person_id, alias"""
MOVIE_PERSON_SQL = """SELECT mp.movie_id, mv.display_title, mv.tconst, mp.person_id, p.canonical_name, mp.role,
       mp.credit_order, mp.order_conflict, array_to_string(mp.source_labels, '|') AS source_labels, mp.mention_count
FROM task5.movie_person mp JOIN task5.person p ON p.person_mapping_version_id = mp.person_mapping_version_id AND
p.person_id = mp.person_id LEFT JOIN task4.movie mv ON mv.mapping_version_id = %(t4)s AND mv.movie_id = mp.movie_id
WHERE mp.person_mapping_version_id = %(v)s ORDER BY mp.movie_id, mp.role, mp.credit_order NULLS LAST, mp.person_id"""
HINTS_SQL = """SELECT asin, member_ordinal, tconst, mention_count, array_to_string(mention_keys, '|') AS mention_keys
FROM task5.t4_upgrade_hint WHERE extraction_id = %(eid)s AND stage_key = %(key)s ORDER BY mention_count DESC, asin, tconst"""
DECISIONS_SQL = FINAL + """, allrows AS (
    SELECT 'person' AS kind, person_candidate_id AS decision_ref, tier, reason, score, confidence, round,
           array_to_string(veto, '|') AS veto, features::text AS features FROM fin
    UNION ALL
    SELECT 'external', d.link_decision_id, d.tier, d.reason, NULL, d.confidence, NULL, NULL,
           jsonb_build_object('route', c.route, 'mention_key', c.mention_key, 'tconst', c.tconst, 'nconst', c.nconst,
                              'level', c.level, 'evidence', c.evidence)::text
    FROM task5.link_candidate c JOIN task5.link_decision d ON d.link_candidate_id = c.link_candidate_id AND
         d.rule_version = %(rv)s WHERE c.extraction_id = %(eid)s AND c.stage_key = %(key)s)
SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY kind, tier ORDER BY md5(kind || decision_ref::text)) AS rn
               FROM allrows) t WHERE rn <= %(n)s ORDER BY kind, tier, rn"""


def _write_query(conn: psycopg.Connection, sql: str, params: dict[str, Any], dest: Path) -> dict[str, Any]:
    buf = io.StringIO()
    w = csv.writer(buf)
    rows = 0
    with conn.cursor() as cur:
        cur.execute(sql, params)
        w.writerow([d.name for d in cur.description])
        for r in cur:
            w.writerow(r)
            rows += 1
    reporting.write_atomic(dest, buf.getvalue().encode("utf-8"))
    return {**reporting.file_digest(dest), "rows": rows}


def export_files(conn: psycopg.Connection, pmap: str, directory: Path, ex: dict[str, Any]) -> list[dict[str, Any]]:
    """persons.csv、aliases.csv、movie_person.csv、decisions_sample.csv（每类每档按 md5 取前 200）、t4_upgrade_hints.csv、
    examples.md；返回每个文件的路径、行数、字节数与 SHA-256。不含评论正文与整页 HTML。"""
    m = pmap_row(conn, pmap)
    p = {"v": pmap, "eid": m["extraction_id"], "key": m["link_key"], "rv": m["rule_version"],
         "t4": m["t4_mapping_version_id"], "n": DECISION_SAMPLE}
    files = [_write_query(conn, PERSONS_SQL, p, directory / "persons.csv"),
             _write_query(conn, ALIASES_SQL, p, directory / "aliases.csv"),
             _write_query(conn, MOVIE_PERSON_SQL, p, directory / "movie_person.csv"),
             _write_query(conn, DECISIONS_SQL, p, directory / "decisions_sample.csv"),
             _write_query(conn, HINTS_SQL, p, directory / "t4_upgrade_hints.csv")]
    reporting.write_atomic(directory / "examples.md", render_examples(ex).encode("utf-8"))
    files.append({**reporting.file_digest(directory / "examples.md"), "rows": len(ex["alias_merges"]) + len(ex["not_merged"])})
    return files


def render_markdown(s: dict[str, Any]) -> str:
    fmt = lambda n: f"{n:,}" if isinstance(n, int) else ("—" if n is None else str(n))  # noqa: E731
    pct = lambda r: "—" if r is None else f"{r * 100:.2f}%"  # noqa: E731
    table = lambda title, d: ["", f"| {title} | 数量 |", "|---|---|"] + [f"| {k} | {fmt(v)} |" for k, v in d.items()]  # noqa: E731
    lines = ["# 任务 5 人物姓名归一与消歧报告", "",
             f"- 最终状态：**{s['final_status']}**（交付完整：{s['delivery_complete']}）",
             f"- DAG / run：`{s['dag_id']}` / `{s['run_key']}`", "", "## 任务状态", ""]
    lines += [f"- {k}：{v}" for k, v in s["tasks"].items()]
    st = s.get("stats")
    if st:
        mn, ba, ex, cw, pe, mp, al = (st[k] for k in ("mentions", "before_after", "external_ids", "cross_work", "people",
                                                    "movie_person", "aliases"))
        lines += ["", f"- 抽取：`{st['extraction_id']}`；任务 4 映射版本 {st['t4_mapping_version_id']}、解析 {st['parser_version']}、"
                      f"规范化 {st['norm_version']}、人物规则 {st['rule_version']}、IMDb 快照 `{st['imdb_snapshot_id']}`",
                  f"- 人物映射版本：`{st['person_mapping_version_id']}`（上一版本 {st['parent_version_id'] or '无'}），"
                  f"人工决策 {pe['human_decisions']} 条", "", "## 人物提及", ""]
        lines += table("状态", mn["by_status"]) + table("角色", mn["by_role"]) + table("区域", mn["by_section"])
        lines += ["", f"其他角色署名（页面字段行）{fmt(mn['other_role_rows'])} 行：" + "；".join(
            f"{x['section']}/{x['label'] or x['field_key']} {fmt(x['rows'])}" for x in mn["other_roles"][:10]),
                  mn["note"], "", "## 合并前后", "",
                  f"- resolvable 提及 {fmt(ba['resolvable_mentions'])} → 不同原始写法 {fmt(ba['raw_variants'])} → 不同严格键 "
                  f"{fmt(ba['strict_keys'])} → 作品内人物组 {fmt(ba['groups'])} → 人物 {fmt(ba['persons'])}"
                  f"（合并掉 {fmt(ba['merged_away'])} 条提及，{pct(ba['reduction_rate'])}）", "", "## 外部人物 ID", "",
                  f"- 途径一（作品演职员）自动关联 {fmt(ex['mentions_linked_by_route'].get('credits', 0))} 条提及，"
                  f"途径二（作品年表）{fmt(ex['mentions_linked_by_route'].get('filmography', 0))} 条；"
                  f"关联作品中未找到 {fmt(ex['not_found_in_linked_works'])} 条",
                  f"- 有外部 ID 的人物 {fmt(ex['persons_with_external_id'])} 个，涉及提及 {fmt(ex['mentions_in_persons_with_external_id'])} 条"
                  f"（第三方 IMDb 来源 {fmt(ex['source_kind']['third_party_imdb'])}，页面直接识别 {fmt(ex['source_kind']['page_derived'])}，"
                  f"分开统计）", f"- 合作者佐证合入的提及 {fmt(ex['merges_by_route']['co_credit'])} 条",
                  f"- 任务 4 升级线索 {fmt(ex['upgrade_hints'])} 个（只记录，不改作品归属）"]
        lines += table("途径/判定档", ex["tiers_by_route"]) + table("途径/判定档/原因", ex["reasons_by_route"])
        lines += ["", "## 跨作品归并", "", f"候选 {fmt(cw['candidates'])} 个，各档：" + "，".join(
            f"{k} {fmt(v)}" for k, v in cw["by_tier"].items()),
                  f"- 迭代 {cw['rounds']} 轮：" + "；".join(f"第 {r['round']} 轮新增合并边 {r['new_auto_edges']}、"
                                                          f"拦下 {r['blocked_edges']}" for r in cw["iteration"]),
                  f"- 块溢出 {fmt(cw['block_overflow']['blocks'])} 个（跳过 {fmt(cw['block_overflow']['skipped'])}）",
                  f"- 特征开关打开会改变判定的候选：稀有度 {fmt(cw['feature_switches']['rarity_would_change'])}，"
                  f"发行商 {fmt(cw['feature_switches']['publisher_would_change'])}"]
        lines += table("生成器/判定档", cw["by_generator_tier"]) + table("判定档/原因", cw["by_tier_reason"])
        lines += table("硬冲突", cw["hard_conflicts"]) + table("一致性检查", cw["cluster_checks"])
        lines += ["", "## 人物状态与待复核", ""] + table("状态", pe["status"])
        lines += ["", f"- 待复核率（review_pending 人物）{pct(pe['review_pending_rate'])}；有待复核候选的人物 "
                      f"{fmt(pe['persons_with_pending_candidates'])} 个（{pct(pe['pending_candidate_rate'])}）",
                  f"- 人工复核覆盖率 {pct(pe['human_review_coverage'])}（{fmt(pe['human_reviewed_candidates'])} / "
                  f"{fmt(pe['review_candidates'])}）；待复核队列 {fmt(pe['queue_rows'])} 条，影响最大的前 5 条："
                  + "；".join(f"{x['left']} / {x['right']}（影响 {x['impact']}）" for x in pe["queue_top"][:5]),
                  "", "## MoviePerson", "", f"- 关系 {fmt(mp['relations'])} 条，来源提及 {fmt(mp['sources'])} 条；"
                  f"有署名顺序冲突 {fmt(mp['order_conflicts'])}；多角色人物 {fmt(mp['multi_role_persons'])}；"
                  f"unattributed 提及 {fmt(mp['unattributed_mentions'])}"]
        lines += table("角色", mp["by_role"]) + table("来源区域", mp["by_source_section"])
        lines += ["", "## 别名类型", ""] + table("类型", al["types"])
        if s.get("examples_file"):
            lines += ["", f"实例见 `{s['examples_file']}`（{len(s['examples']['alias_merges'])} 组别名合并、"
                          f"{len(s['examples']['not_merged'])} 组同名或近似名不合并）。"]
    if s.get("exports"):
        lines += ["", "## 导出文件", ""]
        lines += [f"- `{f['path']}`：{fmt(f['rows'])} 行，{fmt(f['bytes'])} 字节，SHA-256 `{f['sha256']}`"
                  for f in s["exports"]]
    if s["failures"]:
        lines += ["", "## 失败记录", ""]
        lines += [f"- {f.get('task')} 第 {f.get('try_number')} 次：{f.get('error_type')}："
                  f"{str(f.get('message'))[:300]}（暂时错误：{f.get('transient')}）" for f in s["failures"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 5 统计与实例")
    ap.add_argument("cmd", choices=("summary", "examples", "export", "diff"))
    ap.add_argument("versions", nargs="*", help="diff：<旧版本> <新版本>")
    ap.add_argument("--extraction-id")
    ap.add_argument("--pmap")
    ap.add_argument("--run-key", default="manual")
    ap.add_argument("--count", type=int, default=EXAMPLES_PER_KIND)
    args = ap.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        if args.cmd == "diff":
            if len(args.versions) != 2:
                ap.error("diff 需要 <旧版本> <新版本>")
            rows = diff_versions(conn, *args.versions)
            out: dict[str, Any] = {"old": args.versions[0], "new": args.versions[1], "changed_mentions": len(rows),
                                   "changes": [{"mention_key": a, "old_person_id": b, "new_person_id": c, "reason": d}
                                               for a, b, c, d in rows]}
        else:
            v = args.pmap or latest_pmap(conn, eid)
            if args.cmd == "summary":
                out = build_summary(conn, v)
            elif args.cmd == "examples":
                out = examples(conn, v, args.count)
            else:
                ex = examples(conn, v, args.count)
                directory = run_dir(s.output_dir, eid, v, args.run_key)
                out = {"directory": str(directory), "files": export_files(conn, v, directory, ex)}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
