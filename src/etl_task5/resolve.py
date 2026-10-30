"""作品内人物组、跨作品候选与第 1 轮判定（spec R17–R22、R25，AC05、AC06）。

- build_groups（全局单事务）：同一作品单位内严格键相同（不分角色）或同角色词集键相同的提及合成一组；提及已 auto 关联 nconst 时
  整组带这个外部 ID，一组带两个 nconst 时按 nconst 拆组并记质量问题。组记录作品年份、发行商与名字稀有度。
- generate_pairs：C1–C4 四种分块生成跨作品候选，每个候选只出现在第一个产生它的块里；块过大只判复核或不枚举，记质量问题。
- decide_blocks（按分块键分批，阶段 decide_pair）：每批的候选、第 1 轮判定、实体标记与批次审计在同一事务提交。
合作者佐证的后续轮次与聚类在 cluster.py。判定的纯函数在 rules.py。
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task4.normalize import strict_name

from . import names
from .external import CREDITS
from .rules import PairInput, RuleSet, decide_pair, era_conflict, is_rare, ruleset
from .run import BeforeCommit, RunContext, copy_rows, mark_batch, mark_entities, next_batch_no

STAGE = "decide_pair"


@dataclass
class Group:
    key: str
    work_unit: str
    movie_id: str | None
    roles: set[str]
    strict: str
    loose: str
    tokenset: str
    family: str
    initial: str
    nconst: str | None
    route: str | None
    year: int | None
    publishers: set[str]
    rarity: dict[str, int]
    mentions: list[str]
    name: names.Name
    birth: int | None = None
    death: int | None = None

    @property
    def rep(self) -> str:
        return self.key.split("|", 1)[1]


# ---------------------------------------------------------------- 作品内人物组

def _links(conn: psycopg.Connection, ctx: RunContext) -> dict[str, tuple[str, str, str | None, int]]:
    """提及 → (nconst, 途径, 作品, 决策 ID)：两条途径 auto 判定里的外部 ID（一条提及至多一个）。"""
    i = ctx.inputs
    out = {}
    for mkey, nconst, route, movie, did in conn.execute(
            "SELECT c.mention_key, c.nconst, c.route, c.work_movie_id, d.link_decision_id FROM task5.link_candidate c "
            "JOIN task5.link_decision d USING (link_candidate_id) WHERE c.extraction_id = %s AND c.stage_key = %s "
            "AND d.rule_version = %s AND d.tier = 'auto' ORDER BY c.mention_key, c.route", (
                i.extraction_id, i.stage_key(CREDITS), i.rule_version)).fetchall():
        out.setdefault(mkey, (nconst, route, movie, did))
    return out


def _work_years(conn: psycopg.Connection, ctx: RunContext) -> tuple[dict[str, int | None], dict[str, set[str]], dict]:
    """作品 → 年份（IMDb 起始年份，否则作品内商品标题年份的最小值）与发行商集合；以及 ASIN → 作品。"""
    i = ctx.inputs
    mv = conn.execute("SELECT parser_version, norm_version FROM task4.mapping_version WHERE mapping_version_id = %s",
                      (i.t4_mapping_version_id,)).fetchone()
    movies = {r[0]: r for r in conn.execute(
        "SELECT m.movie_id, m.status, t.start_year FROM task4.movie m LEFT JOIN imdb.title t "
        "ON t.snapshot_id = %s AND t.tconst = m.tconst WHERE m.mapping_version_id = %s",
        (i.imdb_snapshot_id, i.t4_mapping_version_id)).fetchall()}
    editions = conn.execute("SELECT asin, movie_id FROM task4.product_movie WHERE mapping_version_id = %s "
                            "AND relation_type = 'edition'", (i.t4_mapping_version_id,)).fetchall()
    asin_movie = dict(editions)
    years: dict[str, list[int]] = defaultdict(list)
    pubs: dict[str, set[str]] = defaultdict(set)
    for entity, fname, raw, value in conn.execute(
            "SELECT entity_id, field_name, raw_value, normalized_value FROM task4.field_assertion "
            "WHERE extraction_id = %s AND parser_version = %s AND norm_version = %s AND entity_type = 'product_edition' "
            "AND field_name IN ('title_year', 'edition.publisher', 'edition.publisher_series') AND ordinal = 1",
            (i.extraction_id, mv[0], mv[1])).fetchall():
        movie = asin_movie.get(entity)
        if movie is None:
            continue
        if fname == "title_year" and value:
            years[movie].append(int(value))
        elif fname != "title_year" and (raw or value):
            pubs[movie].add(strict_name(raw or value))
    result_year = {mid: (m[2] if m[2] is not None else min(years[mid], default=None)) for mid, m in movies.items()}
    return result_year, pubs, asin_movie


def _imdb_people_counts(conn: psycopg.Connection, ctx: RunContext, wanted: set[str]) -> Counter:
    """IMDb 全部人名里同严格键的人数（只数需要的键；一次性只读扫描，R22）。"""
    counts: Counter = Counter()
    if not wanted:
        return counts
    with conn.transaction(), conn.cursor(name="t5_imdb_names") as cur:
        cur.itersize = 50000
        cur.execute("SELECT primary_name FROM imdb.name WHERE snapshot_id = %s", (ctx.inputs.imdb_snapshot_id,))
        for (pname,) in cur:
            key = strict_name(pname or "")
            if key in wanted:
                counts[key] += 1
    return counts


def build_groups(conn: psycopg.Connection, ctx: RunContext) -> dict[str, int]:
    """全局步骤：生成本输入键下的作品内人物组。已有同一阶段键的组时什么都不做（确定性重算结果相同）。"""
    i = ctx.inputs
    key = i.stage_key(CREDITS)
    if conn.execute("SELECT 1 FROM task5.person_group WHERE extraction_id = %s AND stage_key = %s LIMIT 1",
                    (i.extraction_id, key)).fetchone():
        return {"groups": 0, "reused": 1}
    rows = conn.execute(
        "SELECT mention_key, asin, product_kind, role_class, raw_value, strict_key, loose_key, tokenset_key, "
        "family_key, initial_key FROM task5.person_mention WHERE extraction_id = %s AND parser_version = %s "
        "AND norm_version = %s AND status = 'resolvable' ORDER BY mention_key COLLATE \"C\"",
        (i.extraction_id, i.parser_version, i.norm_version)).fetchall()
    links = _links(conn, ctx)
    year_of, pubs_of, asin_movie = _work_years(conn, ctx)
    # 作品单位：电影类取 edition 作品；合集提及经外部 ID 归到成员作品，否则 collection:<ASIN>
    unit_of: dict[str, tuple[str, str | None]] = {}
    for mkey, asin, kind, *_ in rows:
        link = links.get(mkey)
        movie = asin_movie.get(asin) if kind == "movie" else (link[2] if link else None)
        unit_of[mkey] = (f"movie:{movie}", movie) if movie else (f"collection:{asin}", None)
    by_unit: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by_unit[unit_of[r[0]][0]].append(r)
    groups: list[tuple[str, list[tuple]]] = []
    issues = []
    for unit, members in sorted(by_unit.items()):
        parent = {r[0]: r[0] for r in members}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        first: dict[Any, str] = {}
        for r in members:
            for k in (("s", r[5]), ("t", r[3], r[7])):
                if k in first:
                    parent[find(r[0])] = find(first[k])
                else:
                    first[k] = r[0]
        comps: dict[str, list[tuple]] = defaultdict(list)
        for r in members:
            comps[find(r[0])].append(r)
        for comp in comps.values():
            ncs = sorted({links[r[0]][0] for r in comp if r[0] in links})
            if len(ncs) <= 1:
                groups.append((unit, comp))
                continue
            issues.append((unit, {"nconsts": ncs, "mentions": [r[0] for r in comp]}))
            for nc in ncs:
                groups.append((unit, [r for r in comp if r[0] in links and links[r[0]][0] == nc]))
            rest = [r for r in comp if r[0] not in links]
            if rest:
                groups.append((unit, rest))
    page_works: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        page_works[r[5]].add(unit_of[r[0]][0])
    people = _imdb_people_counts(conn, ctx, set(page_works))
    group_rows, member_rows = [], []
    for unit, comp in groups:
        comp.sort(key=lambda r: r[0])
        rep = comp[0]
        gkey = f"{unit}|{rep[0]}"
        ncs = [links[r[0]] for r in comp if r[0] in links]
        movie = unit_of[rep[0]][1]
        route = "credits" if any(x[1] == "credits" for x in ncs) else (ncs[0][1] if ncs else None)
        group_rows.append((i.extraction_id, key, gkey, unit, movie, sorted({r[3] for r in comp}), rep[5], rep[6],
                           rep[7], rep[8], rep[9], ncs[0][0] if ncs else None, route,
                           year_of.get(movie) if movie else None, sorted(pubs_of.get(movie, set())) if movie else [],
                           Jsonb({"page_works": len(page_works[rep[5]]), "imdb_people": people.get(rep[5], 0)})))
        for r in comp:
            reason = ("singleton" if len(comp) == 1 else "linked_same_nconst" if r[0] in links
                      else "same_strict" if r[5] == rep[5] else "same_tokenset")
            member_rows.append((i.extraction_id, key, gkey, r[0], reason))
    with conn.transaction():
        copy_rows(conn, "task5.person_group", ("extraction_id", "stage_key", "group_key", "work_unit", "movie_id",
                                               "role_classes", "strict_key", "loose_key", "tokenset_key", "family_key",
                                               "initial_key", "nconst", "nconst_route", "year", "publishers",
                                               "rarity"), group_rows)
        copy_rows(conn, "task5.group_member", ("extraction_id", "stage_key", "group_key", "mention_key", "reason"),
                  member_rows)
        if issues:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO task5.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                                "VALUES (%s, %s, %s, 'two_nconst_in_group', %s)",
                                [(ctx.run_id, i.extraction_id, u, Jsonb(d)) for u, d in issues])
    return {"groups": len(group_rows), "members": len(member_rows), "two_nconst_groups": len(issues)}


def load_groups(conn: psycopg.Connection, ctx: RunContext) -> dict[str, Group]:
    i = ctx.inputs
    key = i.stage_key(CREDITS)
    raws = dict(conn.execute("SELECT mention_key, raw_value FROM task5.person_mention WHERE extraction_id = %s "
                             "AND parser_version = %s AND norm_version = %s", (i.extraction_id, i.parser_version,
                                                                              i.norm_version)).fetchall())
    members: dict[str, list[str]] = defaultdict(list)
    for gkey, mkey in conn.execute("SELECT group_key, mention_key FROM task5.group_member WHERE extraction_id = %s "
                                   "AND stage_key = %s ORDER BY mention_key COLLATE \"C\"",
                                   (i.extraction_id, key)).fetchall():
        members[gkey].append(mkey)
    wanted = [r[0] for r in conn.execute("SELECT DISTINCT nconst FROM task5.person_group WHERE extraction_id = %s "
                                         "AND stage_key = %s AND nconst IS NOT NULL", (i.extraction_id, key)).fetchall()]
    life = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT nconst, birth_year, death_year FROM imdb.name WHERE snapshot_id = %s AND nconst = ANY(%s)",
        (i.imdb_snapshot_id, wanted)).fetchall()}
    out: dict[str, Group] = {}
    for r in conn.execute(
            "SELECT group_key, work_unit, movie_id, role_classes, strict_key, loose_key, tokenset_key, family_key, "
            "initial_key, nconst, nconst_route, year, publishers, rarity FROM task5.person_group "
            "WHERE extraction_id = %s AND stage_key = %s ORDER BY group_key COLLATE \"C\"", (i.extraction_id, key)).fetchall():
        g = Group(r[0], r[1], r[2], set(r[3]), r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], set(r[12]), r[13],
                  members[r[0]], names.parse(raws[r[0].split("|", 1)[1]]))
        if g.nconst:
            g.birth, g.death = life.get(g.nconst, (None, None))
        out[g.key] = g
    return out


# ---------------------------------------------------------------- 候选生成

def _pairs_in(block: list[Group]) -> list[tuple[Group, Group]]:
    return [(a, b) for a, b in itertools.combinations(block, 2)
            if a.work_unit != b.work_unit and not (a.nconst and a.nconst == b.nconst)]


def generate_pairs(groups: dict[str, Group], rules: RuleSet) -> tuple[dict[str, list], list[tuple[str, dict]]]:
    """分块键 → [(group_a, group_b, 生成器, 块是否溢出)]，每个候选只在第一个产生它的块里；以及质量问题 (块, 细节)。"""
    gs = sorted(groups.values(), key=lambda g: g.key)
    index: dict[tuple, list[Group]] = defaultdict(list)
    for g in gs:
        index[("C1", g.strict)].append(g)
        index[("C2t", g.tokenset)].append(g)
        index[("C2l", g.loose)].append(g)
        if g.initial:
            index[("C3", g.initial)].append(g)
            index[("C4", g.family)].append(g)
    seen: set[tuple[str, str]] = set()
    blocks: dict[str, list] = {}
    issues: list[tuple[str, dict]] = []
    for kind in ("C1", "C2t", "C2l", "C3", "C4"):
        for (k, bkey), block in sorted((x for x in index.items() if x[0][0] == kind), key=lambda x: x[0][1]):
            if len(block) < 2:
                continue
            gen = "C2" if kind.startswith("C2") else kind
            bid = f"block:{kind}:{bkey}"
            if kind == "C4" and len(block) > rules.c4_block_limit:
                issues.append((bid, {"groups": len(block), "limit": rules.c4_block_limit, "skipped": True}))
                continue
            overflow = len(block) > rules.block_limit
            if overflow:
                issues.append((bid, {"groups": len(block), "limit": rules.block_limit, "skipped": False}))
            out = []
            for a, b in _pairs_in(block):
                a, b = (a, b) if a.key < b.key else (b, a)
                if (a.key, b.key) in seen:
                    continue
                if kind in ("C2t", "C2l") and a.strict == b.strict:
                    continue                                         # 严格键相同的归 C1
                if kind == "C3" and not (len(a.strict.split()[0]) == 1 or len(b.strict.split()[0]) == 1):
                    continue
                if kind == "C4" and (a.strict == b.strict or names.level(a.name, b.name) != names.L_NEAR):
                    continue
                seen.add((a.key, b.key))
                out.append((a.key, b.key, gen, overflow))
            if out:
                blocks[bid] = sorted(out)
    return blocks, issues


# ---------------------------------------------------------------- 特征与判定

@dataclass
class Clusters:
    """当前人物簇：group_key → 簇号；簇号取簇内最小 group_key（确定性）。"""
    of: dict[str, str]
    members: dict[str, set[str]]

    @classmethod
    def initial(cls, groups: dict[str, Group]) -> "Clusters":
        c = cls({k: k for k in groups}, {k: {k} for k in groups})
        by_nconst: dict[str, list[str]] = defaultdict(list)
        for k, g in groups.items():
            if g.nconst:
                by_nconst[g.nconst].append(k)
        for keys in by_nconst.values():
            for other in keys[1:]:
                c.merge(keys[0], other)
        return c

    def merge(self, a: str, b: str) -> str:
        ca, cb = self.of[a], self.of[b]
        if ca == cb:
            return ca
        keep, drop = min(ca, cb), max(ca, cb)
        for k in self.members[drop]:
            self.of[k] = keep
        self.members[keep] |= self.members.pop(drop)
        return keep


def pair_input(a: Group, b: Group, groups: dict[str, Group], units: dict[str, list[str]], cl: Clusters,
               rules: RuleSet, overflow: bool) -> tuple[PairInput, dict[str, Any]]:
    ca, cb = cl.of[a.key], cl.of[b.key]
    own = {a.key, b.key}
    side_a = {cl.of[k] for k in units[a.work_unit] if k not in own} - {ca, cb}
    side_b = {cl.of[k] for k in units[b.work_unit] if k not in own} - {ca, cb}
    shared = side_a & side_b

    def named(g: Group) -> set[str]:
        return {groups[k].strict for k in units[g.work_unit] if k not in own and cl.of[k] not in shared | {ca, cb}
                and len(groups[k].strict.split()) >= 2 and groups[k].strict not in (a.strict, b.strict)}

    same_name = named(a) & named(b)
    lvl = names.level(a.name, b.name)
    level = names.N_LEVEL.get(lvl) if lvl else None
    era = (a.nconst and era_conflict(a.birth, a.death, b.year, rules)) or (
        b.nconst and era_conflict(b.birth, b.death, a.year, rules))
    da, db = a.name.disambiguator, b.name.disambiguator
    pub = sorted(a.publishers & b.publishers)
    inp = PairInput(level, bool(a.roles & b.roles), a.nconst, b.nconst, len(shared), len(same_name), bool(era),
                    bool(da and db and da != db), is_rare(a.rarity.get("page_works", 99), a.rarity.get("imdb_people", 99),
                                                          rules) and is_rare(b.rarity.get("page_works", 99),
                                                                              b.rarity.get("imdb_people", 99), rules),
                    bool(pub), overflow)
    feats = {"n_level": level, "name_a": a.name.display, "name_b": b.name.display,
             "roles": {"a": sorted(a.roles), "b": sorted(b.roles), "overlap": inp.roles_overlap},
             "external": {"a": a.nconst, "b": b.nconst},
             "collaborators": {"cluster": sorted(shared), "same_name": sorted(same_name)},
             "era": {"a_year": a.year, "b_year": b.year, "conflict": bool(era)},
             "disambiguator": {"a": da, "b": db},
             "rarity": {"a": a.rarity, "b": b.rarity, "rare_both": inp.rare_both},
             "publishers": {"overlap": pub},
             "mentions": {"a": a.rep, "b": b.rep}}
    return inp, feats


def decision_row(verdict, feats, rules: RuleSet) -> tuple[Any, ...]:
    feats = {**feats, "feature_switches": {"use_rarity": rules.use_rarity, "use_publisher": rules.use_publisher,
                                           "tier_if_enabled": verdict.tier_if_enabled}, "w": verdict.w}
    return (verdict.tier, verdict.score, verdict.confidence, verdict.veto, verdict.reason, Jsonb(feats),
            Jsonb(rules.thresholds()))


def units_of(groups: dict[str, Group]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for k, g in groups.items():
        out[g.work_unit].append(k)
    return out


def count_pending(conn: psycopg.Connection, ctx: RunContext, blocks: dict[str, list]) -> int:
    done = {r[0] for r in conn.execute("SELECT entity_id FROM task5.entity_stage WHERE extraction_id = %s AND "
                                       "stage = %s AND stage_key = %s", (ctx.inputs.extraction_id, STAGE,
                                                                         ctx.inputs.stage_key(STAGE))).fetchall()}
    return len([b for b in blocks if b not in done])


@dataclass
class DecideStats:
    blocks: int = 0
    candidates: int = 0
    batches: int = 0
    remaining: int = 0
    tiers: dict[str, int] = field(default_factory=dict)


def decide_blocks(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet | None = None, *, batch_size: int = 2000,
                  limit: int | None = None, before_commit: BeforeCommit | None = None) -> DecideStats:
    """跨作品候选与第 1 轮判定，按分块键分批；中断后只处理没有标记的块。"""
    if not conn.autocommit:
        raise ValueError("装载连接必须是自动提交模式（使用 etl_task1.db.connect）")
    rules = rules or ruleset(ctx.inputs.rule_version)
    i = ctx.inputs
    key = i.stage_key(STAGE)
    groups = load_groups(conn, ctx)
    blocks, issues = generate_pairs(groups, rules)
    units, clusters = units_of(groups), Clusters.initial(groups)
    done = {r[0] for r in conn.execute("SELECT entity_id FROM task5.entity_stage WHERE extraction_id = %s AND stage = %s "
                                       "AND stage_key = %s", (i.extraction_id, STAGE, key)).fetchall()}
    issue_of = dict(issues)
    pending = sorted(b for b in set(blocks) | set(issue_of) if b not in done)
    stats, first_batch = DecideStats(), next_batch_no(conn, ctx, STAGE)
    while pending and (limit is None or stats.blocks < limit):
        chunk = pending[:batch_size if limit is None else min(batch_size, limit - stats.blocks)]
        pending = pending[len(chunk):]
        cand_rows = [(i.extraction_id, key, a, b, gen) for bid in chunk for a, b, gen, _ in blocks.get(bid, [])]
        with conn.transaction():
            copy_rows(conn, "task5.person_candidate", ("extraction_id", "stage_key", "group_a", "group_b", "generator"),
                      cand_rows)
            ids = {(r[1], r[2]): r[0] for r in conn.execute(
                "SELECT person_candidate_id, group_a, group_b FROM task5.person_candidate WHERE extraction_id = %s "
                "AND stage_key = %s AND (group_a, group_b) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
                (i.extraction_id, key, [c[2] for c in cand_rows], [c[3] for c in cand_rows])).fetchall()}
            decisions = []
            for bid in chunk:
                for a, b, gen, overflow in blocks.get(bid, []):
                    inp, feats = pair_input(groups[a], groups[b], groups, units, clusters, rules, overflow)
                    feats["generator"], feats["overflow"] = gen, overflow
                    v = decide_pair(inp, rules)
                    decisions.append((ids[(a, b)], i.rule_version, 1, ctx.run_id) + decision_row(v, feats, rules))
                    stats.tiers[v.tier] = stats.tiers.get(v.tier, 0) + 1
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO task5.person_decision (person_candidate_id, rule_version, round, run_id, tier, score, "
                    "confidence, veto, reason, features, thresholds) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    decisions)
                for bid in chunk:
                    if bid in issue_of:
                        cur.execute("INSERT INTO task5.quality_issue (run_id, extraction_id, entity_id, issue_code, "
                                    "detail) VALUES (%s, %s, %s, 'block_overflow', %s)",
                                    (ctx.run_id, i.extraction_id, bid, Jsonb(issue_of[bid])))
            n = first_batch + stats.batches
            mark_entities(conn, ctx, STAGE, n, [(bid, len(blocks.get(bid, []))) for bid in chunk])
            mark_batch(conn, ctx, STAGE, n, chunk[0], chunk[-1], len(decisions))
            if before_commit:
                before_commit(STAGE, n)
        stats.blocks += len(chunk)
        stats.candidates += len(cand_rows)
        stats.batches += 1
    stats.remaining = len(pending)
    return stats
