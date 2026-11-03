"""去重版本的物化与差异（spec R26、R30–R33、R38）。

finalize_version 在一个事务里写 dedup_version（状态 unverified，记录输入键与四个阶段键）与按版本物化的三张统计表；
verify 通过才由 mark_verified 把版本标为 verified、运行标为 succeeded（R38）。同一输入键已有未核实的版本（上次 verify
失败）时，在同一个事务里清掉它的统计行再重写，版本号不变；已核实的版本不能重写（应当 reused）。
大表（逻辑评论、映射、归属等）按阶段键保存、被多个版本共用，物化不复制它们。

统计口径（design“评分查询口径”）：
- 商品（product_review_stats）：每个出现在映射里的 ASIN。去重前为该 ASIN 上的原始有效记录（含不完整记录）与其评分；
  去重后为该 ASIN 上的不同逻辑评论（同一逻辑评论在同一 ASIN 只计一次，R26）。
- 作品（movie_review_stats）：去重前为该作品单品 ASIN 上的原始有效记录；去重后为归属该作品的逻辑评论（不含合集，R30），
  另列与同组其他作品共享的逻辑评论数、两端都归属该作品的近似评论对数。
- 统计组（stat_group_review_stats）：组内每条逻辑评论只计一次；另给把每组近似评论只计一次时的敏感性分母与平均分（R31）。
平均分的分母是有评分的记录或逻辑评论数（逻辑评论都有评分）。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import sources
from .rules import sensitivity
from .run import STAGES, RunContext, Task8InputError, copy_rows, require_stage_complete

STATS_TABLES = ("product_review_stats", "movie_review_stats", "stat_group_review_stats")
_NEAR = "candidate_type = 'near_duplicate' AND stage_key IN (%(dk)s, %(ak)s)"

_PRODUCT = f"""
WITH raw AS (
    SELECT asin, count(*) AS valid, count(score) AS rated, avg(score) AS avg_score
    FROM task8.review_clean WHERE clean_stage_key = %(ck)s GROUP BY asin),
q AS (
    SELECT asin, count(*) AS n FROM task8.review_mapping
    WHERE dedup_stage_key = %(dk)s AND mapping_status = 'quarantined' AND asin IS NOT NULL GROUP BY asin),
lg AS (
    SELECT x.asin, count(*) AS n, avg(l.score) AS avg_score
    FROM (SELECT DISTINCT asin, logical_review_id FROM task8.review_mapping
          WHERE dedup_stage_key = %(dk)s AND mapping_status = 'mapped') x
    JOIN task8.logical_review l ON l.dedup_stage_key = %(dk)s AND l.logical_review_id = x.logical_review_id
    GROUP BY x.asin),
a AS (SELECT asin FROM raw UNION SELECT asin FROM q)
INSERT INTO task8.product_review_stats (review_dedup_version_id, asin, product_category, has_movie, raw_valid,
    raw_quarantined, raw_rated, raw_avg_score, logical_reviews, logical_rated, logical_avg_score)
SELECT %(rd)s, a.asin, {sources.CATEGORY_SQL}, cardinality(coalesce(e.movie_ids, '{{}}')) > 0,
       coalesce(raw.valid, 0), coalesce(q.n, 0), coalesce(raw.rated, 0), raw.avg_score,
       coalesce(lg.n, 0), coalesce(lg.n, 0), lg.avg_score
FROM a
LEFT JOIN raw ON raw.asin = a.asin
LEFT JOIN q ON q.asin = a.asin
LEFT JOIN lg ON lg.asin = a.asin
LEFT JOIN task3.product_classification pc ON pc.extraction_id = %(eid)s AND pc.asin = a.asin AND pc.rule_version = %(t3)s
LEFT JOIN task7.edition_resolved e ON e.conflict_version_id = %(cv)s AND e.asin = a.asin
"""

# 作品：去重前（单品上的原始有效记录）与去重后（归属的逻辑评论）
_MOVIE_RAW = """
SELECT e.movie_ids[1], count(*), count(c.score), sum(c.score)
FROM task7.edition_resolved e
JOIN task8.review_mapping m ON m.dedup_stage_key = %(dk)s AND m.asin = e.asin AND m.mapping_status <> 'quarantined'
JOIN task8.review_clean c ON c.clean_stage_key = %(ck)s AND c.record_no = m.record_no
WHERE e.conflict_version_id = %(cv)s AND e.product_kind = 'movie' AND cardinality(e.movie_ids) >= 1
GROUP BY 1
"""

_MOVIE_LOGICAL = """
SELECT w.movie_id, count(*), count(l.score), sum(l.score),
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM task8.logical_review_work o
                                      WHERE o.attribution_stage_key = w.attribution_stage_key
                                        AND o.logical_review_id = w.logical_review_id AND o.movie_id <> w.movie_id))
