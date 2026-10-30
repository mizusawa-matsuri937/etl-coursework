"""合作者佐证迭代、聚类一致性、人工决策叠加与人物生成（spec R21–R29，AC05–AC07，design“合作者佐证迭代、聚类与一致性”）。

全局步骤，一个事务生成整个人物映射版本：
1. 第 0 步：同一 nconst 的组合成一簇。
2. 第 1 轮：取分批判定落下的 auto 边，按（得分降序、候选 ID 升序）依次尝试合并两簇；合并前检查两簇所有组之间的硬冲突，
   有冲突的边不执行，改记 review 并写 cluster_check。
3. 第 k 轮：只重算“两边作品里有组在上一轮换了簇”的候选；判定档变化时新增 round = k 的决策；新的 auto 边同样顺序执行。
   某一轮没有新执行的合并即停止（上限 rules.max_rounds）；每轮写 iteration_round。
4. 人工决策（迭代后、不再迭代）、person_identity（锚点登记）、人物、外部 ID、提及归属、别名、版本与事件。
复核与不合并的边永远不进入簇（R23）。
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task4.normalize import fold

from . import names
from .external import CREDITS
from .resolve import (STAGE, Clusters, Group, _links, decide_pair, decision_row, load_groups, pair_input, units_of)
from .rules import PairInput, RuleSet, era_conflict, ruleset
from .run import BeforeCommit, RunContext


@dataclass
class PeopleResult:
    person_mapping_version_id: str
    counts: dict[str, Any] = field(default_factory=dict)


def cluster_conflict(cl: Clusters, groups: dict[str, Group], ga: str, gb: str, rules: RuleSet) -> str | None:
    """两簇所有组之间的硬冲突（R20、R23）：两个不同 nconst、年代不相容、不同的括号消歧段。"""
    a, b = cl.members[cl.of[ga]], cl.members[cl.of[gb]]
    na = {groups[k].nconst for k in a if groups[k].nconst}
    nb = {groups[k].nconst for k in b if groups[k].nconst}
    if len(na | nb) > 1:
        return "two_external_ids"
    for x, y in ((a, b), (b, a)):
        for k in x:
            g = groups[k]
            if g.nconst and any(era_conflict(g.birth, g.death, groups[o].year, rules) for o in y):
                return "era_conflict"
    da = {groups[k].name.disambiguator for k in a} - {None}
    db = {groups[k].name.disambiguator for k in b} - {None}
    if da and db and len(da | db) > 1:
        return "different_disambiguator"
    return None


def alias_types(raw: str, canonical: str) -> list[str]:
    """别名与规范姓名的关系，可多选（R28）。"""
    if raw == canonical:
        return ["identical"]
    a, c = names.parse(raw), names.parse(canonical)
    out = []
    if raw.lower() == canonical.lower():
        out.append("case_variant")
    elif fold(raw) == fold(canonical) or fold(a.display) == fold(c.display) and a.display != c.display and a.strict == c.strict and not a.reversed:
        out.append("accent_variant")
    if a.reversed:
        out.append("reversed_order")
    if a.nickname:
        out.append("nickname")
    if a.encoding_damaged:
        out.append("encoding_damaged")
    if a.strict == c.strict:
        if not out and a.display != c.display:
            out.append("punctuation")
    elif a.tokenset == c.tokenset:
        out.append("token_order")
    elif a.loose == c.loose:
        out.append("middle_initial")
    else:
        lvl = names.level(a, c)
        if lvl == names.L_PARTIAL:
            out.append("suffix" if a.suffix_key() != c.suffix_key() and a.core() == c.core() else "middle_name")
        elif lvl:
            out.append("other")
    return out or ["other"]


def _next_pmap(conn: psycopg.Connection) -> str:
    return f"pmap{conn.execute('SELECT count(*) FROM task5.person_mapping_version').fetchone()[0] + 1:04d}"


def person_id_for(anchor: str) -> str:
    return "ps_" + hashlib.sha256(anchor.encode()).hexdigest()[:16]


def _isolate(cl: Clusters, key: str) -> None:
    old = cl.of[key]
    if len(cl.members[old]) == 1:
        return
    rest = cl.members[old] - {key}
    new_id = min(rest)
    del cl.members[old]
    cl.members[new_id] = rest
    for k in rest:
        cl.of[k] = new_id
    cl.of[key] = key
    cl.members[key] = {key}


def build_people(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet | None = None, *,
                 before_commit: BeforeCommit | None = None) -> PeopleResult:
    """全局步骤：迭代、一致性检查、人工决策叠加，生成一个新的人物映射版本及其人物、外部 ID、提及归属与别名。

    同一输入键已有人物映射版本时（上一次运行在生成版本之后、收尾之前失败）直接沿用，不重算；before_commit("cluster", 1)
    在事务提交前调用，抛异常即整个版本回滚（测试注入故障）。"""
    rules = rules or ruleset(ctx.inputs.rule_version)
    i = ctx.inputs
    key = i.stage_key(STAGE)
    have = conn.execute("SELECT person_mapping_version_id, counts FROM task5.person_mapping_version "
                        "WHERE extraction_id = %s AND input_key = %s", (i.extraction_id, i.key())).fetchone()
    if have:
        return PeopleResult(have[0], {**have[1], "resumed": True})
    started = time.monotonic()
    groups = load_groups(conn, ctx)
    units, cl = units_of(groups), Clusters.initial(groups)
    group_of = {m: g.key for g in groups.values() for m in g.mentions}
    cands: dict[int, tuple[str, str]] = {}
    state: dict[int, dict[str, Any]] = {}
    for cid, ga, gb, tier, score, reason, feats in conn.execute(
            "SELECT c.person_candidate_id, c.group_a, c.group_b, d.tier, d.score, d.reason, d.features "
            "FROM task5.person_candidate c JOIN task5.person_decision d USING (person_candidate_id) "
            "WHERE c.extraction_id = %s AND c.stage_key = %s AND d.rule_version = %s AND d.round = 1 "
            "ORDER BY c.person_candidate_id", (i.extraction_id, key, i.rule_version)).fetchall():
        cands[cid] = (ga, gb)
        state[cid] = {"tier": tier, "score": float(score), "feats": feats, "reason": reason}
    pmap = _next_pmap(conn)
    parent = conn.execute("SELECT person_mapping_version_id FROM task5.person_mapping_version "
                          "WHERE extraction_id = %s ORDER BY created_at DESC, person_mapping_version_id DESC LIMIT 1",
                          (i.extraction_id,)).fetchone()
    co_round: dict[str, int] = {}
    rounds: list[dict[str, int]] = []
    with conn.transaction():
        conn.execute("INSERT INTO task5.person_mapping_version (person_mapping_version_id, extraction_id, "
                     "parent_version_id, run_id, input_key, t4_mapping_version_id, parser_version, norm_version, "
                     "rule_version, imdb_snapshot_id, human_set_hash, counts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                     "%s, %s, %s, '{}')", (pmap, i.extraction_id, parent[0] if parent else None, ctx.run_id, i.key(),
                                           i.t4_mapping_version_id, i.parser_version, i.norm_version, i.rule_version,
                                           i.imdb_snapshot_id, i.human_set_hash))

        def execute(auto: list[int], rnd: int) -> tuple[int, int, set[str]]:
            done = blocked = 0
            changed: set[str] = set()
            for cid in sorted(auto, key=lambda c: (-state[c]["score"], c)):
                ga, gb = cands[cid]
                if cl.of[ga] == cl.of[gb]:
                    continue
                conflict = cluster_conflict(cl, groups, ga, gb, rules)
                if conflict:
                    blocked += 1
                    state[cid].update(tier="review", reason="edge_blocked")
                    conn.execute("INSERT INTO task5.cluster_check (person_mapping_version_id, round, group_a, group_b, "
                                 "conflict, action, resolved_by) VALUES (%s, %s, %s, %s, %s, 'edge_blocked', 'system')",
                                 (pmap, rnd, ga, gb, conflict))
                    if rnd == 1:
                        conn.execute("UPDATE task5.person_decision SET tier = 'review', reason = 'edge_blocked', "
                                     "veto = '{}' WHERE person_candidate_id = %s AND rule_version = %s AND round = 1",
                                     (cid, i.rule_version))
                    else:
                        _insert_decision(conn, ctx, rules, cid, rnd, "review", "edge_blocked", state[cid])
                    continue
                changed |= cl.members[cl.of[ga]] | cl.members[cl.of[gb]]
                cl.merge(ga, gb)
                co_round.setdefault(ga, rnd)
                co_round.setdefault(gb, rnd)
                done += 1
            return done, blocked, changed

        rnd, changed = 1, set()
        while True:
            t0 = time.monotonic()
            if rnd == 1:
                auto = [c for c, s in state.items() if s["tier"] == "auto"]
            else:
                units_changed = {groups[k].work_unit for k in changed}
                auto = []
                for cid, (ga, gb) in cands.items():
                    if cl.of[ga] == cl.of[gb] or state[cid]["tier"] == "auto" or state[cid]["reason"] == "edge_blocked":
                        continue
                    if groups[ga].work_unit not in units_changed and groups[gb].work_unit not in units_changed:
                        continue
                    f = state[cid]["feats"]
                    inp, feats = pair_input(groups[ga], groups[gb], groups, units, cl, rules, bool(f.get("overflow")))
                    v = decide_pair(inp, rules)
                    if v.tier != state[cid]["tier"]:
                        feats.update(generator=f.get("generator"), overflow=f.get("overflow"))
                        state[cid].update(tier=v.tier, score=float(v.score), reason=v.reason, feats=feats)
                        _insert_decision(conn, ctx, rules, cid, rnd, v.tier, v.reason, state[cid], verdict=v)
                        if v.tier == "auto":
                            auto.append(cid)
            done, blocked, changed = execute(auto, rnd)
            rounds.append({"round": rnd, "new_auto_edges": done, "new_merged_persons": len({cl.of[k] for k in changed}),
                           "blocked_edges": blocked})
            conn.execute("INSERT INTO task5.iteration_round (person_mapping_version_id, round, new_auto_edges, "
                         "new_merged_persons, blocked_edges, finished_at) VALUES (%s, %s, %s, %s, %s, clock_timestamp())",
                         (pmap, rnd, done, rounds[-1]["new_merged_persons"], blocked))
            if done == 0:
                break
            if rnd >= rules.max_rounds:
                conn.execute("INSERT INTO task5.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                             "VALUES (%s, %s, %s, 'iteration_limit', %s)",
                             (ctx.run_id, i.extraction_id, pmap, Jsonb({"max_rounds": rules.max_rounds})))
                break
            rnd += 1
        human_groups = _apply_human(conn, ctx, rules, pmap, groups, cl, group_of)
        counts = _write_people(conn, ctx, rules, pmap, groups, cl, cands, state, co_round, human_groups, rounds,
                               parent[0] if parent else None)
        if before_commit:
            before_commit("cluster", 1)
    counts["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return PeopleResult(pmap, counts)


def _insert_decision(conn, ctx, rules, cid, rnd, tier, reason, st, verdict=None) -> None:
    f = {**st["feats"], "blocked": reason == "edge_blocked"}
    conn.execute(
        "INSERT INTO task5.person_decision (person_candidate_id, rule_version, round, run_id, tier, score, confidence, "
        "veto, reason, features, thresholds) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (cid, ctx.inputs.rule_version, rnd, ctx.run_id, tier, st["score"],
         verdict.confidence if verdict else 0, verdict.veto if verdict else [], reason,
         Jsonb({**f, "feature_switches": {"use_rarity": rules.use_rarity, "use_publisher": rules.use_publisher,
                                          "tier_if_enabled": verdict.tier_if_enabled if verdict else {}}}),
         Jsonb(rules.thresholds())))


# ---------------------------------------------------------------- 人工决策（R29）

def _apply_human(conn, ctx, rules, pmap, groups, cl, group_of) -> set[str]:
    i = ctx.inputs
    touched: set[str] = set()
    rows = conn.execute("SELECT human_decision_id, kind, left_entity, right_entity FROM task5.human_decision "
                        "WHERE extraction_id = %s ORDER BY human_decision_id", (i.extraction_id,)).fetchall()
    for hid, kind, left, right in rows:
        ga = group_of.get(left)
        if ga is None:
            continue
        if kind == "detach" or kind == "cannot_link":
            target = ga if kind == "detach" else group_of.get(right)
            if target is None or (kind == "cannot_link" and cl.of[ga] != cl.of[target]):
                continue
            _isolate(cl, target)
            if groups[target].nconst:
                groups[target].nconst, groups[target].route = None, None
            touched.add(target)
        elif right and right.startswith("imdb:"):
            nconst = right[5:]
            row = conn.execute("SELECT birth_year, death_year FROM imdb.name WHERE snapshot_id = %s AND nconst = %s",
                               (i.imdb_snapshot_id, nconst)).fetchone()
            g = groups[ga]
            g.nconst, g.route = nconst, "human"
            g.birth, g.death = (row if row else (None, None))
            for k, o in groups.items():
                if o.nconst == nconst and cl.of[k] != cl.of[ga]:
                    cl.merge(ga, k)
            touched.add(ga)
        else:
            gb = group_of.get(right)
            if gb is None or cl.of[ga] == cl.of[gb]:
                continue
            conflict = cluster_conflict(cl, groups, ga, gb, rules)
            if conflict:
                conn.execute("INSERT INTO task5.cluster_check (person_mapping_version_id, round, group_a, group_b, "
                             "conflict, action, resolved_by) VALUES (%s, NULL, %s, %s, %s, 'kept_by_human', 'human')",
                             (pmap, ga, gb, conflict))
                conn.execute("INSERT INTO task5.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                             "VALUES (%s, %s, %s, 'human_vs_veto', %s)",
                             (ctx.run_id, i.extraction_id, left, Jsonb({"right": right, "conflict": conflict})))
            cl.merge(ga, gb)
            touched.update((ga, gb))
    return touched


# ---------------------------------------------------------------- 人物、外部 ID、提及归属与别名

def _write_people(conn, ctx, rules, pmap, groups, cl, cands, state, co_round, human_groups, rounds, parent) -> dict:
    i = ctx.inputs
    mention_rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT mention_key, raw_value, display_name, strict_key FROM task5.person_mention WHERE extraction_id = %s "
        "AND parser_version = %s AND norm_version = %s AND status = 'resolvable'",
        (i.extraction_id, i.parser_version, i.norm_version)).fetchall()}
    links = _links(conn, ctx)
    link_review = Counter(r[0] for r in conn.execute(
        "SELECT c.mention_key FROM task5.link_candidate c JOIN task5.link_decision d USING (link_candidate_id) "
        "WHERE c.extraction_id = %s AND c.stage_key = %s AND d.rule_version = %s AND d.tier = 'review'",
        (i.extraction_id, i.stage_key(CREDITS), i.rule_version)).fetchall())
    pending_edges: Counter = Counter()
    for cid, (ga, gb) in cands.items():
        if state[cid]["tier"] == "review" and cl.of[ga] != cl.of[gb]:
            pending_edges[cl.of[ga]] += 1
            pending_edges[cl.of[gb]] += 1
    imdb = {r[0]: r[1:] for r in conn.execute(
        "SELECT nconst, primary_name, birth_year, death_year, primary_profession, line_no FROM imdb.name "
        "WHERE snapshot_id = %s AND nconst = ANY(%s)",
        (i.imdb_snapshot_id, sorted({g.nconst for g in groups.values() if g.nconst}))).fetchall()}
    parent_map = {}
    if parent:
        parent_map = dict(conn.execute("SELECT mention_key, person_id FROM task5.mention_person "
                                       "WHERE person_mapping_version_id = %s", (parent,)).fetchall())
    link_ids: dict[str, list[int]] = defaultdict(list)
    for mkey, (nc, route, movie, did) in links.items():
        link_ids[nc].append(did)
    person_rows, ext_rows, mp_rows, alias_rows, status_count = [], [], [], [], Counter()
    new_map: dict[str, str] = {}
    for cid_, members in sorted(cl.members.items()):
        mkeys = sorted(m for k in members for m in groups[k].mentions)
        nconsts = {groups[k].nconst for k in members if groups[k].nconst}
        nconst = min(nconsts) if nconsts else None
        anchor = f"imdb:{nconst}" if nconst else f"mention:{mkeys[0]}"
        pid = person_id_for(anchor)
        conn.execute("INSERT INTO task5.person_identity (anchor_key, person_id, created_run) VALUES (%s, %s, %s) "
                     "ON CONFLICT (anchor_key) DO NOTHING", (anchor, pid, ctx.run_id))
        pending = pending_edges[cid_] + sum(link_review[m] for m in mkeys)
        status = "external_verified" if nconst else "review_pending" if pending else "page_only"
        status_count[status] += 1
        display = Counter(mention_rows[m][1] for m in mkeys)
        best = sorted(display.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if nconst and nconst in imdb and imdb[nconst][0]:
            canonical, source = imdb[nconst][0], f"imdb:{nconst}"
        else:
            canonical, source = best, f"mention:{next(m for m in mkeys if mention_rows[m][1] == best)}"
        routes = [groups[k].route for k in members if groups[k].nconst]
        route = ("credits" if "credits" in routes else "filmography" if "filmography" in routes
                 else "human" if "human" in routes else None)
        person_rows.append((pmap, pid, status, bool(nconst), route, anchor, canonical, source, len(mkeys),
                            len({groups[k].work_unit for k in members}), pending))
        if nconst:
            nm = imdb.get(nconst, (None, None, None, None, None))
            ext_rows.append((pmap, pid, "imdb", nconst, i.imdb_snapshot_id, nm[4], nm[1], nm[2], nm[3],
                             sorted(link_ids.get(nconst, []))))
        raws = Counter(mention_rows[m][0] for m in mkeys)
        aliases = {a: [alias_types(a, canonical), "page", n] for a, n in raws.items()}
        if nconst and nconst in imdb and imdb[nconst][0]:
            if imdb[nconst][0] in aliases:
                aliases[imdb[nconst][0]][0].append("imdb_primary")
            else:
                aliases[imdb[nconst][0]] = [["imdb_primary"], "imdb", 0]
        alias_rows += [(pmap, pid, a, types, src, n) for a, (types, src, n) in sorted(aliases.items())]
        for k in sorted(members):
            g = groups[k]
            for m in g.mentions:
                if m in links:
                    basis, rnd = links[m][1], None
                elif k in human_groups:
                    basis, rnd = "human", None
                elif k in co_round:
                    basis, rnd = "co_credit", co_round[k]
                elif len(g.mentions) > 1:
                    basis, rnd = "within_work", None
                else:
                    basis, rnd = "singleton", None
                mp_rows.append((pmap, i.extraction_id, i.parser_version, i.norm_version, m, pid, k,
                                "movie" if g.work_unit.startswith("movie:") else "unattributed", basis, rnd))
                new_map[m] = pid
    from .run import copy_rows
    copy_rows(conn, "task5.person", ("person_mapping_version_id", "person_id", "status", "has_external_id",
                                     "external_route", "anchor_key", "canonical_name", "canonical_source",
                                     "mention_count", "work_count", "pending_candidates"), person_rows)
    copy_rows(conn, "task5.person_external_id", ("person_mapping_version_id", "person_id", "system", "external_id",
                                                 "snapshot_id", "name_line_no", "birth_year", "death_year",
                                                 "primary_profession", "basis_decision_ids"), ext_rows)
    copy_rows(conn, "task5.mention_person", ("person_mapping_version_id", "extraction_id", "parser_version",
                                             "norm_version", "mention_key", "person_id", "group_key", "attribution",
                                             "basis", "round"), mp_rows)
    copy_rows(conn, "task5.person_alias", ("person_mapping_version_id", "person_id", "alias", "alias_types", "source",
                                           "occurrences"), alias_rows)
    events = _events(conn, pmap, parent_map, new_map, {r[4]: r[8] for r in mp_rows})
    counts = {"mentions_resolvable": len(mention_rows), "raw_variants": len({v[0] for v in mention_rows.values()}),
              "strict_keys": len({v[2] for v in mention_rows.values()}), "groups": len(groups),
              "persons": len(person_rows), "persons_external_verified": status_count["external_verified"],
              "persons_page_only": status_count["page_only"], "persons_review_pending": status_count["review_pending"],
              "rounds": len(rounds), "iteration": rounds, "events": events,
              "review_candidates": sum(1 for s in state.values() if s["tier"] == "review"),
              "auto_candidates": sum(1 for s in state.values() if s["tier"] == "auto"),
              "mentions_by_basis": dict(Counter(r[8] for r in mp_rows))}
    conn.execute("UPDATE task5.person_mapping_version SET counts = %s WHERE person_mapping_version_id = %s",
                 (Jsonb(counts), pmap))
    return counts


def _events(conn, pmap, old: dict[str, str], new: dict[str, str], basis: dict[str, str]) -> int:
    """由新旧两个人物映射版本逐提及对比生成事件（R44）；第一个版本没有父版本，不生成逐提及事件。"""
    if not old:
        return 0
    rows = []
    sizes = Counter(new.values())
    gone = set(old.values()) - set(new.values())
    for m, pid in sorted(new.items()):
        before = old.get(m)
        if before == pid:
            continue
        if before is None:
            rows.append((pmap, m, "create", None, pid, basis[m]))
        else:
            event = "merge" if pid in old.values() else "split" if before in new.values() else "move"
            rows.append((pmap, m, event, before, pid, basis[m]))
    rows += [(pmap, None, "retire", p, None, "person_retired") for p in sorted(gone)]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO task5.mapping_event (person_mapping_version_id, mention_key, event, "
                        "from_person_id, to_person_id, reason) VALUES (%s, %s, %s, %s, %s, %s)", rows)
    return len(rows)


def diff_versions(conn: psycopg.Connection, old: str, new: str) -> list[tuple]:
    """两个人物映射版本之间 person_id 变化的提及：(mention_key, 旧 person_id, 新 person_id, 原因)。"""
    return conn.execute(
        "SELECT n.mention_key, o.person_id, n.person_id, n.basis FROM task5.mention_person n "
        "LEFT JOIN task5.mention_person o ON o.mention_key = n.mention_key AND o.person_mapping_version_id = %s "
        "WHERE n.person_mapping_version_id = %s AND o.person_id IS DISTINCT FROM n.person_id "
        "ORDER BY n.mention_key", (old, new)).fetchall()
