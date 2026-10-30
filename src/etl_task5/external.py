"""外部人物 ID：途径一（关联作品的演职员）与途径二（已核实人物的作品年表反查）（spec R10–R16，AC03、AC04）。

- 途径一（阶段 credits）：按商品分批；电影类取 edition 关系的 imdb_linked 作品，合集取 contains 成员里 imdb_linked 的；
  导演对 imdb.title_director，演员对 imdb.title_principal 的 actor/actress；只存姓名兼容（X1–X4）的候选，每个候选一条决策。
- 途径二（阶段 filmography）：在途径一全部完成后按商品分批。只处理 page_only 作品的电影类提及与未归属的合集提及；
  已核实集合 = 途径一 auto 的 (角色, 严格键) → nconst，在阶段开始时算出，哈希写进运行的 params，保证分批重跑用同一集合。
  只读 imdb.* 与 task4.*，不写任务 1–4 的任何表。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import T4_NORM_VERSION, names
from .rules import CreditCand, FilmHit, RuleSet, decide_credit_links, decide_filmography
from .run import (BeforeCommit, RunContext, Task5InputError, copy_rows, mark_batch, mark_entities, next_batch_no)

CREDITS, FILMOGRAPHY = "credits", "filmography"
CAND_COLUMNS = ("extraction_id", "stage_key", "mention_key", "route", "tconst", "nconst", "category", "level",
                "cross_role", "work_movie_id", "anchor_link_decision_id", "matched_title_key", "title_key_source",
                "evidence")
ROLE_CATEGORIES = {"director": ("director",), "actor": ("actor", "actress")}


@dataclass
class LinkStats:
    entities: int = 0
    candidates: int = 0
    batches: int = 0
    remaining: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    hints: int = 0


def _count_pending(conn: psycopg.Connection, ctx: RunContext, stage: str) -> int:
    i = ctx.inputs
    return conn.execute(
        """SELECT count(*) FROM task4.product_edition e JOIN task4.mapping_version m
                ON m.extraction_id = e.extraction_id AND m.t3_rule_version = e.t3_rule_version
           WHERE m.mapping_version_id = %(m)s AND e.match_scope = 'matched'
             AND NOT EXISTS (SELECT 1 FROM task5.entity_stage s WHERE s.extraction_id = e.extraction_id
                             AND s.stage = %(stage)s AND s.stage_key = %(key)s AND s.entity_id = e.asin)""",
        {"m": i.t4_mapping_version_id, "stage": stage, "key": i.stage_key(stage)}).fetchone()[0]


def count_pending(conn: psycopg.Connection, ctx: RunContext, stage: str = CREDITS) -> int:
    return _count_pending(conn, ctx, stage)


def _pending(conn: psycopg.Connection, ctx: RunContext, stage: str, after: str, n: int) -> list[str]:
    i = ctx.inputs
    return [r[0] for r in conn.execute(
        """SELECT e.asin FROM task4.product_edition e JOIN task4.mapping_version m
                ON m.extraction_id = e.extraction_id AND m.t3_rule_version = e.t3_rule_version
           WHERE m.mapping_version_id = %(m)s AND e.match_scope = 'matched' AND e.asin COLLATE "C" > %(after)s
             AND NOT EXISTS (SELECT 1 FROM task5.entity_stage s WHERE s.extraction_id = e.extraction_id
                             AND s.stage = %(stage)s AND s.stage_key = %(key)s AND s.entity_id = e.asin)
           ORDER BY e.asin COLLATE "C" LIMIT %(n)s""",
        {"m": i.t4_mapping_version_id, "stage": stage, "key": i.stage_key(stage), "after": after, "n": n}).fetchall()]


def _mentions(conn: psycopg.Connection, ctx: RunContext, asins: list[str]) -> dict[str, list[tuple]]:
    i = ctx.inputs
    out: dict[str, list[tuple]] = defaultdict(list)
    for key, asin, role, raw in conn.execute(
            "SELECT mention_key, asin, role_class, raw_value FROM task5.person_mention WHERE extraction_id = %s "
            "AND parser_version = %s AND norm_version = %s AND status = 'resolvable' AND asin = ANY(%s) "
            "ORDER BY mention_key COLLATE \"C\"", (i.extraction_id, i.parser_version, i.norm_version, asins)).fetchall():
        out[asin].append((key, role, names.parse(raw)))
    return out


def _works(conn: psycopg.Connection, ctx: RunContext, asins: list[str]) -> dict[str, list[tuple]]:
    """ASIN → [(成员序号, movie_id, 作品状态, tconst)]：电影类取 edition，合集取 contains。"""
    out: dict[str, list[tuple]] = defaultdict(list)
    for asin, ordinal, movie_id, status, tconst in conn.execute(
            "SELECT pm.asin, pm.member_ordinal, pm.movie_id, m.status, m.tconst FROM task4.product_movie pm "
            "JOIN task4.movie m ON m.mapping_version_id = pm.mapping_version_id AND m.movie_id = pm.movie_id "
            "WHERE pm.mapping_version_id = %s AND pm.asin = ANY(%s) ORDER BY pm.asin, pm.member_ordinal",
            (ctx.inputs.t4_mapping_version_id, asins)).fetchall():
        out[asin].append((ordinal, movie_id, status, tconst))
    return out


def _save(conn: psycopg.Connection, ctx: RunContext, route: str, cand_rows: list[tuple],
          verdicts: dict[tuple[str, str, str], Any], mention_keys: list[str]) -> dict[tuple[str, str, str], int]:
    """写候选（COPY）与决策；verdicts 的键为 (mention_key, tconst, nconst)。返回键 → 决策 ID。"""
    i = ctx.inputs
    key = i.stage_key(CREDITS)
    copy_rows(conn, "task5.link_candidate", CAND_COLUMNS, cand_rows)
    ids = {(r[1], r[2], r[3]): r[0] for r in conn.execute(
        "SELECT link_candidate_id, mention_key, tconst, nconst FROM task5.link_candidate WHERE extraction_id = %s "
        "AND stage_key = %s AND route = %s AND mention_key = ANY(%s)",
        (i.extraction_id, key, route, mention_keys)).fetchall()}
    thresholds = Jsonb(_rules(ctx).thresholds())
    decisions: dict[tuple[str, str, str], int] = {}
    for k, v in verdicts.items():
        row = conn.execute(
            "INSERT INTO task5.link_decision (link_candidate_id, rule_version, run_id, tier, reason, confidence, "
            "thresholds) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING link_decision_id",
            (ids[k], i.rule_version, ctx.run_id, v.tier, v.reason, v.confidence, thresholds)).fetchone()
        decisions[k] = row[0]
    return decisions


def _rules(ctx: RunContext) -> RuleSet:
    from .rules import ruleset
    return ruleset(ctx.inputs.rule_version)


# ---------------------------------------------------------------- 途径一

def credit_candidates(mention: tuple, members: list[tuple], credits: dict[str, list[tuple]]) -> list[tuple[CreditCand, tuple]]:
    """一条提及对其全部可比作品的演职员 → [(候选, 演职员行)]。演职员行 = (tconst, nconst, 类别, 表, 序号, 行号, IMDb 姓名)。"""
    _, role, name = mention
    out = []
    for _ordinal, _movie, _status, tconst in members:
        for row in credits.get(tconst, []):
            level = names.level(name, row[7])
            if level is None:
                continue
            out.append((CreditCand(tconst, row[1], names.X_LEVEL[level], row[2] in ROLE_CATEGORIES[role]), row))
    return out


def _credits(conn: psycopg.Connection, ctx: RunContext, tconsts: list[str]) -> dict[str, list[tuple]]:
    """tconst → [(tconst, nconst, 类别, 表, 序号, 行号, 人名行号, Name, IMDb 姓名)]；人名 Name 放在第 8 个位置。"""
    sid = ctx.inputs.imdb_snapshot_id
    rows = conn.execute(
        "SELECT d.tconst, d.nconst, 'director', 'title_director', d.ordinal, d.line_no, n.line_no, n.primary_name "
        "FROM imdb.title_director d LEFT JOIN imdb.name n ON n.snapshot_id = d.snapshot_id AND n.nconst = d.nconst "
        "WHERE d.snapshot_id = %(s)s AND d.tconst = ANY(%(t)s) "
        "UNION ALL "
        "SELECT p.tconst, p.nconst, p.category, 'title_principal', p.ordering, p.line_no, n.line_no, n.primary_name "
        "FROM imdb.title_principal p LEFT JOIN imdb.name n ON n.snapshot_id = p.snapshot_id AND n.nconst = p.nconst "
        "WHERE p.snapshot_id = %(s)s AND p.tconst = ANY(%(t)s) AND p.category IN ('actor', 'actress') "
        "ORDER BY 1, 5", {"s": sid, "t": tconsts}).fetchall()
    out: dict[str, list[tuple]] = defaultdict(list)
    for t, nc, cat, table, ordinal, line, name_line, pname in rows:
        out[t].append((t, nc, cat, table, ordinal, line, name_line, names.parse(pname or ""), pname))
    return out


def _link_credits_batch(conn: psycopg.Connection, ctx: RunContext, batch_no: int, asins: list[str],
                        before_commit: BeforeCommit | None, stats: LinkStats) -> None:
    i, rules = ctx.inputs, _rules(ctx)
    key = i.stage_key(CREDITS)
    mentions, works = _mentions(conn, ctx, asins), _works(conn, ctx, asins)
    linked = {a: [w for w in works.get(a, []) if w[2] == "imdb_linked"] for a in asins}
    credits = _credits(conn, ctx, sorted({w[3] for ws in linked.values() for w in ws}))
    cand_rows, verdicts, mention_keys = [], {}, []
    for asin in asins:
        for m in mentions.get(asin, []):
            pairs = credit_candidates(m, linked[asin], credits)
            if not pairs:
                continue
            by_key = {(c.tconst, c.nconst): row for c, row in pairs}
            result = decide_credit_links([c for c, _ in pairs], rules)
            movie_of = {w[3]: w[1] for w in linked[asin]}
            for (tconst, nconst), verdict in result.items():
                row = by_key[(tconst, nconst)]
                cand = next(c for c, _ in pairs if (c.tconst, c.nconst) == (tconst, nconst))
                evidence = {"table": row[3], "ordinal": row[4], "credit_line_no": row[5], "name_line_no": row[6],
                            "imdb_name": row[8], "snapshot": i.imdb_snapshot_id}
                cand_rows.append((i.extraction_id, key, m[0], CREDITS, tconst, nconst, row[2], cand.x_level,
                                  not cand.same_role, movie_of[tconst], None, None, None, Jsonb(evidence)))
                verdicts[(m[0], tconst, nconst)] = verdict
            mention_keys.append(m[0])
    with conn.transaction():
        _save(conn, ctx, CREDITS, cand_rows, verdicts, mention_keys)
        mark_entities(conn, ctx, CREDITS, batch_no, [(a, 0) for a in asins])
        mark_batch(conn, ctx, CREDITS, batch_no, asins[0], asins[-1], len(cand_rows))
        if before_commit:
            before_commit(CREDITS, batch_no)
    stats.entities += len(asins)
    stats.candidates += len(cand_rows)
    stats.batches += 1
    for v in verdicts.values():
        stats.tiers[v.tier] = stats.tiers.get(v.tier, 0) + 1


def _run_stage(conn, ctx, stage, fn, batch_size, limit, before_commit) -> LinkStats:
    from .mentions import count_pending as mentions_pending
    if not conn.autocommit:
        raise ValueError("装载连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if mentions_pending(conn, ctx):
        raise Task5InputError("人物提及阶段还有商品未处理，不进入外部 ID 阶段")
    stats, after = LinkStats(), ""
    first_batch = next_batch_no(conn, ctx, stage)
    while limit is None or stats.entities < limit:
        n = batch_size if limit is None else min(batch_size, limit - stats.entities)
        asins = _pending(conn, ctx, stage, after, n)
        if not asins:
            break
        fn(conn, ctx, first_batch + stats.batches, asins, before_commit, stats)
        after = asins[-1]
    stats.remaining = _count_pending(conn, ctx, stage)
    return stats


def link_credits(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int, limit: int | None = None,
                 before_commit: BeforeCommit | None = None) -> LinkStats:
    """途径一：商品按 ASIN 分批；每批的候选、决策、实体标记与批次审计在同一事务提交。"""
    return _run_stage(conn, ctx, CREDITS, _link_credits_batch, batch_size, limit, before_commit)


# ---------------------------------------------------------------- 途径二

def verified_set(conn: psycopg.Connection, ctx: RunContext) -> dict[tuple[str, str], list[str]]:
    """已核实人物：途径一 auto 判定得到的 (角色, 严格键) → nconst 列表。只用途径一（R15）。"""
    i = ctx.inputs
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for role, strict, nconst in conn.execute(
            "SELECT m.role_class, m.strict_key, c.nconst FROM task5.link_candidate c "
            "JOIN task5.link_decision d ON d.link_candidate_id = c.link_candidate_id AND d.rule_version = %s "
            "JOIN task5.person_mention m ON m.extraction_id = c.extraction_id AND m.mention_key = c.mention_key "
            "AND m.parser_version = %s AND m.norm_version = %s "
            "WHERE c.extraction_id = %s AND c.stage_key = %s AND c.route = 'credits' AND d.tier = 'auto'",
            (i.rule_version, i.parser_version, i.norm_version, i.extraction_id, i.stage_key(CREDITS))).fetchall():
        out[(role, strict)].add(nconst)
    return {k: sorted(v) for k, v in out.items()}


def verified_hash(verified: dict[tuple[str, str], list[str]]) -> str:
    rows = sorted([k[0], k[1], v] for k, v in verified.items())
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]


def _title_entities(conn: psycopg.Connection, ctx: RunContext, asins: list[str]) -> dict[str, list[tuple]]:
    """ASIN → [(成员序号, 标题键, 标题年份)]：电影类取商品的 title 断言，合集取成员的 title 断言（entity_id 为 ASIN#序号）。"""
    i = ctx.inputs
    mv = conn.execute("SELECT parser_version, norm_version FROM task4.mapping_version WHERE mapping_version_id = %s",
                      (i.t4_mapping_version_id,)).fetchone()
    rows = conn.execute(
        "SELECT entity_id, entity_type, field_name, normalized_value FROM task4.field_assertion "
        "WHERE extraction_id = %s AND parser_version = %s AND norm_version = %s AND field_name IN ('title', 'title_year') "
        "AND ordinal = 1 AND split_part(entity_id, '#', 1) = ANY(%s)", (i.extraction_id, mv[0], mv[1], asins)).fetchall()
    titles: dict[str, str] = {}
    years: dict[str, int] = {}
    for entity, etype, fname, value in rows:
        if fname == "title" and value:
            titles[entity] = value
        elif fname == "title_year" and value:
            years[entity] = int(value)
    kinds = dict(conn.execute("SELECT asin, product_kind FROM task4.product_edition e JOIN task4.mapping_version m "
                              "ON m.extraction_id = e.extraction_id AND m.t3_rule_version = e.t3_rule_version "
                              "WHERE m.mapping_version_id = %s AND e.asin = ANY(%s)",
                              (i.t4_mapping_version_id, asins)).fetchall())
    out: dict[str, list[tuple]] = defaultdict(list)
    for entity, title in sorted(titles.items()):
        asin, _, member = entity.partition("#")
        if kinds.get(asin) == "collection" and member:
            out[asin].append((int(member), title, years.get(entity)))
        elif kinds.get(asin) == "movie" and not member:
            out[asin].append((0, title, years.get(entity)))
    return out