FROM task8.logical_review_work w
JOIN task8.logical_review l ON l.dedup_stage_key = %(dk)s AND l.logical_review_id = w.logical_review_id
WHERE w.attribution_stage_key = %(ak)s
GROUP BY 1
"""

# 两端都在归属里的近似评论对：每条逻辑评论属于哪些作品（同一逻辑评论的作品都在同一统计组）
_NEAR_PAIRS = f"""
SELECT c.candidate_id, c.logical_review_ids FROM task8.dup_candidate c
WHERE {_NEAR}
  AND EXISTS (SELECT 1 FROM task8.logical_review_work w WHERE w.attribution_stage_key = %(ak)s
              AND w.logical_review_id = ANY(c.logical_review_ids))
"""


def next_version_id(conn: psycopg.Connection) -> str:
    n = conn.execute("SELECT count(*) FROM task8.dedup_version").fetchone()[0]
    return f"rd{n + 1:04d}"


def version_keys(ctx: RunContext) -> dict[str, str]:
    return {s: ctx.key(s) for s in STAGES}


def finalize_version(conn: psycopg.Connection, ctx: RunContext) -> tuple[str, dict[str, Any]]:
    """R33、R38：单事务物化版本（状态 unverified）。返回 (去重版本号, 版本计数)。上游阶段未完成时确定性失败。"""
    i = ctx.inputs
    keys = {s: require_stage_complete(conn, ctx, s) for s in STAGES}
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task8_versions'))")
        existing = conn.execute("SELECT review_dedup_version_id, status FROM task8.dedup_version "
                                "WHERE extraction_id = %s AND input_key = %s", (i.extraction_id, i.key())).fetchone()
        if existing is not None and existing[1] == "verified":
            raise Task8InputError(f"版本 {existing[0]} 已通过核对，应当复用而不是重写")
        if existing is not None:      # 上次 verify 失败留下的未核实版本：同事务清掉统计行后重写
            rd = existing[0]
            for t in STATS_TABLES:
                conn.execute(f"DELETE FROM task8.{t} WHERE review_dedup_version_id = %s", (rd,))
            conn.execute("UPDATE task8.dedup_version SET run_id = %s, created_at = clock_timestamp(), "
                         "clean_stage_key = %s, family_stage_key = %s, dedup_stage_key = %s, attribution_stage_key = %s "
                         "WHERE review_dedup_version_id = %s",
                         (ctx.run_id, keys["review_clean"], keys["review_family"], keys["review_dedup"],
                          keys["review_attribution"], rd))
        else:
            rd = next_version_id(conn)
            parent = conn.execute("SELECT review_dedup_version_id FROM task8.dedup_version WHERE extraction_id = %s "
                                  "ORDER BY created_at DESC, review_dedup_version_id DESC LIMIT 1",
                                  (i.extraction_id,)).fetchone()
            conn.execute(
                "INSERT INTO task8.dedup_version (review_dedup_version_id, extraction_id, parent_version_id, run_id, "
                "input_key, t3_rule_version, conflict_version_id, rule_version, clean_stage_key, family_stage_key, "
                "dedup_stage_key, attribution_stage_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rd, i.extraction_id, parent[0] if parent else None, ctx.run_id, i.key(), i.t3_rule_version,
                 i.conflict_version_id, i.rule_version, keys["review_clean"], keys["review_family"],
                 keys["review_dedup"], keys["review_attribution"]))
        p = {"rd": rd, "eid": i.extraction_id, "t3": i.t3_rule_version, "cv": i.conflict_version_id,
             "ck": keys["review_clean"], "dk": keys["review_dedup"], "ak": keys["review_attribution"]}
        n: dict[str, Any] = {"product_review_stats": conn.execute(_PRODUCT, p).rowcount}
        n["movie_review_stats"], n["stat_group_review_stats"] = _movie_and_group_stats(conn, p)
        counts = {**version_counts(conn, p), "materialized": n}
        conn.execute("UPDATE task8.dedup_version SET counts = %s WHERE review_dedup_version_id = %s",
                     (Jsonb(counts), rd))
        conn.execute("UPDATE task8.run SET review_dedup_version_id = %s WHERE run_id = %s", (rd, ctx.run_id))
    return rd, counts


def _avg(total: Decimal | None, n: int) -> Decimal | None:
    return None if not n or total is None else Decimal(total) / n


def _movie_and_group_stats(conn: psycopg.Connection, p: dict[str, Any]) -> tuple[int, int]:
    """作品与统计组的统计行（R30、R31）。作品与组的数量为数千，在 Python 里汇总后 COPY。"""
    group_of = dict(conn.execute("SELECT movie_id, stat_group_id FROM task8.stat_group_member "
                                 "WHERE attribution_stage_key = %s", (p["ak"],)).fetchall())
    raw = {m: (n, rated, total) for m, n, rated, total in conn.execute(_MOVIE_RAW, p)}
    logical = {m: (n, rated, total, shared) for m, n, rated, total, shared in conn.execute(_MOVIE_LOGICAL, p)}
    # 近似评论对（review_dedup 的家族内对与 review_attribution 的统计组内对）：两端各属哪些作品
    movies_of: dict[str, set[str]] = defaultdict(set)
    pairs = [(cid, list(ids)) for cid, ids in conn.execute(_NEAR_PAIRS, p)]
    lids = sorted({lid for _, ids in pairs for lid in ids})
    if lids:
        for lid, m in conn.execute("SELECT logical_review_id, movie_id FROM task8.logical_review_work "
                                   "WHERE attribution_stage_key = %s AND logical_review_id = ANY(%s)", (p["ak"], lids)):
            movies_of[lid].add(m)
    movie_pairs: dict[str, int] = defaultdict(int)
    group_pairs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for _, ids in pairs:
        a, b = ids[0], ids[1]
        for m in movies_of[a] & movies_of[b]:
            movie_pairs[m] += 1
        ga = {group_of[m] for m in movies_of[a]}
        gb = {group_of[m] for m in movies_of[b]}
        for g in ga & gb:
            group_pairs[g].append((a, b))
    rows = []
    for m, g in sorted(group_of.items()):
        rn, rr, rt = raw.get(m, (0, 0, None))
        ln, lr, lt, sh = logical.get(m, (0, 0, None, 0))
        rows.append((p["rd"], m, g, rn, rr, _avg(rt, rr), ln, lr, _avg(lt, lr), sh, movie_pairs.get(m, 0)))
    copy_rows(conn, "task8.movie_review_stats",
              ("review_dedup_version_id", "movie_id", "stat_group_id", "raw_records", "raw_rated", "raw_avg_score",
               "logical_reviews", "logical_rated", "logical_avg_score", "shared_with_group", "near_dup_candidates"), rows)
    # 统计组：组内每条逻辑评论只计一次
    members: dict[str, list[str]] = defaultdict(list)
    for m, g in group_of.items():
        members[g].append(m)
    glog = {g: (n, rated, total) for g, n, rated, total in conn.execute(
        "SELECT g.stat_group_id, count(*), count(l.score), sum(l.score) FROM ("
        "SELECT DISTINCT s.stat_group_id, w.logical_review_id FROM task8.logical_review_work w "
        "JOIN task8.stat_group_member s ON s.attribution_stage_key = w.attribution_stage_key AND s.movie_id = w.movie_id "
        "WHERE w.attribution_stage_key = %(ak)s) g "
        "JOIN task8.logical_review l ON l.dedup_stage_key = %(dk)s AND l.logical_review_id = g.logical_review_id "
        "GROUP BY 1", p)}
    # 敏感性：只为有近似对的组取组内逻辑评论的评分与评论日
    items: dict[str, dict[str, tuple[Decimal, Any]]] = defaultdict(dict)
    if group_pairs:
        for g, lid, score, day in conn.execute(
                "SELECT DISTINCT s.stat_group_id, w.logical_review_id, l.score, l.review_day "
                "FROM task8.logical_review_work w JOIN task8.stat_group_member s ON s.attribution_stage_key = "
                "w.attribution_stage_key AND s.movie_id = w.movie_id JOIN task8.logical_review l ON l.dedup_stage_key = "
                "%(dk)s AND l.logical_review_id = w.logical_review_id WHERE w.attribution_stage_key = %(ak)s "
                "AND s.stat_group_id = ANY(%(groups)s)", {**p, "groups": sorted(group_pairs)}):
            items[g][lid] = (score, day)
    grows = []
    for g, ms in sorted(members.items()):
        rn = sum(raw.get(m, (0, 0, None))[0] for m in ms)
        rr = sum(raw.get(m, (0, 0, None))[1] for m in ms)
        rt = sum((raw[m][2] for m in ms if m in raw and raw[m][2] is not None), Decimal(0))
        ln, lr, lt = glog.get(g, (0, 0, None))
        if g in group_pairs:
            s_rated, s_avg, _ = sensitivity(items[g], group_pairs[g])
        else:
            s_rated, s_avg = lr, _avg(lt, lr)
        grows.append((p["rd"], g, len(ms), rn, rr, _avg(rt, rr), ln, lr, _avg(lt, lr), len(group_pairs.get(g, ())),
                      s_rated, s_avg))
    copy_rows(conn, "task8.stat_group_review_stats",
              ("review_dedup_version_id", "stat_group_id", "movie_count", "raw_records", "raw_rated", "raw_avg_score",
               "logical_reviews", "logical_rated", "logical_avg_score", "near_dup_pairs", "sensitivity_rated",
               "sensitivity_avg_score"), grows)
    return len(rows), len(grows)


def version_counts(conn: psycopg.Connection, p: dict[str, Any]) -> dict[str, Any]:
    """版本摘要计数（R33、R42）：映射、层次、逻辑评论、家族、候选、归属与统计组。"""
    def one(sql: str, **extra: Any) -> Any:
        return conn.execute(sql, {**p, **extra}).fetchone()[0]

    def tally(sql: str) -> dict[str, int]:
        return {str(k): int(v) for k, v in conn.execute(sql, p)}

    fk = conn.execute("SELECT parent_stage_key FROM task8.stage_key WHERE stage_key = %s", (p["dk"],)).fetchone()[0]
    status = tally("SELECT mapping_status, count(*) FROM task8.review_mapping WHERE dedup_stage_key = %(dk)s GROUP BY 1")
    levels = tally("SELECT match_level, count(*) FROM task8.review_mapping WHERE dedup_stage_key = %(dk)s "
                   "AND match_level IS NOT NULL GROUP BY 1")
    logical = one("SELECT count(*) FROM task8.logical_review WHERE dedup_stage_key = %(dk)s")
    return {
        "records": sum(status.values()), "mapping_status": status, "match_level": levels,
        "logical_reviews": logical,
        "duplicates": sum(v for k, v in levels.items() if k != "representative"),
        "reviewers": one("SELECT count(*) FROM task8.reviewer WHERE dedup_stage_key = %(dk)s"),
        "families": one("SELECT count(*) FROM task8.review_family WHERE family_stage_key = %(fk)s", fk=fk),
        "multi_asin_families": one("SELECT count(*) FROM task8.review_family WHERE family_stage_key = %(fk)s "
                                   "AND asin_count > 1", fk=fk),
        "weak_support_multi_asin_families": one("SELECT count(*) FROM task8.review_family WHERE family_stage_key = "
                                                "%(fk)s AND asin_count > 1 AND weak_support", fk=fk),
        "candidates": tally("SELECT candidate_type || ':' || stage, count(*) FROM task8.dup_candidate "
                            "WHERE stage_key IN (%(dk)s, %(ak)s) GROUP BY 1"),
        "attributed_logical_reviews": one("SELECT count(DISTINCT logical_review_id) FROM task8.logical_review_work "
                                          "WHERE attribution_stage_key = %(ak)s"),
        "work_rows": one("SELECT count(*) FROM task8.logical_review_work WHERE attribution_stage_key = %(ak)s"),
        "stat_groups": one("SELECT count(*) FROM task8.stat_group WHERE attribution_stage_key = %(ak)s"),
        "multi_movie_groups": one("SELECT count(*) FROM task8.stat_group WHERE attribution_stage_key = %(ak)s "
                                  "AND movie_count > 1"),
    }


def mark_verified(conn: psycopg.Connection, rd: str, run_id: str, counts: dict[str, Any]) -> None:
    """R38：verify 通过后单事务把版本标为 verified、运行标为 succeeded。"""
    with conn.transaction():
        conn.execute("UPDATE task8.dedup_version SET status = 'verified', verified_at = clock_timestamp() "
                     "WHERE review_dedup_version_id = %s", (rd,))
        conn.execute("UPDATE task8.run SET status = 'succeeded', counts = %s, finished_at = clock_timestamp(), "
                     "failure_reason = NULL, review_dedup_version_id = %s WHERE run_id = %s", (Jsonb(counts), rd, run_id))


# ---------------------------------------------------------------- 版本差异（R33）

def _flat(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def diff_versions(conn: psycopg.Connection, old: str, new: str) -> list[tuple[str, str, str, Any, Any, str]]:
    """两个去重版本的差异：(层, 实体, 字段, 旧, 新, 说明)。
    层：stage（哪些阶段键变了，即哪些阶段重算）、count（版本计数）、movie（作品的统计组、去重后评论数与平均分、共享数）、
    group（统计组的作品数与评论数）、product（商品的逻辑评论数与平均分）。"""
    cols = ("clean_stage_key", "family_stage_key", "dedup_stage_key", "attribution_stage_key", "counts")
    rows = {}
    for v in (old, new):
        r = conn.execute(f"SELECT {', '.join(cols)} FROM task8.dedup_version WHERE review_dedup_version_id = %s",
                         (v,)).fetchone()
        if r is None:
            raise ValueError(f"去重版本 {v} 不存在")
        rows[v] = dict(zip(cols, r))
    out: list[tuple[str, str, str, Any, Any, str]] = []
    for c in cols[:4]:
        if rows[old][c] != rows[new][c]:
            out.append(("stage", c.removesuffix("_stage_key"), c, rows[old][c], rows[new][c], "阶段键变化（该阶段重算）"))
    a, b = _flat(dict(rows[old]["counts"])), _flat(dict(rows[new]["counts"]))
    for k in sorted(set(a) | set(b)):
        if k.startswith("materialized.") or a.get(k) == b.get(k):
            continue
        out.append(("count", "version", k, a.get(k), b.get(k), "计数变化"))
    for layer, table, entity, fields in (
            ("movie", "movie_review_stats", "movie_id",
             ("stat_group_id", "logical_reviews", "logical_avg_score", "shared_with_group", "near_dup_candidates")),
            ("group", "stat_group_review_stats", "stat_group_id", ("movie_count", "logical_reviews", "logical_avg_score")),
            ("product", "product_review_stats", "asin", ("logical_reviews", "logical_avg_score", "product_category"))):
        sel = ", ".join(f"o.{f}, n.{f}" for f in fields)
        cond = " OR ".join(f"o.{f} IS DISTINCT FROM n.{f}" for f in fields)
        for r in conn.execute(
                f"SELECT coalesce(o.{entity}, n.{entity}), o.{entity} IS NULL, n.{entity} IS NULL, {sel} "
                f"FROM (SELECT * FROM task8.{table} WHERE review_dedup_version_id = %s) o "
                f"FULL OUTER JOIN (SELECT * FROM task8.{table} WHERE review_dedup_version_id = %s) n "
                f"ON n.{entity} = o.{entity} WHERE o.{entity} IS NULL OR n.{entity} IS NULL OR {cond} ORDER BY 1",
                (old, new)):
            ent, added, removed, vals = r[0], r[1], r[2], r[3:]
            if added:
                out.append((layer, ent, "*", "ABSENT", "PRESENT", "新增"))
                continue
            if removed:
                out.append((layer, ent, "*", "PRESENT", "ABSENT", "移除"))
                continue
            for k, f in enumerate(fields):
                o, nv = vals[2 * k], vals[2 * k + 1]
                if o != nv:
                    out.append((layer, ent, f, o, nv, "变化"))
    return out
