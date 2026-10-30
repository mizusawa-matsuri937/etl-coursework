"""任务 5 的两个只读查询（spec R41，AC12）。

    python -m etl_task5.query person <person_id>          # 原始署名、别名、归并决策链与参与作品
    python -m etl_task5.query director "<姓名>"            # 匹配的人物及其作品与来源（同名的多个人物分别列出）
两者都可加 --pmap（默认抽取的最新人物映射版本）与 --extraction-id。输出一段 JSON，不写任何表。
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

from . import names
from .check import latest_pmap
from .report import _person_card, mention_briefs, pmap_row


def person_works(conn: psycopg.Connection, m: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    """人物参与的作品：按作品列出角色、署名顺序、来源标签与来源商品。"""
    pm = m["person_mapping_version_id"]
    out: dict[str, dict[str, Any]] = {}
    for movie, title, tconst, role, order, conflict, labels, n in conn.execute(
            "SELECT mp.movie_id, mv.display_title, mv.tconst, mp.role, mp.credit_order, mp.order_conflict, "
            "mp.source_labels, mp.mention_count FROM task5.movie_person mp LEFT JOIN task4.movie mv ON "
            "mv.mapping_version_id = %s AND mv.movie_id = mp.movie_id WHERE mp.person_mapping_version_id = %s AND "
            "mp.person_id = %s ORDER BY mv.display_title, mp.movie_id, mp.role", (m["t4_mapping_version_id"], pm, pid)):
        w = out.setdefault(movie, {"movie_id": movie, "title": title, "tconst": tconst, "roles": [], "asins": []})
        w["roles"].append({"role": role, "credit_order": order, "order_conflict": conflict,
                           "source_labels": list(labels), "mentions": n})
    for movie, asin, key in conn.execute(
            "SELECT movie_id, asin, mention_key FROM task5.movie_person_source WHERE person_mapping_version_id = %s AND "
            "person_id = %s ORDER BY movie_id, asin, mention_key", (pm, pid)):
        out[movie]["asins"].append({"asin": asin, "mention_key": key, "url": f"https://www.amazon.com/dp/{asin}"})
    return list(out.values())


def decision_chain(conn: psycopg.Connection, m: dict[str, Any], pid: str, keys: list[str]) -> dict[str, Any]:
    """归并依据：外部 ID 判定（两条途径）、作品内分组原因、跨作品决策（含轮次）、人工决策、映射事件。"""
    pm, key = m["person_mapping_version_id"], m["link_key"]
    links = [{"link_decision_id": did, "route": route, "mention_key": mk, "tconst": t, "nconst": nc, "level": lvl,
              "tier": tier, "reason": reason, "confidence": float(conf), "evidence": ev}
             for did, route, mk, t, nc, lvl, tier, reason, conf, ev in conn.execute(
                 "SELECT d.link_decision_id, c.route, c.mention_key, c.tconst, c.nconst, c.level, d.tier, d.reason, "
                 "d.confidence, c.evidence FROM task5.link_candidate c JOIN task5.link_decision d ON d.link_candidate_id "
                 "= c.link_candidate_id AND d.rule_version = %s WHERE c.extraction_id = %s AND c.stage_key = %s AND "
                 "c.mention_key = ANY(%s) AND d.tier = 'auto' ORDER BY c.mention_key, c.route",
                 (m["rule_version"], m["extraction_id"], key, keys)).fetchall()]
    groups = [{"group_key": g, "reason": r, "mention_key": mk} for g, r, mk in conn.execute(
        "SELECT gm.group_key, gm.reason, gm.mention_key FROM task5.group_member gm WHERE gm.extraction_id = %s AND "
        "gm.stage_key = %s AND gm.mention_key = ANY(%s) ORDER BY gm.group_key, gm.mention_key", (
            m["extraction_id"], key, keys)).fetchall()]
    group_keys = sorted({g["group_key"] for g in groups})
    edges = [{"person_candidate_id": cid, "group_a": ga, "group_b": gb, "round": rnd, "tier": tier, "reason": reason,
              "score": float(score), "collaborators": feats.get("collaborators"), "n_level": feats.get("n_level")}
             for cid, ga, gb, rnd, tier, reason, score, feats in conn.execute(
                 "SELECT c.person_candidate_id, c.group_a, c.group_b, d.round, d.tier, d.reason, d.score, d.features "
                 "FROM task5.person_candidate c JOIN task5.person_decision d USING (person_candidate_id) WHERE "
                 "c.extraction_id = %s AND c.stage_key = %s AND d.rule_version = %s AND d.tier = 'auto' AND "
                 "(c.group_a = ANY(%s) OR c.group_b = ANY(%s)) ORDER BY c.person_candidate_id, d.round",
                 (m["extraction_id"], key, m["rule_version"], group_keys, group_keys)).fetchall()]
    human = [{"human_decision_id": hid, "kind": kind, "left": left, "right": right, "annotator": who, "note": note}
            for hid, kind, left, right, who, note in conn.execute(
                "SELECT human_decision_id, kind, left_entity, right_entity, annotator, note FROM task5.human_decision "
                "WHERE extraction_id = %s AND (left_entity = ANY(%s) OR right_entity = ANY(%s)) ORDER BY 1",
                (m["extraction_id"], keys, keys)).fetchall()]
    events = [{"event": e, "mention_key": mk, "from": a, "to": b, "reason": r} for e, mk, a, b, r in conn.execute(
        "SELECT event, mention_key, from_person_id, to_person_id, reason FROM task5.mapping_event WHERE "
        "person_mapping_version_id = %s AND (to_person_id = %s OR from_person_id = %s) ORDER BY event_id", (pm, pid, pid))]
    return {"external_id_decisions": links, "within_work_groups": groups, "cross_work_auto_decisions": edges,
            "human_decisions": human, "mapping_events": events}


def query_person(conn: psycopg.Connection, pmap: str, person_id: str) -> dict[str, Any]:
    m = pmap_row(conn, pmap)
    row = conn.execute("SELECT 1 FROM task5.person WHERE person_mapping_version_id = %s AND person_id = %s",
                       (pmap, person_id)).fetchone()
    if row is None:
        return {"person_mapping_version_id": pmap, "person_id": person_id, "found": False}
    card = _person_card(conn, m, person_id, limit=10_000)
    keys = [x["mention_key"] for x in card["mentions"]]
    for x in card["mentions"]:
        x["url"] = f"https://www.amazon.com/dp/{x['asin']}"
    return {"person_mapping_version_id": pmap, "found": True, **card, "works": person_works(conn, m, person_id),
            "decision_chain": decision_chain(conn, m, person_id, keys)}


def query_director(conn: psycopg.Connection, pmap: str, name: str) -> dict[str, Any]:
    """按严格键找到所有导演提及所在的人物；同名的不同人物分别列出，每个给出作品与来源提及。"""
    m = pmap_row(conn, pmap)
    strict = names.parse(name).strict
    rows = conn.execute(
        "SELECT mp.person_id, array_agg(mp.mention_key ORDER BY mp.mention_key COLLATE \"C\") FROM task5.person_mention pm "
        "JOIN task5.mention_person mp ON mp.person_mapping_version_id = %s AND mp.mention_key = pm.mention_key "
        "WHERE pm.extraction_id = %s AND pm.parser_version = %s AND pm.norm_version = %s AND pm.role_class = 'director' "
        "AND pm.strict_key = %s GROUP BY mp.person_id ORDER BY mp.person_id",
        (pmap, m["extraction_id"], m["parser_version"], m["norm_version"], strict)).fetchall()
    persons = []
    for pid, mention_keys in rows:
        card = _person_card(conn, m, pid, limit=0)
        briefs = mention_briefs(conn, m, mention_keys)
        works = [w for w in person_works(conn, m, pid) if any(r["role"] == "director" for r in w["roles"])]
        persons.append({"person_id": pid, "canonical_name": card["canonical_name"], "status": card["status"],
                        "external_id": card["external_id"], "aliases": card["aliases"], "works": works,
                        "director_mentions": [{**briefs[k], "url": f"https://www.amazon.com/dp/{briefs[k]['asin']}"}
                                              for k in mention_keys if k in briefs]})
    return {"person_mapping_version_id": pmap, "query": name, "strict_key": strict, "matched_persons": len(persons),
            "persons": persons}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 5 只读查询")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("person")
    pp.add_argument("person_id")
    dp = sub.add_parser("director")
    dp.add_argument("name")
    for p in (pp, dp):
        p.add_argument("--extraction-id")
        p.add_argument("--pmap")
    args = ap.parse_args(argv)
    with connect(load_settings()) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        pmap = args.pmap or latest_pmap(conn, eid)
        out = query_person(conn, pmap, args.person_id) if args.cmd == "person" else query_director(conn, pmap, args.name)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
