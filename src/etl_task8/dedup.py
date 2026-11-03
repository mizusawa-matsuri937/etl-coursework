"""review_dedup 阶段（spec R03–R05、R10、R11、R14–R25、R36）：分组核验、逻辑评论、ReviewMapping、Reviewer 与候选。

批次单元依次为：
- unmapped：隔离记录与不完整记录的映射（R05、R08）。
- verify：核验（R09、R17）。全部 (family_id, content_hash) 相同且多于一行的组，在库内对 review_staging 的完整原值
  逐项比较（min = max，COLLATE "C"）；原值不同的组（哈希碰撞）把原值取进 Python 拆组，每条记录写一条 hash_collision
  问题（detail 含子组序号），分区读这些问题得到拆组结果。一次扫描 review_staging 完成全部核验（演练实测 91 秒）；按分区各核验一次
  要 32 次随机读或顺序扫描 review_staging，单个分区 35–63 秒（design“演练实测”）。
- 按用户分区桶分 N 个分区（默认 32），每个分区一个事务：
1. 流式读取分区内的键完整记录（按用户、家族、内容键、record_no 排序），逐个用户处理：每组一条逻辑评论，
   层次由 rules.assign_levels 判定，helpfulness 由 rules.pick_helpfulness 选择，ID 由 rules.logical_review_id 生成。
2. 同一用户内：内容键相同而家族不同的逻辑评论记一组 cross_family_same_content（C1）；同一家族内正文不同的逻辑评论
   取代表记录正文两两算词集 Jaccard，≥ 阈值记 near_duplicate（C2）。统计组内（跨家族）的 C2 由 review_attribution 补算。
3. Reviewer：分区内全部有效记录（含不完整记录）的显示名变体与次数、记录数、逻辑评论数、首末评论日（R10、R25）。
结果、entity_stage 与 run_batch 同事务提交；写入按 50,000 行一批 COPY，分区数据不整体留在内存。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import sources
from .rules import (Copy, assign_levels, candidate_id, cross_family_groups, logical_review_id, near_duplicate_pairs,
                    partition_buckets, pick_helpfulness, split_collisions)
from .run import (DEFAULT_PARTITIONS, BeforeCommit, RunContext, copy_rows, done_units, mark_complete, mark_unit, next_batch_no,
                  register_stage_key, require_partitions, require_stage_complete, stage_complete)

STAGE = "review_dedup"
UNMAPPED = "unmapped"
VERIFY = "verify"
UNMAPPED_PARTITION = -1
FLUSH_ROWS = 50_000

_PARTITION_ROWS = """
FROM task8.review_clean c
JOIN task8.family_member m ON m.family_stage_key = %(fk)s AND m.asin = c.asin
WHERE c.clean_stage_key = %(ck)s AND c.key_complete AND c.reviewer_bucket = ANY(%(b)s)
"""

_KEYED = """
FROM task8.review_clean c
JOIN task8.family_member m ON m.family_stage_key = %(fk)s AND m.asin = c.asin
WHERE c.clean_stage_key = %(ck)s AND c.key_complete
"""

_COLLISIONS = f"""
WITH multi AS MATERIALIZED (
    SELECT m.family_id, c.content_hash {_KEYED} GROUP BY m.family_id, c.content_hash HAVING count(*) > 1)
SELECT x.family_id, x.content_hash
FROM task8.review_clean c
JOIN task8.family_member m ON m.family_stage_key = %(fk)s AND m.asin = c.asin
JOIN multi x ON x.family_id = m.family_id AND x.content_hash = c.content_hash
JOIN public.review_staging s ON s.extraction_id = %(eid)s AND s.source_review_id = c.source_review_id
WHERE c.clean_stage_key = %(ck)s AND c.key_complete
GROUP BY x.family_id, x.content_hash
HAVING NOT (min(s.reviewer_id COLLATE "C") = max(s.reviewer_id COLLATE "C")
            AND min((s.review_time_utc AT TIME ZONE 'UTC')::date) = max((s.review_time_utc AT TIME ZONE 'UTC')::date)
            AND min(s.score) = max(s.score)
            AND count(s.summary_raw) IN (0, count(*))
            AND min(s.summary_raw COLLATE "C") IS NOT DISTINCT FROM max(s.summary_raw COLLATE "C")
            AND min(s.review_text_raw COLLATE "C") = max(s.review_text_raw COLLATE "C"))