def _filmography(conn: psycopg.Connection, ctx: RunContext, wanted: dict[str, set[str]], keys: set[str]) -> dict:
    """{角色: {nconst: [(tconst, 标题键, 键来源, 键行号, 作品年份)]}}，只取标题键在 keys 里的作品。"""
    sid, out = ctx.inputs.imdb_snapshot_id, {}
    for role, ncs in wanted.items():
        if not ncs:
            continue
        cats = ROLE_CATEGORIES[role]
        rows = conn.execute(
            ("SELECT x.nconst, k.tconst, k.title_key, k.key_source, k.source_line, t.start_year FROM ("
             + ("SELECT nconst, tconst FROM imdb.title_director WHERE snapshot_id = %(s)s AND nconst = ANY(%(n)s)"
                if role == "director" else
                "SELECT nconst, tconst FROM imdb.title_principal WHERE snapshot_id = %(s)s AND nconst = ANY(%(n)s) "
                "AND category = ANY(%(c)s)")
             + ") x JOIN task4.imdb_title_key k ON k.imdb_snapshot_id = %(s)s AND k.tconst = x.tconst "
               "AND k.norm_version = %(nv)s AND k.title_class = 'movie' AND k.title_key = ANY(%(k)s) "
               "JOIN imdb.title t ON t.snapshot_id = %(s)s AND t.tconst = x.tconst"),
            {"s": sid, "n": sorted(ncs), "c": list(cats), "nv": T4_NORM_VERSION, "k": sorted(keys)}).fetchall()
        films: dict[str, list[tuple]] = defaultdict(list)
        for nc, tconst, tkey, source, line, year in rows:
            films[nc].append((tconst, tkey, source, line, year))
        out[role] = films
    return out


