"""聚类、一致性检查、稳定 movie_id、映射版本与历史、人工纠正叠加、版本差异（spec R24–R26、R28–R31，AC07–AC09）。

聚类是全局步骤（需要看到全部决策），整个映射版本在一个事务里生成：
1. IMDb 簇：每个商品取它唯一的自动 IMDb 关联，同一 tconst 归一簇；页面簇：没有 IMDb 关联的商品之间用 G3 的 auto 边（R25）。
   review 与 reject 的决策永远不连簇，所以 A≈B、B≈C 不会把冲突的 A、C 连带合并。
2. 人工决策叠加（按导入顺序）：must_link 合并、cannot_link 与 detach 移出；人工优先于自动。
3. 一致性检查：多商品簇内枚举全部商品对，发现硬冲突（导演完全不重叠、标题年份相差超过 1 年）就把关联分较低的商品移出
   并标为 review_pending，写 cluster_check；与硬冲突矛盾的人工合并照常执行，但记录 human_vs_veto。
4. 锚点与 movie_id：有 IMDb 关联的簇取 imdb:<最小 tconst>，否则 asin:<簇内最小 ASIN>；movie_id = mv_ + sha256(锚点) 前 16 位，
   登记后不再变化，所以同一输入重跑、锚点没变的作品在新规则版本下都保持原 movie_id。
5. 合集成员的自动关联生成 contains 关系（没有对应作品时按 imdb:<tconst> 新建没有 edition 商品的作品）。
6. 与父映射版本逐商品对比，生成 create/merge/split/move/retire 事件。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import candidates
from .match import DEFAULT_RULES, PageFacts, RuleSet, director_feature
from .run import RunContext

MAX_CLUSTER = 30


@dataclass
class MappingResult:
    mapping_version_id: str
    parent_version_id: str | None
    counts: dict[str, Any]
    events: dict[str, int] = field(default_factory=dict)
    checks: int = 0


def movie_id_for(anchor_key: str) -> str:
    """R28：movie_id 不含 ASIN 或 IMDb 编号原文，由锚点的哈希得到，同一锚点永远得到同一个。"""
    return "mv_" + hashlib.sha256(anchor_key.encode()).hexdigest()[:16]


class _Clusters:
    """极简并查集 + 移出：成员集合按簇编号保存。"""

    def __init__(self, items: list[str]):
        self.cid = {a: i for i, a in enumerate(items)}
        self.members = {i: {a} for i, a in enumerate(items)}
        self._next = len(items)

    def union(self, a: str, b: str) -> None:
        ca, cb = self.cid[a], self.cid[b]
        if ca == cb:
            return
        if len(self.members[ca]) < len(self.members[cb]):
            ca, cb = cb, ca
        for x in self.members[cb]:
            self.cid[x] = ca
        self.members[ca] |= self.members.pop(cb)

    def remove(self, a: str) -> None:
        c = self.cid[a]
        if len(self.members[c]) == 1:
            return
        self.members[c].discard(a)
        self.cid[a] = self._next
        self.members[self._next] = {a}
        self._next += 1

    def same(self, a: str, b: str) -> bool:
        return self.cid[a] == self.cid[b]


def _conflict(a: PageFacts, b: PageFacts, rules: RuleSet) -> str | None:
    """R14 的硬冲突：两边都有导演而姓名键完全不重叠，或两边标题年份相差超过 1 年。"""
    if director_feature(a.directors, b.directors, rules).get("veto"):
        return "director_disjoint"
    if a.title_year is not None and b.title_year is not None and abs(a.title_year - b.title_year) > rules.year_tolerance:
        return "year_conflict"
    return None


def _load_inputs(conn: psycopg.Connection, ctx: RunContext) -> dict[str, Any]:
    i = ctx.inputs
    products = conn.execute(
        "SELECT asin, product_kind FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s "
        "AND match_scope = 'matched' ORDER BY asin COLLATE \"C\"", (i.extraction_id, i.t3_rule_version)).fetchall()
    movies = [a for a, k in products if k == "movie"]
    collections = [a for a, k in products if k == "collection"]
    base = (i.extraction_id, i.norm_version, i.imdb_snapshot_id, i.rule_version)
    links = {r[0]: r[1:] for r in conn.execute(
        """SELECT c.left_entity, c.right_id, d.decision_id, d.score FROM task4.merge_decision d
           JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND c.right_kind = 'imdb'
             AND d.rule_version = %s AND d.tier = 'auto'""", base).fetchall()}
    edges = conn.execute(
        """SELECT c.left_entity, c.right_id, d.decision_id, d.score FROM task4.merge_decision d
           JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND c.right_kind = 'product'
             AND d.rule_version = %s AND d.tier = 'auto' AND d.reason = 'page_cluster'
           ORDER BY c.left_entity, c.right_id""", base).fetchall()
    review: dict[str, str] = {}
    for left, right, kind, reason in conn.execute(
            """SELECT c.left_entity, c.right_id, c.right_kind, d.reason FROM task4.merge_decision d
               JOIN task4.candidate c USING (candidate_id)
               WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s
                 AND d.tier = 'review' ORDER BY c.candidate_id""", base).fetchall():
        review.setdefault(left, reason or "review")
        if kind == "product":
            review.setdefault(right, reason or "review")
    humans = conn.execute(
        "SELECT human_decision_id, kind, left_entity, right_entity FROM task4.human_decision "
        "WHERE extraction_id = %s ORDER BY human_decision_id", (i.extraction_id,)).fetchall()
    return {"movies": movies, "collections": collections, "links": links, "edges": edges, "review": review,
            "humans": humans}


def _facts(conn: psycopg.Connection, ctx: RunContext, asins: list[str]) -> dict[str, PageFacts]:
    out: dict[str, PageFacts] = {}
    for k in range(0, len(asins), 1000):
        out.update(candidates.load_page_facts(conn, ctx, asins[k:k + 1000]))
    return out


def build_mapping(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet = DEFAULT_RULES,
                  ) -> MappingResult:
    """生成一个新的映射版本（整个版本一个事务）。调用方保证已持有抽取锁。"""
    if rules.version != ctx.inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 运行输入里的规则版本 {ctx.inputs.rule_version}")
    i = ctx.inputs
    data = _load_inputs(conn, ctx)
    movies, links, review = data["movies"], data["links"], data["review"]
    facts = _facts(conn, ctx, movies)
    cl = _Clusters(movies)

    # 1. 自动聚类：同一 tconst 一簇；页面簇用 R25 的 auto 边
    link = {a: links[a] for a in movies if a in links}                 # asin → (tconst, decision_id, score)
    by_tconst: dict[str, list[str]] = defaultdict(list)
    for a, (t, _, _) in link.items():
        by_tconst[t].append(a)
    for group in by_tconst.values():
        for other in group[1:]:
            cl.union(group[0], other)
    edge_best: dict[str, tuple[float, int]] = {}
    for a, b, did, score in data["edges"]:
        if a in cl.cid and b in cl.cid:
            cl.union(a, b)
            for x in (a, b):
                if x not in edge_best or float(score) > edge_best[x][0]:
                    edge_best[x] = (float(score), did)

    # 2. 人工决策叠加（人工优先于自动）
    human_by_asin: dict[str, int] = {}
    pinned: set[frozenset[str]] = set()
    for hid, kind, left, right in data["humans"]:
        if left not in cl.cid or (right is not None and right not in cl.cid):
            continue                                                    # 引用的商品不在本次输入里（导入时已校验）
        if kind == "must_link":
            cl.union(left, right)
            pinned.add(frozenset((left, right)))
            human_by_asin[left] = human_by_asin[right] = hid
        elif kind == "cannot_link":
            if cl.same(left, right):
                cl.remove(right)
                link.pop(right, None)
                edge_best.pop(right, None)
                pinned = {p for p in pinned if right not in p}
                human_by_asin[right] = hid
        elif kind == "detach":
            cl.remove(left)
            link.pop(left, None)
            edge_best.pop(left, None)
            pinned = {p for p in pinned if left not in p}
            human_by_asin[left] = hid

    # 3. 一致性检查（R26）
    def assoc(a: str) -> float:
        return float(link[a][2]) if a in link else (edge_best[a][0] if a in edge_best else 0.0)

    cluster_checks: list[tuple] = []        # (movie 暂不知，asin_a, asin_b, conflict, action, resolved_by)
    pending_reason: dict[str, str] = {}
    quality: list[tuple] = []
    seen_human: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for members in [sorted(m) for m in cl.members.values() if len(m) > 1]:
            for a, b in combinations(members, 2):
                why = _conflict(facts[a], facts[b], rules)
                if why is None:
                    continue
                if frozenset((a, b)) in pinned:
                    if (a, b) not in seen_human:
                        seen_human.add((a, b))
                        cluster_checks.append((a, b, why, "kept_by_human", "human"))
                        quality.append((ctx.run_id, i.extraction_id, a, "human_vs_veto",
                                        Jsonb({"other": b, "conflict": why})))
                    continue
                loser = a if assoc(a) < assoc(b) else b                    # 关联分低者移出；同分移出 ASIN 较大者（a < b）
                cluster_checks.append((a, b, why, "removed_a" if loser == a else "removed_b", "system"))
                cl.remove(loser)
                link.pop(loser, None)
                edge_best.pop(loser, None)
                pending_reason[loser] = "cluster_conflict"
                changed = True
                break
            if changed:
                break
    for members in cl.members.values():
        if len(members) > MAX_CLUSTER:
            quality.append((ctx.run_id, i.extraction_id, min(members), "cluster_too_large",
                            Jsonb({"size": len(members)})))

    # 4. 锚点与作品
    clusters = sorted((sorted(m) for m in cl.members.values()), key=lambda m: m[0])
    anchor_of: dict[int, str] = {}
    cluster_of_asin: dict[str, int] = {}
    for n, members in enumerate(clusters):
        tconsts = sorted({link[a][0] for a in members if a in link})
        anchor_of[n] = f"imdb:{tconsts[0]}" if tconsts else f"asin:{members[0]}"
        for a in members:
            cluster_of_asin[a] = n
    movie_of_tconst: dict[str, int] = {link[a][0]: cluster_of_asin[a] for a in link}

    # 5. 合集成员 → contains
    contains: list[tuple[str, int, str, int]] = []        # (合集 asin, 成员序号, tconst, decision_id)
    member_total: dict[str, int] = defaultdict(int)
    member_linked: dict[str, int] = defaultdict(int)
    member_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM task4.field_assertion WHERE extraction_id = %s AND parser_version = %s AND "
        "norm_version = %s AND entity_type = 'collection_member' AND entity_id LIKE ANY(%s) ORDER BY 1",
        (i.extraction_id, i.parser_version, i.norm_version, [f"{c}#%" for c in data["collections"]])).fetchall()] \
        if data["collections"] else []
    for m in member_ids:
        base, ordinal = m.split("#")
        member_total[base] += 1
        if m in links:
            contains.append((base, int(ordinal), links[m][0], links[m][1]))
            member_linked[base] += 1
        else:
            quality.append((ctx.run_id, i.extraction_id, m, "collection_member_unmatched",
                            Jsonb({"reason": "review" if m in review else "no_auto_match"})))
    extra_tconsts = sorted({t for _, _, t, _ in contains} - set(movie_of_tconst))

    # 6. 登记 movie_id、写映射版本
    all_anchors = [anchor_of[n] for n in range(len(clusters))] + [f"imdb:{t}" for t in extra_tconsts]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task4.movie_identity (anchor_key, movie_id, created_run) VALUES (%s, %s, %s) "
                            "ON CONFLICT (anchor_key) DO NOTHING",
                            [(a, movie_id_for(a), ctx.run_id) for a in all_anchors])
        ids = dict(conn.execute("SELECT anchor_key, movie_id FROM task4.movie_identity WHERE anchor_key = ANY(%s)",
                                (all_anchors,)).fetchall())
        n_versions = conn.execute("SELECT count(*) FROM task4.mapping_version WHERE extraction_id = %s",
                                  (i.extraction_id,)).fetchone()[0]
        parent = conn.execute("SELECT mapping_version_id FROM task4.mapping_version WHERE extraction_id = %s "
                              "ORDER BY created_at DESC, mapping_version_id DESC LIMIT 1",
                              (i.extraction_id,)).fetchone()
        parent_id = parent[0] if parent else None
        version = f"map{n_versions + 1:04d}"
        movie_ids = {n: ids[anchor_of[n]] for n in range(len(clusters))}
        movie_id_of_asin = {a: movie_ids[n] for a, n in cluster_of_asin.items()}
        movie_of_extra = {t: ids[f"imdb:{t}"] for t in extra_tconsts}

        # 显示标题：IMDb 作品取主标题，页面作品取簇内最小 ASIN 的标题主体
        tcs = sorted({a.split(":", 1)[1] for a in all_anchors if a.startswith("imdb:")} |
                     {link[a][0] for a in link} | set(extra_tconsts))
        imdb_titles = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT tconst, primary_title, line_no FROM imdb.title WHERE snapshot_id = %s AND tconst = ANY(%s)",
            (i.imdb_snapshot_id, tcs)).fetchall()}
        page_titles = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT entity_id, extra->>'body', assertion_id FROM task4.field_assertion WHERE extraction_id = %s AND "
            "parser_version = %s AND norm_version = %s AND field_name = 'title' AND entity_id = ANY(%s)",
            (i.extraction_id, i.parser_version, i.norm_version, movies)).fetchall()}

        def movie_row(anchor: str, mid: str, edition_count: int, contains_count: int, rep: str | None) -> tuple:
            if anchor.startswith("imdb:"):
                t = anchor.split(":", 1)[1]
                title, line = imdb_titles.get(t, (None, None))
                return (version, mid, "imdb_linked", anchor, t, title,
                        Jsonb({"imdb": {"tconst": t, "file": "title.basics.tsv.gz", "line_no": line}}),
                        edition_count, contains_count)
            title, aid = page_titles.get(rep, (None, None))
            return (version, mid, "page_only", anchor, None, title,
                    Jsonb({"assertion_id": aid}) if aid else None, edition_count, contains_count)

        contains_per_movie: dict[str, int] = defaultdict(int)
        contains_rows = []
        for base, ordinal, t, did in contains:
            mid = movie_ids[movie_of_tconst[t]] if t in movie_of_tconst else movie_of_extra[t]
            contains_rows.append((version, base, ordinal, mid, "contains", "auto_decision", did, None))
            contains_per_movie[mid] += 1
        movie_rows = [movie_row(anchor_of[n], movie_ids[n], len(clusters[n]), contains_per_movie[movie_ids[n]],
                                clusters[n][0]) for n in range(len(clusters))]
        movie_rows += [movie_row(f"imdb:{t}", movie_of_extra[t], 0, contains_per_movie[movie_of_extra[t]], None)
                       for t in extra_tconsts]

        # 计数与状态
        pending: dict[str, str] = dict(pending_reason)
        for a in movies:
            if len(clusters[cluster_of_asin[a]]) == 1 and a not in pending and a not in link and a in review:
                pending[a] = review[a]
        status_rows: list[tuple] = []
        for a in movies:
            size = len(clusters[cluster_of_asin[a]])
            if size > 1:
                status_rows.append((version, a, "merged", None))
            elif a in pending:
                status_rows.append((version, a, "review_pending", pending[a]))
            else:
                status_rows.append((version, a, "singleton", None))
        for c in data["collections"]:
            if member_total.get(c):
                status_rows.append((version, c, "collection_split",
                                    f"{member_linked.get(c, 0)}/{member_total[c]} members linked"))
            else:
                status_rows.append((version, c, "collection_unsplit", "no_members"))
        edition_rows = []
        for a in movies:
            mid = movie_id_of_asin[a]
            if a in human_by_asin:
                basis, did, hid = "human", None, human_by_asin[a]
            elif a in link:
                basis, did, hid = "auto_decision", link[a][1], None
            elif a in edge_best and len(clusters[cluster_of_asin[a]]) > 1:
                basis, did, hid = "auto_decision", edge_best[a][1], None
            else:
                basis, did, hid = "singleton", None, None
            edition_rows.append((version, a, 0, mid, "edition", basis, did, hid))
        multi = sum(1 for m in clusters if len(m) > 1)
        counts = {
            "participating_products": len(movies), "collections": len(data["collections"]),
            "movies_with_editions": len(clusters), "multi_product_movies": multi,
            "single_product_movies": len(clusters) - multi,
            "products_in_multi_product_movies": sum(len(m) for m in clusters if len(m) > 1),
            "review_pending_products": sum(1 for r in status_rows if r[2] == "review_pending"),
            "imdb_linked_movies": sum(1 for r in movie_rows if r[2] == "imdb_linked"),
            "page_only_movies": sum(1 for r in movie_rows if r[2] == "page_only"),
            "contains_only_movies": sum(1 for r in movie_rows if r[7] == 0),
            "contains_relations": len(contains_rows),
            "collections_split": sum(1 for r in status_rows if r[2] == "collection_split"),
            "collections_unsplit": sum(1 for r in status_rows if r[2] == "collection_unsplit"),
            "cluster_conflicts_removed": sum(1 for c in cluster_checks if c[3].startswith("removed")),
        }
        conn.execute(
            "INSERT INTO task4.mapping_version (mapping_version_id, extraction_id, parent_version_id, run_id, "
            "input_key, t3_rule_version, parser_version, norm_version, rule_version, imdb_snapshot_id, "
            "human_set_hash, counts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (version, i.extraction_id, parent_id, ctx.run_id, i.key(), i.t3_rule_version, i.parser_version,
             i.norm_version, i.rule_version, i.imdb_snapshot_id, i.human_set_hash, Jsonb(counts)))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, tconst, display_title, "
                "display_title_source, edition_count, contains_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                movie_rows)
            cur.executemany(
                "INSERT INTO task4.product_movie (mapping_version_id, asin, member_ordinal, movie_id, relation_type, "
                "basis, decision_id, human_decision_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                edition_rows + contains_rows)
            cur.executemany("INSERT INTO task4.product_match_status (mapping_version_id, asin, status, reason) "
                            "VALUES (%s, %s, %s, %s)", status_rows)
            cur.executemany(
                "INSERT INTO task4.cluster_check (mapping_version_id, movie_id, asin_a, asin_b, conflict, action, "
                "resolved_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(version, movie_id_of_asin[a], a, b, why, action, by) for a, b, why, action, by in cluster_checks])
            if quality:
                cur.executemany("INSERT INTO task4.quality_issue (run_id, extraction_id, entity_id, issue_code, "
                                "detail) VALUES (%s, %s, %s, %s, %s)", quality)
        events = _write_events(conn, version, parent_id, {a: movie_id_of_asin[a] for a in movies},
                               {r[1] for r in movie_rows}, human_by_asin)
    return MappingResult(version, parent_id, counts, events, len(cluster_checks))


def _write_events(conn: psycopg.Connection, version: str, parent_id: str | None, new_map: dict[str, str],
                  new_movies: set[str], human_by_asin: dict[str, int]) -> dict[str, int]:
    """与父版本逐商品对比（R29）：create（首个版本）、merge（并入已有作品）、split（离开仍存在的作品成为新作品）、
    move（在两部已有作品之间移动）、retire（旧作品不再出现，to 指向接收它成员最多的作品）。"""
    rows: list[tuple] = []
    if parent_id is None:
        rows = [(version, "create", a, None, m, "first_version", None, human_by_asin.get(a))
                for a, m in sorted(new_map.items())]
    else:
        old_map = {r[0]: r[1] for r in conn.execute(
            "SELECT asin, movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND "
            "relation_type = 'edition'", (parent_id,)).fetchall()}
        old_movies = {r[0] for r in conn.execute("SELECT movie_id FROM task4.movie WHERE mapping_version_id = %s",
                                                 (parent_id,)).fetchall()}
        moved_to: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for a, new in sorted(new_map.items()):
            old = old_map.get(a)
            if old is None:
                rows.append((version, "create", a, None, new, "new_product", None, human_by_asin.get(a)))
                continue
            if old == new:
                continue
            moved_to[old][new] += 1
            hid = human_by_asin.get(a)
            if new in old_movies:
                kind = "merge" if old not in new_movies else "move"
            else:
                kind = "split" if old in new_movies else "move"
            reason = "human_decision" if hid else "auto_decision_changed"
            rows.append((version, kind, a, old, new, reason, None, hid))
        for old in sorted(old_movies - new_movies):
            dest = moved_to.get(old)
            to = max(dest, key=lambda m: (dest[m], m)) if dest else None
            rows.append((version, "retire", None, old, to, "anchor_no_longer_present", None, None))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO task4.mapping_event (mapping_version_id, event, asin, from_movie_id, "
                        "to_movie_id, reason, decision_id, human_decision_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        rows)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r[1]] += 1
    return dict(counts)


# ---------------------------------------------------------------- 查询与差异

def diff_versions(conn: psycopg.Connection, old_version: str, new_version: str) -> list[dict[str, Any]]:
    """R31：两个映射版本之间 movie_id 发生变化的商品，附事件、原因和两个版本里各自的 IMDb 关联判定。"""
    def link_info(version: str) -> dict[str, tuple]:
        row = conn.execute("SELECT extraction_id, norm_version, imdb_snapshot_id, rule_version "
                           "FROM task4.mapping_version WHERE mapping_version_id = %s", (version,)).fetchone()
        return {r[0]: r[1:] for r in conn.execute(
            """SELECT DISTINCT ON (c.left_entity) c.left_entity, d.tier, d.score, c.right_id
               FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
               WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND c.right_kind = 'imdb'
                 AND d.rule_version = %s AND c.left_entity NOT LIKE '%%#%%'
               ORDER BY c.left_entity, (d.tier = 'auto') DESC, d.score DESC, c.right_id""", row).fetchall()}

    old_info, new_info = link_info(old_version), link_info(new_version)
    events = {r[0]: r[1:] for r in conn.execute(
        "SELECT asin, event, reason, human_decision_id FROM task4.mapping_event WHERE mapping_version_id = %s "
        "AND asin IS NOT NULL", (new_version,)).fetchall()}
    rows = conn.execute(
        """SELECT o.asin, o.movie_id, n.movie_id FROM task4.product_movie o
           JOIN task4.product_movie n ON n.asin = o.asin AND n.relation_type = 'edition' AND n.member_ordinal = 0
           WHERE o.mapping_version_id = %s AND n.mapping_version_id = %s AND o.relation_type = 'edition'
             AND o.movie_id <> n.movie_id ORDER BY o.asin COLLATE "C" """, (old_version, new_version)).fetchall()
    out = []
    for asin, old_m, new_m in rows:
        ev = events.get(asin, (None, None, None))
        o, n = old_info.get(asin), new_info.get(asin)
        out.append({"asin": asin, "old_movie_id": old_m, "new_movie_id": new_m, "event": ev[0], "reason": ev[1],
                    "human_decision_id": ev[2], "old_tier": o[0] if o else None, "old_score": float(o[1]) if o else None,
                    "new_tier": n[0] if n else None, "new_score": float(n[1]) if n else None})
    return out


def record_scopes(conn: psycopg.Connection, mapping_version_id: str) -> dict[str, int]:
    """R19：评论记录数按口径拆分，各口径之和等于清单记录总数。
    edition = 电影类商品（经 edition 关系归到作品）；collection = 合集商品（不复制给成员）；
    other_video = 剧集等有页面但不匹配的类别；no_page = 没有可解析页面。"""
    v = conn.execute("SELECT extraction_id, t3_rule_version FROM task4.mapping_version WHERE mapping_version_id = %s",
                     (mapping_version_id,)).fetchone()
    rows = conn.execute(
        """SELECT CASE WHEN e.match_scope = 'no_page' THEN 'no_page'
                       WHEN e.product_kind = 'movie' AND e.match_scope = 'matched' THEN 'edition'
                       WHEN e.product_kind = 'collection' AND e.match_scope = 'matched' THEN 'collection'
                       ELSE 'other_video' END, count(*), sum(m.records_total)
           FROM task4.product_edition e JOIN task2.product_manifest m
             ON m.extraction_id = e.extraction_id AND m.asin = e.asin
           WHERE e.extraction_id = %s AND e.t3_rule_version = %s GROUP BY 1""", v).fetchall()
    out = {"edition": 0, "collection": 0, "other_video": 0, "no_page": 0}
    products = dict.fromkeys(out, 0)
    for scope, n, records in rows:
        out[scope], products[scope] = int(records), int(n)
    out["manifest_total"] = sum(out.values())
    out.update({f"products_{k}": n for k, n in products.items()})
    return out