"""

_ROWS = f"""
SELECT c.reviewer_id, m.family_id, c.content_hash, c.record_no, c.source_review_id, c.asin, c.review_day, c.score,
       c.helpfulness_raw, c.helpfulness_status, c.helpful_votes, c.total_votes, c.profile_name_raw, c.text_md5
{_PARTITION_ROWS}
ORDER BY c.reviewer_id COLLATE "C", m.family_id COLLATE "C", c.content_hash, c.record_no
"""

_PROFILES = """
SELECT reviewer_id, profile_name_raw, count(*), min(review_day), max(review_day)
FROM task8.review_clean
WHERE clean_stage_key = %s AND reviewer_bucket = ANY(%s) AND reviewer_id IS NOT NULL
GROUP BY reviewer_id, profile_name_raw
"""

_UNMAPPED = """
INSERT INTO task8.review_mapping (dedup_stage_key, record_no, source_review_id, asin, logical_review_id, mapping_status,
                                  match_level, diff_fields, partition_no, rule_version)
SELECT %(dk)s, r.record_no, r.source_review_id, r.product_id_observed, NULL, 'quarantined', NULL, '{}'::text[], %(p)s, %(rv)s
FROM public.review_record r
WHERE r.extraction_id = %(eid)s AND r.classification = 'quarantined'
UNION ALL
SELECT %(dk)s, c.record_no, c.source_review_id, c.asin, NULL, 'incomplete', NULL, '{}'::text[], %(p)s, %(rv)s
FROM task8.review_clean c
WHERE c.clean_stage_key = %(ck)s AND NOT c.key_complete
"""

_TABLES = {
    "mapping": ("task8.review_mapping", ("dedup_stage_key", "record_no", "source_review_id", "asin", "logical_review_id",
                                         "mapping_status", "match_level", "diff_fields", "partition_no", "rule_version")),
    "logical": ("task8.logical_review", ("dedup_stage_key", "logical_review_id", "reviewer_id", "review_day", "score",
                                         "content_hash", "text_md5", "family_id", "collision_variant",
                                         "representative_record_no", "copy_count", "asin_count", "helpful_votes",
                                         "total_votes", "helpfulness_record_no", "partition_no")),
    "reviewer": ("task8.reviewer", ("dedup_stage_key", "reviewer_id", "profile_names", "profile_variant_count",
                                    "record_count", "logical_count", "first_day", "last_day", "partition_no")),
    "candidate": ("task8.dup_candidate", ("stage_key", "candidate_id", "stage", "candidate_type", "reviewer_id",
                                          "logical_review_ids", "similarity", "basis")),
    "issue": ("task8.review_issue", ("stage_key", "issue_key", "record_no", "source_review_id", "code", "evidence",
                                     "detail", "rule_version")),
}


class _Writer:
    """按表缓冲待写行，满 FLUSH_ROWS 行即 COPY（在分区事务内；流式游标打开期间也可执行）。"""

    def __init__(self, conn: psycopg.Connection, limit: int = FLUSH_ROWS):
        self.conn, self.limit = conn, limit
        self.buf: dict[str, list[tuple]] = {k: [] for k in _TABLES}
        self.written = 0

    def add(self, kind: str, row: tuple) -> None:
        self.buf[kind].append(row)
        if len(self.buf[kind]) >= self.limit:
            self._flush(kind)

    def _flush(self, kind: str) -> None:
        rows, self.buf[kind] = self.buf[kind], []
        if not rows:
            return
        table, cols = _TABLES[kind]
        with self.conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        self.written += len(rows)

    def close(self) -> None:
        for kind in _TABLES:
            self._flush(kind)


def dedup_reviews(conn: psycopg.Connection, ctx: RunContext, *, partitions: int = DEFAULT_PARTITIONS,
                  before_commit: BeforeCommit | None = None, flush_rows: int = FLUSH_ROWS) -> dict[str, Any]:
    """R03–R05、R10、R14–R25、R36：未映射记录一个单元，其余按用户分区；返回本次写入的计数。"""
    ck = require_stage_complete(conn, ctx, "review_clean")
    fk = require_stage_complete(conn, ctx, "review_family")
    dk = register_stage_key(conn, ctx, STAGE)
    counts: dict[str, Any] = {"stage_key": dk, "partitions": partitions, "units": 0, "quarantined": 0,
                              "incomplete": 0, "logical_reviews": 0, "reviewers": 0, "match_level": {},
                              "candidates": {}, "collision_groups": 0, "collision_records": 0}
    if stage_complete(conn, dk):
        counts["skipped"] = "complete"
        return counts
    done = done_units(conn, dk)
    require_partitions(done, "p", partitions)
    pending = [u for u in [UNMAPPED, VERIFY] + [f"p{p}/{partitions}" for p in range(partitions)] if u not in done]
    batch_no = next_batch_no(conn, ctx, STAGE)
    for unit in pending:
        with conn.transaction():
            if unit == UNMAPPED:
                part = _unmapped(conn, ctx, ck, dk)
            elif unit == VERIFY:
                part = _verify(conn, ctx, ck, fk, dk)
            else:
                part = _partition(conn, ctx, ck, fk, dk, int(unit[1:].split("/")[0]), partitions, flush_rows)
            mark_unit(conn, ctx, STAGE, dk, batch_no, unit, part["rows"], complete=unit == pending[-1])
            if before_commit:
                before_commit(STAGE, batch_no)
        _merge(counts, part)
        counts["units"] += 1
        batch_no += 1
    if not stage_complete(conn, dk):
        mark_complete(conn, ctx, STAGE, dk, batch_no)
    return counts


def _merge(total: dict[str, Any], part: dict[str, Any]) -> None:
    for k, v in part.items():
        if k == "rows":
            continue
        if isinstance(v, dict):
            for kk, vv in v.items():
                total[k][kk] = total[k].get(kk, 0) + vv
        else:
            total[k] += v


def _unmapped(conn: psycopg.Connection, ctx: RunContext, ck: str, dk: str) -> dict[str, Any]:
    """R05、R08：隔离记录与不完整记录各一行映射，逻辑评论与层次为 NULL。"""
    conn.execute(_UNMAPPED, {"dk": dk, "ck": ck, "eid": ctx.inputs.extraction_id, "p": UNMAPPED_PARTITION,
                             "rv": ctx.rules.version})
    q, i = conn.execute("SELECT count(*) FILTER (WHERE mapping_status = 'quarantined'), "
                        "count(*) FILTER (WHERE mapping_status = 'incomplete') FROM task8.review_mapping "
                        "WHERE dedup_stage_key = %s AND partition_no = %s", (dk, UNMAPPED_PARTITION)).fetchone()
    return {"rows": q + i, "quarantined": q, "incomplete": i}


def _verify(conn: psycopg.Connection, ctx: RunContext, ck: str, fk: str, dk: str) -> dict[str, Any]:
    """R09、R17：一次核验全部多行分组的完整原值；原值不同的组拆成子组，每条记录写一条 hash_collision 问题。"""
    params = {"fk": fk, "ck": ck, "eid": ctx.inputs.extraction_id}
    # 只在这条查询上：规划器把多行分组估成几百行而选嵌套循环（600 万次随机读 review_staging，演练中 10 分钟未完成）；
    # 关掉嵌套循环后为一次顺序扫描加哈希连接，演练实测 91 秒
    # 并行哈希连接把哈希表放在共享内存（/dev/shm，容器默认 1 GB）里，256MB × hash_mem_multiplier 的段在 T8-04 演练中
    # 报 DiskFull；关掉并行哈希后各进程在本地内存建表，不占 /dev/shm
    conn.execute("SET LOCAL work_mem = '256MB'")
    conn.execute("SET LOCAL enable_nestloop = off")
    conn.execute("SET LOCAL enable_parallel_hash = off")
    groups = {(fam, bytes(h)) for fam, h in conn.execute(_COLLISIONS, params)}
    conn.execute("SET LOCAL enable_parallel_hash TO DEFAULT")
    conn.execute("SET LOCAL enable_nestloop TO DEFAULT")
    conn.execute("SET LOCAL work_mem TO DEFAULT")
    counts: dict[str, Any] = {"rows": 0, "collision_groups": len(groups), "collision_records": 0}
    if not groups:
        return counts
    rows: dict[tuple[str, bytes], list[tuple[int, str]]] = defaultdict(list)
    for fam, h, rn, sid in conn.execute(f"SELECT m.family_id, c.content_hash, c.record_no, c.source_review_id {_KEYED} "
                                        "AND c.content_hash = ANY(%(hashes)s)", {**params, "hashes": [h for _, h in groups]}):
        if (fam, bytes(h)) in groups:
            rows[(fam, bytes(h))].append((rn, sid))
    raw = sources.raw_values(conn, ctx.inputs.extraction_id, [sid for recs in rows.values() for _, sid in recs])
    issues = []
    for (fam, h), recs in sorted(rows.items()):
        sid_of = dict(recs)
        subgroups = split_collisions([(rn, raw[sid_of[rn]]) for rn, _ in recs])
        for variant, sub in enumerate(subgroups):
            for rn in sub:
                issues.append((dk, f"hash_collision:{rn}", rn, sid_of[rn], "hash_collision", h.hex(),
                               Jsonb({"family_id": fam, "subgroups": len(subgroups), "variant": variant}),
                               ctx.rules.version))
    copy_rows(conn, *_TABLES["issue"], issues)
    counts["rows"] = counts["collision_records"] = len(issues)
    return counts


def _collision_split(conn: psycopg.Connection, dk: str) -> dict[tuple[str, bytes], dict[int, int]]:
    """verify 单元写下的拆组结果 → {(family_id, content_hash): {record_no: 子组序号}}。"""
    split: dict[tuple[str, bytes], dict[int, int]] = defaultdict(dict)
    for rn, h, fam, variant in conn.execute(
            "SELECT record_no, evidence, detail->>'family_id', (detail->>'variant')::int FROM task8.review_issue "
            "WHERE stage_key = %s AND code = 'hash_collision'", (dk,)):
        split[(fam, bytes.fromhex(h))][rn] = variant
    return dict(split)


def _profiles(conn: psycopg.Connection, ck: str, buckets: list[int]) -> dict[str, dict[str, Any]]:
    """分区内每个用户的显示名变体与次数、有效记录数、首末评论日（含不完整记录）。"""
    out: dict[str, dict[str, Any]] = {}
    for reviewer, name, n, first, last in conn.execute(_PROFILES, (ck, buckets)):
        p = out.setdefault(reviewer, {"names": {}, "records": 0, "first": None, "last": None})
        if name is not None:
            p["names"][name] = n
        p["records"] += n
        if first is not None:
            p["first"] = first if p["first"] is None else min(p["first"], first)
            p["last"] = last if p["last"] is None else max(p["last"], last)
    return out


def _partition(conn: psycopg.Connection, ctx: RunContext, ck: str, fk: str, dk: str, p: int, partitions: int,
               flush_rows: int) -> dict[str, Any]:
    eid, rv, rules = ctx.inputs.extraction_id, ctx.rules.version, ctx.rules
    params = {"fk": fk, "ck": ck, "eid": eid, "b": partition_buckets(p, partitions)}
    split = _collision_split(conn, dk)
    profiles = _profiles(conn, ck, params["b"])
    w = _Writer(conn, flush_rows)
    counts: dict[str, Any] = {"logical_reviews": 0, "reviewers": 0, "match_level": defaultdict(int),
                              "candidates": defaultdict(int)}
    near_scope: list[tuple[str, str, list[tuple[str, bytes, str]]]] = []     # (用户, 家族, [(ID, 正文 md5, 代表记录)])

    def candidate(kind: str, reviewer: str, ids: list[str], sim: float | None, basis: dict[str, Any]) -> None:
        w.add("candidate", (dk, candidate_id(kind, ids), STAGE, kind, reviewer, sorted(ids), sim, Jsonb(basis)))
        counts["candidates"][kind] += 1

    def one_reviewer(reviewer: str, rows: list[tuple]) -> None:
        groups: dict[tuple[str, bytes], list[tuple]] = {}
        for r in rows:
            groups.setdefault((r[1], bytes(r[2])), []).append(r)
        mine: list[tuple[str, bytes, str, bytes, str, bool]] = []     # (ID, 内容键, 家族, 正文 md5, 代表 srid, 碰撞)
        for (fam, h), grp in groups.items():
            variant_of = split.get((fam, h))
            subs: dict[int, list[tuple]] = defaultdict(list)
            for r in grp:
                subs[variant_of[r[3]] if variant_of else 0].append(r)
            for variant, sub in sorted(subs.items()):
                rep = sub[0]                                   # 已按 record_no 排序
                copies = [Copy(r[3], r[5], r[8], r[12], r[9], r[10], r[11]) for r in sub]
                lid = logical_review_id(h, fam, variant)
                hv, tv, hrec = pick_helpfulness(copies)
                w.add("logical", (dk, lid, reviewer, rep[6], rep[7], h, bytes(rep[13]), fam, variant, rep[3], len(sub),
                                  len({r[5] for r in sub}), hv, tv, hrec, p))
                asin_of = {r[3]: (r[4], r[5]) for r in sub}
                for rn, level, diff in assign_levels(copies):
                    w.add("mapping", (dk, rn, asin_of[rn][0], asin_of[rn][1], lid, "mapped", level, diff, p, rv))
                    counts["match_level"][level] += 1
                mine.append((lid, h, fam, bytes(rep[13]), rep[4], bool(variant_of)))
                counts["logical_reviews"] += 1
        for ids in cross_family_groups((lid, h, fam) for lid, h, fam, _, _, collided in mine if not collided):
            fams = sorted({fam for lid, _, fam, _, _, _ in mine if lid in ids})
            hh = next(h for lid, h, _, _, _, _ in mine if lid == ids[0])
            candidate("cross_family_same_content", reviewer, ids, None, {"content_hash": hh.hex(), "families": fams})
        by_family: dict[str, list[tuple[str, bytes, str]]] = defaultdict(list)
        for lid, _, fam, md5, sid, _ in mine:
            by_family[fam].append((lid, md5, sid))
        for fam, items in by_family.items():
            if len({md5 for _, md5, _ in items}) >= 2:
                near_scope.append((reviewer, fam, items))
        prof = profiles.pop(reviewer)
        w.add("reviewer", (dk, reviewer, Jsonb(prof["names"]), len(prof["names"]), prof["records"], len(mine),
                           prof["first"], prof["last"], p))
        counts["reviewers"] += 1

    with conn.cursor(name=f"task8_dedup_p{p}") as cur:
        cur.itersize = 20_000
        cur.execute(_ROWS, params)
        current: str | None = None
        rows: list[tuple] = []
        for row in cur:
            if row[0] != current and rows:
                one_reviewer(current, rows)
                rows = []
            current = row[0]
            rows.append(row)
        if rows:
            one_reviewer(current, rows)
    for reviewer, prof in sorted(profiles.items()):           # 只有不完整记录的用户
        w.add("reviewer", (dk, reviewer, Jsonb(prof["names"]), len(prof["names"]), prof["records"], 0,
                           prof["first"], prof["last"], p))
        counts["reviewers"] += 1
    if near_scope:
        texts = sources.review_texts(conn, eid, [sid for _, _, items in near_scope for _, _, sid in items])
        for reviewer, fam, items in near_scope:
            for a, b, sim in near_duplicate_pairs([(lid, md5, texts.get(sid)) for lid, md5, sid in items],
                                                  rules.near_dup_jaccard, rules.token_pattern):
                candidate("near_duplicate", reviewer, [a, b], sim, {"scope": "family", "family_id": fam})
    w.close()
    counts["rows"] = w.written
    counts["match_level"] = dict(counts["match_level"])
    counts["candidates"] = dict(counts["candidates"])
    return counts
