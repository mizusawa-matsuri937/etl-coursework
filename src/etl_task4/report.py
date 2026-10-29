"""任务 4 统计、导出与实例（spec R19、R36、R37，AC12、AC16）。

口径见 design“统计与报告”“实例”。全部查询限定一个映射版本（它记录了抽取、规则版本与 IMDb 快照）；
导出不含评论正文与整页 HTML。IMDb 来源与页面来源分开统计（第三方不并入页面识别数）。

命令行（容器内，工作目录 /opt/etl）：
    python -m etl_task4.report summary                 # 打印统计 JSON（默认抽取的最新映射版本）
    python -m etl_task4.report examples                # 打印跨版本合并与易混淆未合并的实例
    python -m etl_task4.report export --run-key manual # 写 summary、report、CSV 与 examples.md
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

from .check import latest_mapping_version
from .cluster import record_scopes

SUMMARY_VERSION = "task4-match-summary-1"
EXAMPLES_PER_KIND = 5
UNKNOWN_TOP = 50
DECISION_SAMPLE = 200
FEATURES = ("title", "director", "actor", "runtime", "year")
VETO_CONFLICTS = ("director_disjoint", "year_conflict", "different_external_id")


def output_root(output_dir: Path) -> Path:
    return output_dir / "task4"


def run_dir(output_dir: Path, eid: str, mapping_version_id: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / mapping_version_id / run_key


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 6) if d else None


def mapping_row(conn: psycopg.Connection, v: str) -> dict[str, Any]:
    r = conn.execute("SELECT extraction_id, parent_version_id, t3_rule_version, parser_version, norm_version, "
                     "rule_version, imdb_snapshot_id, human_set_hash, counts, run_id, created_at FROM "
                     "task4.mapping_version WHERE mapping_version_id = %s", (v,)).fetchone()
    if r is None:
        raise ValueError(f"映射版本 {v} 不存在")
    keys = ("extraction_id", "parent_version_id", "t3_rule_version", "parser_version", "norm_version", "rule_version",
            "imdb_snapshot_id", "human_set_hash", "counts", "run_id", "created_at")
    return {"mapping_version_id": v, **dict(zip(keys, r))}


def build_summary(conn: psycopg.Connection, v: str) -> dict[str, Any]:
    m = mapping_row(conn, v)
    eid, t3, pv, nv, rv, sid = (m[k] for k in ("extraction_id", "t3_rule_version", "parser_version", "norm_version",
                                              "rule_version", "imdb_snapshot_id"))
    p = {"eid": eid, "t3": t3, "pv": pv, "nv": nv, "rv": rv, "sid": sid, "v": v}
    counts = m["counts"]
    scope = dict(conn.execute("SELECT match_scope, count(*) FROM task4.product_edition WHERE extraction_id = %(eid)s "
                              "AND t3_rule_version = %(t3)s GROUP BY 1 ORDER BY 1", p).fetchall())
    kinds = dict(conn.execute("SELECT product_kind, count(*) FROM task4.product_edition WHERE extraction_id = %(eid)s "
                              "AND t3_rule_version = %(t3)s GROUP BY 1 ORDER BY 1", p).fetchall())
    total_products = sum(scope.values())
    movies = int(counts["participating_products"])

    # 合并前后（R29）：参与匹配的电影类商品 → 作品
    after = int(counts["movies_with_editions"])
    before_after = {
        "movie_products_before": movies, "movies_after": after, "merged_away": movies - after,
        "multi_product_movies": int(counts["multi_product_movies"]),
        "products_in_multi_product_movies": int(counts["products_in_multi_product_movies"]),
        "single_product_movies": int(counts["single_product_movies"]),
        "review_pending_products": int(counts["review_pending_products"]),
        "review_pending_rate": _ratio(int(counts["review_pending_products"]), movies),
        "merge_rate": _ratio(movies - after, movies),
        "multi_product_size_distribution": {
            str(k): int(n) for k, n in conn.execute(
                "SELECT edition_count, count(*) FROM task4.movie WHERE mapping_version_id = %(v)s AND "
                "edition_count > 1 GROUP BY 1 ORDER BY 1", p).fetchall()},
    }
    # 作品来源：IMDb 与页面分开（第三方不并入页面识别数）
    linked_products = int(conn.execute(
        "SELECT coalesce(sum(edition_count), 0) FROM task4.movie WHERE mapping_version_id = %(v)s AND "
        "status = 'imdb_linked'", p).fetchone()[0])
    sources = {
        "imdb_linked_movies": int(counts["imdb_linked_movies"]), "page_only_movies": int(counts["page_only_movies"]),
        "contains_only_movies": int(counts["contains_only_movies"]),
        "products_linked_to_imdb": linked_products, "products_page_only": movies - linked_products,
        "imdb_link_rate": _ratio(linked_products, movies),
        "source_kind": {"page_derived": movies - linked_products, "third_party_imdb": linked_products,
                        "note": "IMDb 是第三方来源，关联数与页面直接识别的数量分开统计（任务 3 R18 的延续）"},
    }
    # 候选与判定
    by_gen = {f"{g}/{t}": int(n) for g, t, n in conn.execute(
        """SELECT c.generator, d.tier, count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %(eid)s AND c.norm_version = %(nv)s AND c.imdb_snapshot_id = %(sid)s
             AND d.rule_version = %(rv)s GROUP BY 1, 2 ORDER BY 1, 2""", p).fetchall()}
    tiers = {t: int(n) for t, n in conn.execute(
        """SELECT d.tier, count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %(eid)s AND c.norm_version = %(nv)s AND c.imdb_snapshot_id = %(sid)s
             AND d.rule_version = %(rv)s GROUP BY 1 ORDER BY 1""", p).fetchall()}
    reasons = {f"{t}/{r or '-'}": int(n) for t, r, n in conn.execute(
        """SELECT d.tier, d.reason, count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %(eid)s AND c.norm_version = %(nv)s AND c.imdb_snapshot_id = %(sid)s
             AND d.rule_version = %(rv)s GROUP BY 1, 2 ORDER BY 1, 2""", p).fetchall()}
    vetoes = {k: int(n) for k, n in conn.execute(
        """SELECT x, count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id),
                  unnest(d.veto) AS x
           WHERE c.extraction_id = %(eid)s AND c.norm_version = %(nv)s AND c.imdb_snapshot_id = %(sid)s
             AND d.rule_version = %(rv)s GROUP BY 1 ORDER BY 1""", p).fetchall()}
    overflow = conn.execute("SELECT count(*), coalesce(sum(candidate_total - kept), 0) FROM task4.candidate_overflow "
                            "WHERE extraction_id = %(eid)s AND norm_version = %(nv)s AND imdb_snapshot_id = %(sid)s",
                            p).fetchone()
    decisions = {"rule_version": rv, "by_generator_tier": by_gen, "by_tier": tiers, "by_tier_reason": reasons,
                 "hard_conflicts": vetoes, "candidates": sum(tiers.values()),
                 "candidate_overflow_entities": int(overflow[0]), "candidates_truncated": int(overflow[1]),
                 "cluster_conflicts_removed": int(counts["cluster_conflicts_removed"]),
                 "cluster_checks": {a: int(n) for a, n in conn.execute(
                     "SELECT action, count(*) FROM task4.cluster_check WHERE mapping_version_id = %(v)s GROUP BY 1 "
                     "ORDER BY 1", p).fetchall()}}
    # 合集与剧集
    members = conn.execute(
        "SELECT count(DISTINCT entity_id) FROM task4.field_assertion WHERE extraction_id = %(eid)s AND "
        "parser_version = %(pv)s AND norm_version = %(nv)s AND entity_type = 'collection_member'", p).fetchone()[0]
    unmatched = conn.execute(
        "SELECT count(*) FROM task4.quality_issue q WHERE q.extraction_id = %(eid)s AND q.issue_code = "
        "'collection_member_unmatched' AND q.run_id = %(run)s", {**p, "run": m["run_id"]}).fetchone()[0]
    collections = {"collections": int(counts["collections"]), "split": int(counts["collections_split"]),
                   "unsplit": int(counts["collections_unsplit"]), "members": int(members),
                   "member_links": int(counts["contains_relations"]), "members_unmatched": int(unmatched),
                   "contains_only_movies": int(counts["contains_only_movies"])}
    tv = {"tv_series_products": int(kinds.get("tv_series", 0)),
          "tv_series_relations": int(conn.execute(
              "SELECT count(*) FROM task4.product_movie pm JOIN task4.product_edition e ON e.extraction_id = %(eid)s "
              "AND e.t3_rule_version = %(t3)s AND e.asin = pm.asin WHERE pm.mapping_version_id = %(v)s AND "
              "e.product_kind = 'tv_series'", p).fetchone()[0]),
          "note": "剧集只建商品版本，不建电影作品，不与电影混合计数（R16）"}
    review_reasons = {r or "-": int(n) for r, n in conn.execute(
        "SELECT reason, count(*) FROM task4.product_match_status WHERE mapping_version_id = %(v)s AND "
        "status = 'review_pending' GROUP BY 1 ORDER BY 2 DESC, 1", p).fetchall()}
    statuses = {s: int(n) for s, n in conn.execute(
        "SELECT status, count(*) FROM task4.product_match_status WHERE mapping_version_id = %(v)s GROUP BY 1 "
        "ORDER BY 1", p).fetchall()}
    scopes = record_scopes(conn, v)
    read, no_asin = conn.execute("SELECT count(*), count(*) FILTER (WHERE product_id_observed IS NULL) FROM "
                                 "public.review_record WHERE extraction_id = %(eid)s", p).fetchone()
    reviews = {"edition": scopes["edition"], "collection": scopes["collection"], "other_video": scopes["other_video"],
               "no_page": scopes["no_page"], "manifest_total": scopes["manifest_total"],
               "records_without_asin": int(no_asin), "records_read": int(read),
               "consistent": scopes["manifest_total"] + int(no_asin) == int(read),
               "note": "评论仍归属商品；按作品统计只经 edition 关系计入，合集评论单列，不复制给成员电影（R19）"}
    unknown = [{"raw": r, "count": int(n)} for r, n in conn.execute(
        "SELECT raw_value, count(*) FROM task4.field_assertion WHERE extraction_id = %(eid)s AND parser_version = %(pv)s "
        "AND norm_version = %(nv)s AND field_name = 'descriptor' AND qualifier = 'unknown' GROUP BY 1 "
        "ORDER BY 2 DESC, 1 LIMIT %(n)s", {**p, "n": UNKNOWN_TOP}).fetchall()]
    human = {"decisions": int(conn.execute("SELECT count(*) FROM task4.human_decision WHERE extraction_id = %(eid)s",
                                           p).fetchone()[0]), "set_hash": m["human_set_hash"]}
    return {"summary_version": SUMMARY_VERSION, "mapping_version_id": v, "parent_version_id": m["parent_version_id"],
            "extraction_id": eid, "t3_rule_version": t3, "parser_version": pv, "norm_version": nv, "rule_version": rv,
            "imdb_snapshot_id": sid, "human": human,
            "product_editions": {"total": total_products, "match_scope": scope, "product_kind": kinds,
                                 "match_status": statuses},
            "before_after": before_after, "sources": sources, "decisions": decisions, "collections": collections,
            "tv_series": tv, "review_pending_reasons": review_reasons, "record_scopes": reviews,
            "unknown_descriptors_top": unknown}


# ---------------------------------------------------------------- 实例（R36）

def _compact(features: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in FEATURES:
        f = (features or {}).get(k)
        if isinstance(f, dict):
            out[k] = {"value": f.get("value"), "score": f.get("score")}
            for extra in ("overlap", "left", "right", "diff"):
                if f.get(extra) not in (None, [], ""):
                    out[k][extra] = f[extra]
    links = (features or {}).get("links")
    if links:
        out["links"] = links
    return out


def _briefs(conn: psycopg.Connection, m: dict[str, Any], asins: list[str]) -> dict[str, dict[str, Any]]:
    """每个商品的页面事实与来源定位：原始标题、标题年份、导演、前 3 位演员、片长、页面地址、快照位置。"""
    if not asins:
        return {}
    out = {a: {"asin": a, "title": None, "title_year": None, "directors": [], "actors": [], "runtime_min": None,
               "final_url": None, "snapshot_path": None, "content_hash": None, "title_assertion_id": None}
           for a in asins}
    for asin, field, raw, norm, aid in conn.execute(
            "SELECT entity_id, field_name, raw_value, normalized_value, assertion_id FROM task4.field_assertion "
            "WHERE extraction_id = %s AND parser_version = %s AND norm_version = %s AND entity_id = ANY(%s) AND "
            "field_name IN ('title', 'title_year', 'director', 'actor', 'runtime_min') ORDER BY entity_id, "
            "field_name, ordinal", (m["extraction_id"], m["parser_version"], m["norm_version"], asins)).fetchall():
        b = out[asin]
        if field == "title" and b["title"] is None:
            b["title"], b["title_assertion_id"] = raw, aid
        elif field == "title_year":
            b["title_year"] = int(norm)
        elif field == "director" and raw not in b["directors"]:
            b["directors"].append(raw)
        elif field == "actor" and raw not in b["actors"] and len(b["actors"]) < 3:
            b["actors"].append(raw)
        elif field == "runtime_min" and b["runtime_min"] is None and norm:
            b["runtime_min"] = int(norm)
    for asin, url, chash, path in conn.execute(
            "SELECT e.asin, e.final_url, e.content_hash, p.snapshot_path FROM task4.product_edition e LEFT JOIN "
            "task3.page_parse p ON p.extraction_id = e.extraction_id AND p.asin = e.asin AND p.parser_version = "
            "e.parser_version WHERE e.extraction_id = %s AND e.t3_rule_version = %s AND e.asin = ANY(%s)",
            (m["extraction_id"], m["t3_rule_version"], asins)).fetchall():
        out[asin].update({"final_url": url, "content_hash": chash, "snapshot_path": path})
    return out


def _labels(conn: psycopg.Connection, eid: str) -> dict[frozenset[str], dict[str, Any]]:
    """用户已标注的评价对（取最近一个评价集的标注），用来在实例里注明标注结论。"""
    rows = conn.execute(
        "SELECT p.left_entity, p.right_entity, l.label, l.annotator, l.note FROM task4.eval_label l JOIN "
        "task4.eval_pair p USING (eval_set_id, pair_id) JOIN task4.eval_set s USING (eval_set_id) "
        "WHERE s.extraction_id = %s ORDER BY l.labeled_at", (eid,)).fetchall()
    return {frozenset((a, b)): {"label": lab, "annotator": who, "note": note} for a, b, lab, who, note in rows}


def examples(conn: psycopg.Connection, v: str, n: int = EXAMPLES_PER_KIND) -> dict[str, Any]:
    """至少 n 组跨版本合并和 n 组易混淆而未合并的实例，每组附双方标题、判定档、关键特征与源记录定位。
    选择是确定性的：已有用户标注的优先，其次按 md5 排序；标注结论只在用户标注过时才写。"""
    m = mapping_row(conn, v)
    eid, nv, rv, sid = m["extraction_id"], m["norm_version"], m["rule_version"], m["imdb_snapshot_id"]
    labels = _labels(conn, eid)

    # 跨版本合并：同一作品下有多个商品版本
    groups = conn.execute(
        """SELECT mv.movie_id, mv.status, mv.tconst, mv.display_title, mv.edition_count
           FROM task4.movie mv WHERE mv.mapping_version_id = %s AND mv.edition_count > 1
           ORDER BY md5(mv.movie_id) LIMIT 400""", (v,)).fetchall()
    members: dict[str, list[tuple]] = {}
    for mid, asin, basis, did, tier, score, conf, feats in conn.execute(
            """SELECT pm.movie_id, pm.asin, pm.basis, pm.decision_id, d.tier, d.score, d.confidence, d.features
               FROM task4.product_movie pm LEFT JOIN task4.merge_decision d ON d.decision_id = pm.decision_id
               WHERE pm.mapping_version_id = %s AND pm.relation_type = 'edition' AND pm.movie_id = ANY(%s)
               ORDER BY pm.movie_id, pm.asin COLLATE "C" """, (v, [g[0] for g in groups])).fetchall():
        members.setdefault(mid, []).append((asin, basis, did, tier, score, conf, feats))

    def labelled(mid: str) -> dict[str, Any] | None:
        asins = [x[0] for x in members.get(mid, [])]
        for i, a in enumerate(asins):
            for b in asins[i + 1:]:
                if (lab := labels.get(frozenset((a, b)))) is not None:
                    return lab
        return None

    ordered = sorted(groups, key=lambda g: (labelled(g[0]) is None, g[1] != "imdb_linked"))   # 稳定排序，保持 md5 次序
    merged = []
    for mid, status, tconst, title, cnt in ordered[:n]:
        briefs = _briefs(conn, m, [x[0] for x in members[mid]])
        imdb_src = None
        for _, _, _, _, _, _, feats in members[mid]:
            sr = ((feats or {}).get("evidence") or {}).get("imdb_source_record_id")
            if sr:
                imdb_src = conn.execute("SELECT locator FROM task4.source_record WHERE source_record_id = %s",
                                        (sr,)).fetchone()
                imdb_src = imdb_src[0] if imdb_src else None
                break
        merged.append({
            "kind": "cross_edition_merge", "movie_id": mid, "status": status, "tconst": tconst, "display_title": title,
            "edition_count": cnt, "annotation": labelled(mid), "imdb_source": imdb_src,
            "products": [{**briefs[a], "basis": basis, "decision_id": did, "tier": tier,
                          "score": float(score) if score is not None else None,
                          "confidence": float(conf) if conf is not None else None, "features": _compact(feats)}
                         for a, basis, did, tier, score, conf, feats in members[mid]]})

    # 易混淆而未合并：标题键相同、判定为不同作品（硬冲突优先）
    rows = conn.execute(
        """SELECT d.decision_id, c.left_entity, c.right_id, d.tier, d.reason, d.veto, d.score, d.confidence, d.features,
                  a.movie_id, b.movie_id
           FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
           JOIN task4.product_movie a ON a.mapping_version_id = %(v)s AND a.asin = c.left_entity
                AND a.relation_type = 'edition'
           JOIN task4.product_movie b ON b.mapping_version_id = %(v)s AND b.asin = c.right_id
                AND b.relation_type = 'edition'
           WHERE c.extraction_id = %(eid)s AND c.norm_version = %(nv)s AND c.imdb_snapshot_id = %(sid)s
             AND d.rule_version = %(rv)s AND c.right_kind = 'product' AND a.movie_id <> b.movie_id
           ORDER BY md5(c.left_entity || c.right_id) LIMIT 600""",
        {"v": v, "eid": eid, "nv": nv, "sid": sid, "rv": rv}).fetchall()

    def rank(r) -> tuple:
        lab = labels.get(frozenset((r[1], r[2])))
        hard = bool(r[5]) and r[3] == "reject"
        return (not (lab and lab["label"] == "different"), lab is None, not hard, r[3] != "reject")

    chosen = sorted(rows, key=rank)[:n]
    briefs = _briefs(conn, m, sorted({a for r in chosen for a in (r[1], r[2])}))
    unmerged = [{
        "kind": "confusable_not_merged", "decision_id": r[0], "tier": r[3], "reason": r[4], "veto": list(r[5]),
        "score": float(r[6]), "confidence": float(r[7]), "features": _compact(r[8]),
        "left": {**briefs[r[1]], "movie_id": r[9]}, "right": {**briefs[r[2]], "movie_id": r[10]},
        "annotation": labels.get(frozenset((r[1], r[2])))} for r in chosen]
    return {"mapping_version_id": v, "rule_version": rv, "per_kind_target": n,
            "cross_edition_merges": merged, "confusable_not_merged": unmerged,
            "note": "选择是确定性的：已由用户标注的优先，其次按 md5 排序；annotation 为空表示未标注"}


def render_examples(ex: dict[str, Any]) -> str:
    def prod(b: dict[str, Any]) -> str:
        bits = [f"`{b['asin']}`", repr(b["title"])]
        if b.get("title_year"):
            bits.append(f"标题年份 {b['title_year']}")
        if b.get("directors"):
            bits.append("导演 " + "、".join(b["directors"][:2]))
        if b.get("runtime_min"):
            bits.append(f"{b['runtime_min']} 分钟")
        return "；".join(bits)

    lines = ["# 任务 4 实例", "", f"映射版本 {ex['mapping_version_id']}，规则 {ex['rule_version']}。{ex['note']}", "",
             "## 跨版本合并", ""]
    for i, g in enumerate(ex["cross_edition_merges"], start=1):
        ann = g["annotation"]
        lines += [f"### 合并 {i}：{g['display_title']}（{g['status']}，{g['tconst'] or '无 IMDb 关联'}，"
                  f"{g['edition_count']} 个版本）", "",
                  f"- 用户标注：{ann['label'] + '（' + ann['annotator'] + '）' if ann else '未标注'}"]
        if g["imdb_source"]:
            lines.append(f"- IMDb 源记录：`{json.dumps(g['imdb_source'], ensure_ascii=False)}`")
        for p in g["products"]:
            lines.append(f"- {prod(p)}；判定 {p['tier'] or p['basis']}，得分 {p['score']}，特征 "
                         f"`{json.dumps(p['features'], ensure_ascii=False)}`；页面 {p['final_url']}，快照 "
                         f"`{p['snapshot_path']}`，哈希 `{p['content_hash']}`")
        lines.append("")
    lines += ["## 易混淆而未合并", ""]
    for i, g in enumerate(ex["confusable_not_merged"], start=1):
        ann = g["annotation"]
        lines += [f"### 未合并 {i}：{g['left']['title']} / {g['right']['title']}", "",
                  f"- 判定 {g['tier']}（{g['reason']}），硬冲突 {g['veto'] or '无'}，得分 {g['score']}，"
                  f"置信度 {g['confidence']}",
                  f"- 用户标注：{ann['label'] + '（' + ann['annotator'] + '）' if ann else '未标注'}",
                  f"- 特征：`{json.dumps(g['features'], ensure_ascii=False)}`",
                  f"- 左：{prod(g['left'])}；页面 {g['left']['final_url']}，快照 `{g['left']['snapshot_path']}`",
                  f"- 右：{prod(g['right'])}；页面 {g['right']['final_url']}，快照 `{g['right']['snapshot_path']}`", ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 导出（R37）

MOVIES_SQL = """SELECT mv.movie_id, mv.status, mv.anchor_key, mv.tconst, mv.display_title, mv.edition_count,
       mv.contains_count FROM task4.movie mv WHERE mv.mapping_version_id = %(v)s ORDER BY mv.movie_id"""
PRODUCT_MOVIE_SQL = """SELECT pm.asin, pm.member_ordinal, pm.relation_type, pm.movie_id, pm.basis, pm.decision_id,
       pm.human_decision_id FROM task4.product_movie pm WHERE pm.mapping_version_id = %(v)s
       ORDER BY pm.asin COLLATE "C", pm.member_ordinal, pm.movie_id"""
DECISIONS_SQL = """SELECT * FROM (
    SELECT d.decision_id, d.tier, c.generator, c.left_entity, c.right_kind, c.right_id, d.score, d.confidence, d.rank,
           d.margin, array_to_string(d.veto, '|') AS veto, d.reason, d.rule_version, d.features::text AS features,
           row_number() OVER (PARTITION BY d.tier ORDER BY md5(d.decision_id::text)) AS rn
    FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
    WHERE c.extraction_id = %(eid)s AND c.norm_version = %(nv)s AND c.imdb_snapshot_id = %(sid)s
      AND d.rule_version = %(rv)s) t WHERE rn <= %(n)s ORDER BY tier, rn"""


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


def export_files(conn: psycopg.Connection, v: str, directory: Path, ex: dict[str, Any]) -> list[dict[str, Any]]:
    """movies.csv、product_movie.csv、decisions_sample.csv（每档按 md5 取前 200 个）、examples.md。"""
    m = mapping_row(conn, v)
    p = {"v": v, "eid": m["extraction_id"], "nv": m["norm_version"], "sid": m["imdb_snapshot_id"],
         "rv": m["rule_version"], "n": DECISION_SAMPLE}
    files = [_write_query(conn, MOVIES_SQL, p, directory / "movies.csv"),
             _write_query(conn, PRODUCT_MOVIE_SQL, p, directory / "product_movie.csv"),
             _write_query(conn, DECISIONS_SQL, p, directory / "decisions_sample.csv")]
    text = render_examples(ex)
    reporting.write_atomic(directory / "examples.md", text.encode("utf-8"))
    files.append({**reporting.file_digest(directory / "examples.md"),
                  "rows": len(ex["cross_edition_merges"]) + len(ex["confusable_not_merged"])})
    return files


def render_markdown(s: dict[str, Any]) -> str:
    fmt = lambda n: f"{n:,}" if isinstance(n, int) else ("—" if n is None else str(n))  # noqa: E731
    pct = lambda r: "—" if r is None else f"{r * 100:.2f}%"  # noqa: E731
    lines = ["# 任务 4 同一电影识别报告", "",
             f"- 最终状态：**{s['final_status']}**（交付完整：{s['delivery_complete']}）",
             f"- DAG / run：`{s['dag_id']}` / `{s['run_key']}`", "", "## 任务状态", ""]
    lines += [f"- {k}：{v}" for k, v in s["tasks"].items()]
    st = s.get("stats")
    if st:
        ba, so, de = st["before_after"], st["sources"], st["decisions"]
        lines += ["", f"- 抽取：`{st['extraction_id']}`；任务 3 规则 {st['t3_rule_version']}、解析 {st['parser_version']}、"
                      f"标准化 {st['norm_version']}、匹配规则 {st['rule_version']}、IMDb 快照 `{st['imdb_snapshot_id']}`",
                  f"- 映射版本：`{st['mapping_version_id']}`（上一版本 {st['parent_version_id'] or '无'}），"
                  f"人工决策 {st['human']['decisions']} 条", "",
                  "## 商品版本与匹配状态", "", "| 匹配范围 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in st["product_editions"]["match_scope"].items()]
        lines += [f"| 合计 | {fmt(st['product_editions']['total'])} |", "", "| 任务 3 类别 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in st["product_editions"]["product_kind"].items()]
        lines += ["", "## 合并前后", "",
                  f"- 参与匹配的电影类商品 {fmt(ba['movie_products_before'])} 个 → 作品 {fmt(ba['movies_after'])} 部"
                  f"（合并掉 {fmt(ba['merged_away'])}，{pct(ba['merge_rate'])}）",
                  f"- 多商品作品 {fmt(ba['multi_product_movies'])} 部，共含 {fmt(ba['products_in_multi_product_movies'])} 个商品；"
                  f"单商品作品 {fmt(ba['single_product_movies'])} 部",
                  f"- 待复核商品 {fmt(ba['review_pending_products'])} 个，待复核率 {pct(ba['review_pending_rate'])}",
                  "", "| 待复核原因 | 商品数 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in st["review_pending_reasons"].items()]
        lines += ["", "## 作品来源（第三方与页面分开）", "",
                  f"- IMDb 关联作品 {fmt(so['imdb_linked_movies'])} 部（其中只有合集成员、没有 edition 商品 "
                  f"{fmt(so['contains_only_movies'])} 部），关联商品 {fmt(so['products_linked_to_imdb'])} 个，"
                  f"关联率 {pct(so['imdb_link_rate'])}",
                  f"- 页面独有（page_only）作品 {fmt(so['page_only_movies'])} 部，商品 {fmt(so['products_page_only'])} 个",
                  f"- {so['source_kind']['note']}", "", "## 候选与判定", "",
                  f"规则 {de['rule_version']}；候选 {fmt(de['candidates'])} 个，各档："
                  + "，".join(f"{k} {fmt(v)}" for k, v in de["by_tier"].items()), "",
                  "| 生成器/判定档 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in de["by_generator_tier"].items()]
        lines += ["", "| 判定档/原因 | 数量 |", "|---|---|"] + [f"| {k} | {fmt(v)} |" for k, v in de["by_tier_reason"].items()]
        lines += ["", "| 硬冲突 | 判定数 |", "|---|---|"] + [f"| {k} | {fmt(v)} |" for k, v in de["hard_conflicts"].items()]
        lines += ["", f"- 候选溢出：{fmt(de['candidate_overflow_entities'])} 个实体，截断候选 {fmt(de['candidates_truncated'])} 个",
                  f"- 簇内一致性检查移出：{fmt(de['cluster_conflicts_removed'])} 个商品", "", "## 合集与剧集", ""]
        c, tv = st["collections"], st["tv_series"]
        lines += [f"- 合集商品 {fmt(c['collections'])} 个：拆分成功 {fmt(c['split'])}、未拆分 {fmt(c['unsplit'])}；"
                  f"成员 {fmt(c['members'])} 个，关联到作品 {fmt(c['member_links'])} 条（成员未关联 {fmt(c['members_unmatched'])}）",
                  f"- 剧集商品 {fmt(tv['tv_series_products'])} 个，关系 {fmt(tv['tv_series_relations'])} 条；{tv['note']}"]
        r = st["record_scopes"]
        lines += ["", "## 评论记录口径", "", "| 口径 | 记录数 |", "|---|---|",
                  f"| 经 edition 关系归到作品 | {fmt(r['edition'])} |", f"| 合集商品（不分给成员） | {fmt(r['collection'])} |",
                  f"| 剧集等其他有页面类别 | {fmt(r['other_video'])} |", f"| 无页面商品 | {fmt(r['no_page'])} |",
                  f"| 清单合计 | {fmt(r['manifest_total'])} |", f"| 无产品标识 | {fmt(r['records_without_asin'])} |", "",
                  f"清单合计 + 无产品标识 = {fmt(r['manifest_total'] + r['records_without_asin'])}，任务 1 读取数 "
                  f"{fmt(r['records_read'])}，一致：{r['consistent']}。{r['note']}", "",
                  "## 未知说明段（前 20，供补词表）", "", "| 原文 | 次数 |", "|---|---|"]
        lines += [f"| {u['raw']} | {fmt(u['count'])} |" for u in st["unknown_descriptors_top"][:20]]
        if s.get("examples_file"):
            lines += ["", f"实例见 `{s['examples_file']}`（{len(s['examples']['cross_edition_merges'])} 组跨版本合并、"
                          f"{len(s['examples']['confusable_not_merged'])} 组易混淆未合并）。"]
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
    ap = argparse.ArgumentParser(description="任务 4 统计与实例")
    ap.add_argument("cmd", choices=("summary", "examples", "export"))
    ap.add_argument("--extraction-id")
    ap.add_argument("--mapping-version")
    ap.add_argument("--run-key", default="manual")
    ap.add_argument("--count", type=int, default=EXAMPLES_PER_KIND)
    args = ap.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        v = args.mapping_version or latest_mapping_version(conn, eid)
        if args.cmd == "summary":
            out: dict[str, Any] = build_summary(conn, v)
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
