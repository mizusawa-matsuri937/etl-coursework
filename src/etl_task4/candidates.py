"""候选生成、特征取数与判定落库（spec R09、R15、R20–R25、R27，AC05）。

阶段（都按实体分批，每批的候选、决策、实体标记与批次审计同一事务提交，重跑只处理没有标记的实体）：
- imdb_keys：IMDb 作品、别名的标题键，写 task4.imdb_title_key（换标准化版本只重算这里，不重装 IMDb）；
- decide：商品（movie 类）与合集成员对 IMDb 作品（G1、G2；只在非电影类型里命中的为 G4，R15）；
- decide_pair：标题键相同的电影类商品两两成对（G3），依赖上一步的 IMDb 关联。
纯规则（特征、得分、三档）在 match.py，本文件只负责取数和写库。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import normalize as nz
from .match import (DEFAULT_RULES, Decision, ImdbFacts, PageFacts, PairScore, PersonKey, RuleSet,
                    decide_imdb_candidates, decide_pair, score_imdb, score_pair)
from .run import BeforeCommit, RunContext, copy_rows, mark_batch, mark_entities, next_batch_no

BASICS_FILE = "title.basics.tsv.gz"
OTHER_CAP = 5                    # G4：每个商品最多记录的非电影作品数
TYPE_PRIORITY = {"movie": 0, "tvMovie": 1, "video": 2}


# ---------------------------------------------------------------- IMDb 标题键（R09）

@dataclass
class KeyStats:
    titles: int = 0
    keys: int = 0
    batches: int = 0


def build_imdb_keys(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = 5000,
                    before_commit: BeforeCommit | None = None) -> KeyStats:
    """primaryTitle、originalTitle 与每个别名各生成标题键，记录地区、语言与来源行。已有主标题键的作品不重算。"""
    sid, nv = ctx.inputs.imdb_snapshot_id, ctx.inputs.norm_version
    stats, batch_no = KeyStats(), next_batch_no(conn, ctx, "imdb_keys")
    for table, klass in (("imdb.title", "movie"), ("imdb.title_other", "other")):
        after = ""
        while True:
            titles = conn.execute(
                f"""SELECT t.tconst, t.primary_title, t.original_title, t.line_no FROM {table} t
                    WHERE t.snapshot_id = %(sid)s AND t.tconst COLLATE "C" > %(after)s
                      AND NOT EXISTS (SELECT 1 FROM task4.imdb_title_key k
                                      WHERE k.imdb_snapshot_id = t.snapshot_id AND k.norm_version = %(nv)s
                                        AND k.tconst = t.tconst AND k.key_source = 'primary')
                    ORDER BY t.tconst COLLATE "C" LIMIT %(n)s""",
                {"sid": sid, "after": after, "nv": nv, "n": batch_size}).fetchall()
            if not titles:
                break
            rows: list[tuple] = []
            for tconst, primary, original, line_no in titles:
                rows.append((sid, nv, tconst, "primary", 0, nz.title_key(primary) if primary else "", klass,
                             None, None, line_no))
                if original is not None:
                    rows.append((sid, nv, tconst, "original", 0, nz.title_key(original), klass, None, None, line_no))
            if klass == "movie":
                for tconst, ordering, title, region, language, line_no in conn.execute(
                        "SELECT tconst, ordering, title, region, language, line_no FROM imdb.title_aka "
                        "WHERE snapshot_id = %s AND tconst = ANY(%s) AND title IS NOT NULL",
                        (sid, [t[0] for t in titles])).fetchall():
                    rows.append((sid, nv, tconst, "aka", ordering, nz.title_key(title), klass, region, language,
                                 line_no))
            with conn.transaction():
                copy_rows(conn, "task4.imdb_title_key",
                          ("imdb_snapshot_id", "norm_version", "tconst", "key_source", "aka_ordering", "title_key",
                           "title_class", "region", "language", "source_line"), rows)
                mark_batch(conn, ctx, "imdb_keys", batch_no, titles[0][0], titles[-1][0], len(rows))
                if before_commit:
                    before_commit("imdb_keys", batch_no)
            batch_no += 1
            after = titles[-1][0]
            stats.titles += len(titles)
            stats.keys += len(rows)
            stats.batches += 1
    return stats


# ---------------------------------------------------------------- 事实取数

def load_page_facts(conn: psycopg.Connection, ctx: RunContext, entity_ids: list[str]) -> dict[str, PageFacts]:
    """从字段断言取出比较用的页面事实；合集成员的人物与类型取自合集商品页面（页面不区分成员）。"""
    i = ctx.inputs
    bases = sorted({e.split("#")[0] for e in entity_ids})
    rows = conn.execute(
        "SELECT entity_id, field_name, normalized_value, qualifier, extra, assertion_id FROM task4.field_assertion "
        "WHERE extraction_id = %s AND parser_version = %s AND norm_version = %s "
        "AND (entity_id = ANY(%s) OR entity_id = ANY(%s)) ORDER BY entity_id, field_name, ordinal",
        (i.extraction_id, i.parser_version, i.norm_version, entity_ids, bases)).fetchall()
    by: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    out: dict[str, PageFacts] = {}
    for eid in entity_ids:
        own, member = by.get(eid, []), "#" in eid
        people_rows = by.get(eid.split("#")[0], []) if member else own

        def pick(rs, field_name):
            return [r for r in rs if r[1] == field_name]

        def people(field_name):
            ps = [r for r in pick(people_rows, field_name) if r[2] and r[3] in nz.STRONG_QUALIFIERS]
            return tuple(PersonKey(r[2], (r[4] or {}).get("loose", r[2])) for r in ps), tuple(r[5] for r in ps)

        title = pick(own, "title")
        year = pick(own, "title_year")
        directors, d_ids = people("director")
        actors, a_ids = people("actor")
        runtimes = [int(r[2]) for r in pick(own, "runtime_min") if r[2]] if not member else []
        genres = frozenset(r[2] for r in pick(people_rows, "genre") if r[2])
        out[eid] = PageFacts(
            eid, title[0][2] if title else "", int(year[0][2]) if year else None, directors, actors,
            min(runtimes) if runtimes else None, bool(pick(own, "edition.cut_type")), genres, member,
            {"title": tuple(r[5] for r in title), "title_year": tuple(r[5] for r in year), "director": d_ids,
             "actor": a_ids, "runtime": tuple(r[5] for r in pick(own, "runtime_min"))})
    return out


def load_imdb_facts(conn: psycopg.Connection, sid: str, tconsts: list[str]) -> dict[str, ImdbFacts]:
    """作品、导演与主要演员（姓名键用与页面相同的函数）；refs 记录文件与行号，供特征证据引用。"""
    if not tconsts:
        return {}
    titles = {r[0]: r for r in conn.execute(
        "SELECT tconst, title_type, start_year, runtime_minutes, genres, line_no FROM imdb.title "
        "WHERE snapshot_id = %s AND tconst = ANY(%s)", (sid, tconsts)).fetchall()}
    names: dict[str, tuple[str, int]] = {}
    people: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)   # (tconst, 角色) → nconst → 证据
    for tconst, nconst, name, file, line_no, role in conn.execute(
            """SELECT d.tconst, d.nconst, n.primary_name, 'title.crew.tsv.gz', d.line_no, 'director'
               FROM imdb.title_director d LEFT JOIN imdb.name n ON n.snapshot_id = d.snapshot_id AND n.nconst = d.nconst
               WHERE d.snapshot_id = %(s)s AND d.tconst = ANY(%(t)s)
               UNION ALL
               SELECT p.tconst, p.nconst, n.primary_name, 'title.principals.tsv.gz', p.line_no,
                      CASE WHEN p.category = 'director' THEN 'director' ELSE 'actor' END
               FROM imdb.title_principal p LEFT JOIN imdb.name n ON n.snapshot_id = p.snapshot_id AND n.nconst = p.nconst
               WHERE p.snapshot_id = %(s)s AND p.tconst = ANY(%(t)s)
               ORDER BY 1, 6, 2, 4""", {"s": sid, "t": tconsts}).fetchall():
        if name:
            people[(tconst, role)].setdefault(nconst, {"nconst": nconst, "name": name, "file": file,
                                                       "line_no": line_no})
    out: dict[str, ImdbFacts] = {}
    for tconst, (_, ttype, start, runtime, genres, line_no) in ((t, titles[t]) for t in tconsts if t in titles):
        def keys(role):
            ev = list(people.get((tconst, role), {}).values())
            return tuple(PersonKey(*nz.name_keys(e["name"])) for e in ev), ev

        d_keys, d_ev = keys("director")
        a_keys, a_ev = keys("actor")
        out[tconst] = ImdbFacts(tconst, ttype, start, runtime, frozenset(nz.fold(g) for g in genres or ()),
                                d_keys, a_keys, {"title": {"file": BASICS_FILE, "line_no": line_no},
                                                 "directors": d_ev, "actors": a_ev})
    return out


def _imdb_source_records(conn: psycopg.Connection, sid: str, facts: dict[str, ImdbFacts]) -> dict[str, int]:
    """进入候选的作品登记 IMDb 源记录（快照、文件、行号、tconst）；不为几百万行逐一登记。"""
    if not facts:
        return {}
    file_hash = conn.execute("SELECT sha256 FROM imdb.source_file WHERE snapshot_id = %s AND file_name = %s",
                             (sid, BASICS_FILE)).fetchone()[0]
    keys = [f"imdb:{sid}:{BASICS_FILE}:{t}" for t in facts]
    locators = [json.dumps({"snapshot": sid, "file": BASICS_FILE, "line_no": f.refs["title"]["line_no"],
                            "tconst": t}, sort_keys=True) for t, f in facts.items()]
    rows = conn.execute(
        """INSERT INTO task4.source_record (source_type, locator_key, locator, content_hash)
           SELECT 'imdb', k, l::jsonb, %s FROM unnest(%s::text[], %s::text[]) AS t(k, l)
           ON CONFLICT (source_type, locator_key) DO UPDATE SET locator_key = EXCLUDED.locator_key
           RETURNING locator_key, source_record_id""", (file_hash, keys, locators)).fetchall()
    by_key = dict(rows)
    return {t: by_key[k] for t, k in zip(facts, keys)}


# ---------------------------------------------------------------- 商品、成员对 IMDb（G1/G2/G4）

@dataclass
class DecideStats:
    entities: int = 0
    candidates: int = 0
    decisions: int = 0
    batches: int = 0
    remaining: int = 0
    overflow: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    generators: dict[str, int] = field(default_factory=dict)


def _check_rules(ctx: RunContext, rules: RuleSet) -> None:
    if rules.version != ctx.inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 运行输入里的规则版本 {ctx.inputs.rule_version}")


def _pending_entities(conn: psycopg.Connection, ctx: RunContext, after: str, n: int) -> list[tuple[str, str]]:
    i = ctx.inputs
    return conn.execute(
        """SELECT a.entity_id, a.normalized_value FROM task4.field_assertion a
           WHERE a.extraction_id = %(eid)s AND a.parser_version = %(pv)s AND a.norm_version = %(nv)s
             AND a.field_name = 'title' AND a.normalized_value <> '' AND a.entity_id COLLATE "C" > %(after)s
             AND (a.entity_type = 'collection_member'
                  OR EXISTS (SELECT 1 FROM task4.product_edition e
                             WHERE e.extraction_id = a.extraction_id AND e.asin = a.entity_id
                               AND e.t3_rule_version = %(t3)s AND e.product_kind = 'movie'
                               AND e.match_scope = 'matched'))
             AND NOT EXISTS (SELECT 1 FROM task4.entity_stage s
                             WHERE s.extraction_id = a.extraction_id AND s.stage = 'decide'
                               AND s.stage_key = %(key)s AND s.entity_id = a.entity_id)
           ORDER BY a.entity_id COLLATE "C" LIMIT %(n)s""",
        {"eid": i.extraction_id, "pv": i.parser_version, "nv": i.norm_version, "t3": i.t3_rule_version,
         "after": after, "key": i.stage_key("decide"), "n": n}).fetchall()


def count_pending_entities(conn: psycopg.Connection, ctx: RunContext) -> int:
    n, after = 0, ""
    while True:
        rows = _pending_entities(conn, ctx, after, 5000)
        if not rows:
            return n
        n += len(rows)
        after = rows[-1][0]


def _rank_hits(facts: PageFacts, hits: dict[str, tuple[str, ImdbFacts]]) -> list[str]:
    """超过上限时的截断依据：先看标题年份与 startYear 的接近程度，再看作品类型（movie 优先），再看 tconst。"""
    def key(t: str):
        f = hits[t][1]
        gap = abs(facts.title_year - f.start_year) if facts.title_year and f.start_year else 10_000
        return (gap, TYPE_PRIORITY.get(f.title_type, 9), t)

    return sorted(hits, key=key)


def _decide_batch(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, entities: list[tuple[str, str]],
                  batch_no: int, before_commit: BeforeCommit | None, stats: DecideStats) -> None:
    i = ctx.inputs
    sid, nv = i.imdb_snapshot_id, i.norm_version
    page = load_page_facts(conn, ctx, [e for e, _ in entities])
    hits: dict[str, list[tuple]] = defaultdict(list)
    for key, tconst, source, klass, aka_ordering, region, language, line in conn.execute(
            """SELECT title_key, tconst, key_source, title_class, aka_ordering, region, language, source_line
               FROM task4.imdb_title_key WHERE imdb_snapshot_id = %s AND norm_version = %s
               AND title_key = ANY(%s) AND title_key <> ''""", (sid, nv, sorted({k for _, k in entities}))).fetchall():
        hits[key].append((tconst, source, klass, aka_ordering, region, language, line))
    movie_t = sorted({h[0] for _, k in entities for h in hits.get(k, []) if h[2] == "movie"})
    imdb = load_imdb_facts(conn, sid, movie_t)
    other_t = sorted({h[0] for _, k in entities for h in hits.get(k, []) if h[2] == "other"})
    others = {r[0]: r for r in conn.execute(
        "SELECT tconst, title_type, primary_title, start_year, line_no FROM imdb.title_other "
        "WHERE snapshot_id = %s AND tconst = ANY(%s)", (sid, other_t)).fetchall()} if other_t else {}

    cand_rows: list[tuple] = []          # (left, tconst, generator)
    plan: list[tuple[str, str, Decision, dict]] = []      # (left, tconst, 决策, 证据)
    overflow_rows: list[tuple] = []
    quality: list[tuple] = []
    for eid, key in entities:
        f = page[eid]
        movie_hits: dict[str, tuple[str, ImdbFacts]] = {}
        evidence_keys: dict[str, list[dict]] = defaultdict(list)
        for tconst, source, klass, aka_ordering, region, language, line in hits.get(key, []):
            if klass == "movie" and tconst in imdb:
                gen = "G1" if source in ("primary", "original") else "G2"
                if tconst not in movie_hits or gen == "G1":
                    movie_hits[tconst] = (gen, imdb[tconst])
                evidence_keys[tconst].append({"key_source": source, "aka_ordering": aka_ordering, "region": region,
                                              "language": language, "source_line": line})
        capped = len(movie_hits) > rules.imdb_candidate_cap
        if capped:
            order = _rank_hits(f, movie_hits)
            overflow_rows.append((i.extraction_id, nv, sid, eid, "G1/G2", len(order), rules.imdb_candidate_cap,
                                  "title_year_gap, title_type, tconst"))
            quality.append((ctx.run_id, i.extraction_id, eid, "candidate_overflow",
                            Jsonb({"total": len(order), "kept": rules.imdb_candidate_cap})))
            movie_hits = {t: movie_hits[t] for t in order[:rules.imdb_candidate_cap]}
        if movie_hits:
            scored = [(t, score_imdb(f, w, gen, rules)) for t, (gen, w) in sorted(movie_hits.items())]
            decisions = decide_imdb_candidates(scored, rules, capped=capped)
            for t, (gen, w) in sorted(movie_hits.items()):
                cand_rows.append((eid, t, gen))
                plan.append((eid, t, decisions[t], {"imdb": w.refs, "keys": evidence_keys[t], "page": f.refs,
                                                    "imdb_type": w.title_type, "start_year": w.start_year}))
        else:                                    # R15：只在非电影作品类型里命中，不关联，标为人工复核
            seen = sorted({h[0] for h in hits.get(key, []) if h[2] == "other" and h[0] in others})[:OTHER_CAP]
            for t in seen:
                _, ttype, ptitle, start, line_no = others[t]
                cand_rows.append((eid, t, "G4"))
                feats = {"title": {"value": "G4", "score": 0, "key": key},
                         "imdb": {"tconst": t, "title_type": ttype, "primary_title": ptitle, "start_year": start,
                                  "file": BASICS_FILE, "line_no": line_no}}
                plan.append((eid, t, Decision("review", 0.0, 0.0, None, None, (), "non_movie_title_type", feats,
                                              rules.thresholds()), {"page": f.refs}))
    # 候选与决策：先登记 IMDb 源记录，再写候选（跨规则版本共用），再写本规则版本的决策
    with conn.transaction():
        sources = _imdb_source_records(conn, sid, {t: imdb[t] for _, t, _ in cand_rows if t in imdb})
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO task4.candidate (extraction_id, norm_version, imdb_snapshot_id, left_entity, right_kind, "
                "right_id, generator) VALUES (%s, %s, %s, %s, 'imdb', %s, %s) "
                "ON CONFLICT (extraction_id, norm_version, imdb_snapshot_id, left_entity, right_kind, right_id) "
                "DO NOTHING", [(i.extraction_id, nv, sid, e, t, g) for e, t, g in cand_rows])
            ids = {(r[1], r[2]): r[0] for r in conn.execute(
                "SELECT candidate_id, left_entity, right_id FROM task4.candidate WHERE extraction_id = %s AND "
                "norm_version = %s AND imdb_snapshot_id = %s AND right_kind = 'imdb' AND left_entity = ANY(%s)",
                (i.extraction_id, nv, sid, [e for e, _ in entities])).fetchall()}
            drows = []
            for eid, t, d, ev in plan:
                features = {**d.features, "evidence": {**ev, "imdb_source_record_id": sources.get(t)}}
                drows.append((ids[(eid, t)], rules.version, ctx.run_id, d.tier, d.score, d.confidence, d.rank,
                              d.margin, list(d.veto), d.reason, Jsonb(features), Jsonb(d.thresholds)))
            cur.executemany(
                "INSERT INTO task4.merge_decision (candidate_id, rule_version, run_id, tier, score, confidence, rank, "
                "margin, veto, reason, features, thresholds) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                drows)
            if overflow_rows:
                cur.executemany(
                    "INSERT INTO task4.candidate_overflow (extraction_id, norm_version, imdb_snapshot_id, entity_id, "
                    "generator, candidate_total, kept, basis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING", overflow_rows)
            if quality:
                cur.executemany("INSERT INTO task4.quality_issue (run_id, extraction_id, entity_id, issue_code, "
                                "detail) VALUES (%s, %s, %s, %s, %s)", quality)
        per_entity: dict[str, int] = defaultdict(int)
        for eid, _, _, _ in plan:
            per_entity[eid] += 1
        mark_entities(conn, ctx, "decide", batch_no, [(e, per_entity[e]) for e, _ in entities])
        mark_batch(conn, ctx, "decide", batch_no, entities[0][0], entities[-1][0], len(drows))
        if before_commit:
            before_commit("decide", batch_no)
    stats.entities += len(entities)
    stats.candidates += len(cand_rows)
    stats.decisions += len(drows)
    stats.overflow += len(overflow_rows)
    stats.batches += 1
    for _, _, d, _ in plan:
        stats.tiers[d.tier] = stats.tiers.get(d.tier, 0) + 1
    for _, _, g in cand_rows:
        stats.generators[g] = stats.generators.get(g, 0) + 1


def decide_entities(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet = DEFAULT_RULES, *, batch_size: int,
                    limit: int | None = None, before_commit: BeforeCommit | None = None) -> DecideStats:
    """阶段 decide：只处理本输入版本下还没有标记的商品与合集成员。"""
    _check_rules(ctx, rules)
    stats, after = DecideStats(), ""
    first = next_batch_no(conn, ctx, "decide")
    while limit is None or stats.entities < limit:
        n = batch_size if limit is None else min(batch_size, limit - stats.entities)
        entities = _pending_entities(conn, ctx, after, n)
        if not entities:
            break
        _decide_batch(conn, ctx, rules, entities, first + stats.batches, before_commit, stats)
        after = entities[-1][0]
    stats.remaining = count_pending_entities(conn, ctx)
    return stats


# ---------------------------------------------------------------- 商品对商品（G3）

def auto_links(conn: psycopg.Connection, ctx: RunContext, entities: list[str]) -> dict[str, str]:
    """实体 → 它唯一的自动 IMDb 关联（R24）。"""
    i = ctx.inputs
    return dict(conn.execute(
        """SELECT c.left_entity, c.right_id FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND c.right_kind = 'imdb'
             AND d.rule_version = %s AND d.tier = 'auto' AND c.left_entity = ANY(%s)""",
        (i.extraction_id, i.norm_version, i.imdb_snapshot_id, i.rule_version, entities)).fetchall())


def _blocks(conn: psycopg.Connection, ctx: RunContext) -> list[tuple[str, list[str]]]:
    i = ctx.inputs
    done = {r[0] for r in conn.execute(
        "SELECT entity_id FROM task4.entity_stage WHERE extraction_id = %s AND stage = 'decide_pair' "
        "AND stage_key = %s", (i.extraction_id, i.stage_key("decide_pair"))).fetchall()}
    rows = conn.execute(
        """SELECT a.normalized_value, array_agg(a.entity_id ORDER BY a.entity_id COLLATE "C")
           FROM task4.field_assertion a
           JOIN task4.product_edition e ON e.extraction_id = a.extraction_id AND e.asin = a.entity_id
                AND e.t3_rule_version = %s AND e.product_kind = 'movie' AND e.match_scope = 'matched'
           WHERE a.extraction_id = %s AND a.parser_version = %s AND a.norm_version = %s AND a.field_name = 'title'
             AND a.entity_type = 'product_edition' AND a.normalized_value <> ''
           GROUP BY a.normalized_value HAVING count(*) > 1 ORDER BY a.normalized_value COLLATE "C" """,
        (i.t3_rule_version, i.extraction_id, i.parser_version, i.norm_version)).fetchall()
    return [(k, asins) for k, asins in rows if f"block:{k}" not in done]


@dataclass
class PairStats:
    blocks: int = 0
    candidates: int = 0
    batches: int = 0
    remaining: int = 0
    overflow: int = 0
    tiers: dict[str, int] = field(default_factory=dict)


def decide_pairs(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet = DEFAULT_RULES, *, batch_size: int,
                 before_commit: BeforeCommit | None = None) -> PairStats:
    """阶段 decide_pair：标题键相同的电影类商品两两成对（一个标题键为一个单位）。"""
    _check_rules(ctx, rules)
    i = ctx.inputs
    stats = PairStats()
    first = next_batch_no(conn, ctx, "decide_pair")
    while True:
        blocks = _blocks(conn, ctx)[:batch_size]
        if not blocks:
            break
        asins = sorted({a for _, group in blocks for a in group})
        facts = load_page_facts(conn, ctx, asins)
        links = auto_links(conn, ctx, asins)
        plan, overflow_rows, quality = [], [], []
        for key, group in blocks:
            oversized = len(group) > rules.block_cap
            if oversized:
                overflow_rows.append((i.extraction_id, i.norm_version, i.imdb_snapshot_id, f"block:{key}", "G3",
                                      len(group), 0, "block_size_above_cap: review only"))
                quality.append((ctx.run_id, i.extraction_id, f"block:{key}", "candidate_overflow",
                                Jsonb({"products": len(group), "cap": rules.block_cap})))
            for a, b in combinations(group, 2):
                s: PairScore = score_pair(facts[a], facts[b], rules)
                d = decide_pair(s, links.get(a), links.get(b), rules, oversized=oversized)
                plan.append((a, b, d, {"left": facts[a].refs, "right": facts[b].refs}))
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO task4.candidate (extraction_id, norm_version, imdb_snapshot_id, left_entity, "
                    "right_kind, right_id, generator) VALUES (%s, %s, %s, %s, 'product', %s, 'G3') "
                    "ON CONFLICT (extraction_id, norm_version, imdb_snapshot_id, left_entity, right_kind, right_id) "
                    "DO NOTHING", [(i.extraction_id, i.norm_version, i.imdb_snapshot_id, a, b) for a, b, _, _ in plan])
                ids = {(r[1], r[2]): r[0] for r in conn.execute(
                    "SELECT candidate_id, left_entity, right_id FROM task4.candidate WHERE extraction_id = %s AND "
                    "norm_version = %s AND imdb_snapshot_id = %s AND right_kind = 'product' AND left_entity = ANY(%s)",
                    (i.extraction_id, i.norm_version, i.imdb_snapshot_id, asins)).fetchall()}
                cur.executemany(
                    "INSERT INTO task4.merge_decision (candidate_id, rule_version, run_id, tier, score, confidence, "
                    "rank, margin, veto, reason, features, thresholds) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s)",
                    [(ids[(a, b)], rules.version, ctx.run_id, d.tier, d.score, d.confidence, d.rank, d.margin,
                      list(d.veto), d.reason, Jsonb({**d.features, "evidence": ev}), Jsonb(d.thresholds))
                     for a, b, d, ev in plan])
                if overflow_rows:
                    cur.executemany(
                        "INSERT INTO task4.candidate_overflow (extraction_id, norm_version, imdb_snapshot_id, "
                        "entity_id, generator, candidate_total, kept, basis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING", overflow_rows)
                if quality:
                    cur.executemany("INSERT INTO task4.quality_issue (run_id, extraction_id, entity_id, issue_code, "
                                    "detail) VALUES (%s, %s, %s, %s, %s)", quality)
            batch_no = first + stats.batches
            per_block = {k: len(g) * (len(g) - 1) // 2 for k, g in blocks}
            mark_entities(conn, ctx, "decide_pair", batch_no, [(f"block:{k}", per_block[k]) for k, _ in blocks])
            mark_batch(conn, ctx, "decide_pair", batch_no, blocks[0][0], blocks[-1][0], len(plan))
            if before_commit:
                before_commit("decide_pair", batch_no)
        stats.blocks += len(blocks)
        stats.candidates += len(plan)
        stats.overflow += len(overflow_rows)
        stats.batches += 1
        for _, _, d, _ in plan:
            stats.tiers[d.tier] = stats.tiers.get(d.tier, 0) + 1
    stats.remaining = len(_blocks(conn, ctx))
    return stats