def _link_filmography_batch(conn: psycopg.Connection, ctx: RunContext, batch_no: int, asins: list[str],
                            before_commit: BeforeCommit | None, stats: LinkStats) -> None:
    i, rules = ctx.inputs, _rules(ctx)
    key = i.stage_key(CREDITS)
    verified = verified_set(conn, ctx)
    mentions, works, titles = _mentions(conn, ctx, asins), _works(conn, ctx, asins), _title_entities(conn, ctx, asins)
    auto_mentions = {r[0] for r in conn.execute(
        "SELECT c.mention_key FROM task5.link_candidate c JOIN task5.link_decision d USING (link_candidate_id) "
        "WHERE c.extraction_id = %s AND c.stage_key = %s AND c.route = 'credits' AND d.tier = 'auto' "
        "AND d.rule_version = %s AND c.mention_key = ANY(%s)",
        (i.extraction_id, key, i.rule_version, [m[0] for a in asins for m in mentions.get(a, [])])).fetchall()}
    eligible: dict[str, list[tuple]] = {}
    wanted: dict[str, set[str]] = {"director": set(), "actor": set()}
    keys: set[str] = set()
    for asin in asins:
        ws = works.get(asin, [])
        page_only_movie = bool(ws) and ws[0][3] is None and ws[0][0] == 0
        is_collection = any(t[0] > 0 for t in titles.get(asin, []))     # 合集成员的标题断言（成员序号 ≥ 1）
        for m in mentions.get(asin, []):
            ncs = verified.get((m[1], m[2].strict), [])
            if m[0] in auto_mentions or not ncs or not (page_only_movie or is_collection):
                continue
            eligible.setdefault(asin, []).append(m)
            wanted[m[1]].update(ncs)
            keys.update(t[1] for t in titles.get(asin, []))
    films = _filmography(conn, ctx, wanted, keys) if keys else {}
    people = {r[0]: r[1:] for r in conn.execute(
        "SELECT nconst, primary_name, birth_year, death_year, line_no FROM imdb.name WHERE snapshot_id = %s "
        "AND nconst = ANY(%s)", (i.imdb_snapshot_id, sorted(wanted["director"] | wanted["actor"]))).fetchall()}
    anchor = {r[0]: r[1] for r in conn.execute(
        "SELECT c.nconst, min(d.link_decision_id) FROM task5.link_candidate c JOIN task5.link_decision d "
        "USING (link_candidate_id) WHERE c.extraction_id = %s AND c.stage_key = %s AND c.route = 'credits' "
        "AND d.tier = 'auto' AND d.rule_version = %s AND c.nconst = ANY(%s) GROUP BY 1",
        (i.extraction_id, key, i.rule_version, sorted(wanted["director"] | wanted["actor"]))).fetchall()}
    cand_rows, verdicts, mention_keys, pending_hints = [], {}, [], []
    for asin, ms in eligible.items():
        for m in ms:
            hits, info = [], {}
            for nconst in verified[(m[1], m[2].strict)]:
                pname, birth, death, _line = people.get(nconst, (None, None, None, None))
                conflict = False
                if pname:
                    theirs, mine = names.parse(pname).disambiguator, m[2].disambiguator
                    conflict = bool(theirs and mine and theirs != mine)
                for member, tkey, year in titles.get(asin, []):
                    for tconst, ftkey, source, line, fyear in films.get(m[1], {}).get(nconst, []):
                        if ftkey != tkey:
                            continue
                        prev = info.get((nconst, tconst, member))
                        order = {"primary": 0, "original": 1, "aka": 2}
                        if prev is None or order[source] < order[prev[0]]:
                            info[(nconst, tconst, member)] = (source, line, fyear, tkey, year)
            for (nconst, tconst, member), (source, line, fyear, tkey, year) in sorted(info.items()):
                pname, birth, death, _l = people.get(nconst, (None, None, None, None))
                theirs = names.parse(pname).disambiguator if pname else None
                hits.append(FilmHit(nconst, tconst, member, source, fyear, birth, death,
                                    bool(theirs and m[2].disambiguator and theirs != m[2].disambiguator)))
            if not hits:
                continue
            title_year = next((y for mem, _t, y in titles.get(asin, []) if y is not None), None)
            for (nconst, tconst, member), verdict in decide_filmography(hits, title_year, rules).items():
                source, line, fyear, tkey, year = info[(nconst, tconst, member)]
                movie = next((w[1] for w in works.get(asin, []) if w[0] == member), None)
                evidence = {"member_ordinal": member, "imdb_start_year": fyear, "title_year": year,
                            "title_key_line_no": line, "snapshot": i.imdb_snapshot_id,
                            "name_line_no": people.get(nconst, (None,) * 4)[3]}
                cand_rows.append((i.extraction_id, key, m[0], FILMOGRAPHY, tconst, nconst, m[1], "X1", False, movie,
                                  anchor.get(nconst), tkey, source, Jsonb(evidence)))
                verdicts[(m[0], tconst, nconst)] = verdict
                if verdict.tier == "auto":
                    pending_hints.append((asin, member, tconst, m[0], (m[0], tconst, nconst)))
            mention_keys.append(m[0])
    with conn.transaction():
        decisions = _save(conn, ctx, FILMOGRAPHY, cand_rows, verdicts, mention_keys)
        grouped: dict[tuple, list] = defaultdict(list)
        for asin, member, tconst, mkey, dkey in pending_hints:
            grouped[(asin, member, tconst)].append((mkey, decisions[dkey]))
        for (asin, member, tconst), items in sorted(grouped.items()):
            if len(items) >= 2:
                conn.execute("INSERT INTO task5.t4_upgrade_hint (extraction_id, stage_key, asin, member_ordinal, "
                             "tconst, mention_keys, link_decision_ids, mention_count) VALUES (%s, %s, %s, %s, %s, %s, "
                             "%s, %s)", (i.extraction_id, key, asin, member, tconst, [x[0] for x in items],
                                         [x[1] for x in items], len(items)))
                stats.hints += 1
        mark_entities(conn, ctx, FILMOGRAPHY, batch_no, [(a, 0) for a in asins])
        mark_batch(conn, ctx, FILMOGRAPHY, batch_no, asins[0], asins[-1], len(cand_rows))
        if before_commit:
            before_commit(FILMOGRAPHY, batch_no)
    stats.entities += len(asins)
    stats.candidates += len(cand_rows)
    stats.batches += 1
    for v in verdicts.values():
        stats.tiers[v.tier] = stats.tiers.get(v.tier, 0) + 1


def link_filmography(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int, limit: int | None = None,
                     before_commit: BeforeCommit | None = None) -> LinkStats:
    """途径二：须在途径一全部完成之后；已核实集合的哈希记进运行的 params（verified_hash）。"""
    if _count_pending(conn, ctx, CREDITS):
        raise Task5InputError("外部 ID 途径一还有商品未处理，不进入作品年表反查")
    digest = verified_hash(verified_set(conn, ctx))
    conn.execute("UPDATE task5.run SET params = params || jsonb_build_object('verified_hash', %s::text) "
                 "WHERE run_id = %s", (digest, ctx.run_id))
    return _run_stage(conn, ctx, FILMOGRAPHY, _link_filmography_batch, batch_size, limit, before_commit)
