"""作品层多值字段消解（movie_multivalue，spec R34–R43）：genres、director、starring、cast。

读 link_assessment 决定是否并入 IMDb：已熔断的关联不并入 IMDb 的类型与演职员（R36、R37），与 movie_scalar 读取同一条判定（AC21）。
类型：页面词面先规整（去 see more、还原下划线）再按映射表处置为 mapped、noise、unmapped，逐行保存（R34、R35）；
与未熔断 IMDb 的 title.genres 取受控词并集，成员标 both、imdb_only、page_only（R36）；合集盒子的类型不下推（R05）。
演职员：页面成员来自任务 5 的 movie_person，IMDb 一侧是 title_director 与 title_principal(actor、actress)；并集前把页面未关联
nconst 的人物与同作品同角色的 IMDb 成员用任务 5 的姓名兼容等级对齐，避免同一人出现两个 person_id（R38）；含分隔符的多人串
只在逐段全部命中时才取代原行（R39）；IMDb 人物复用任务 5 的 person_id，否则按任务 5 公式登记（R40）；页面独有导演与 IMDb
导演并存时以 IMDb 为准（R41）；跨版本署名顺序按出现商品数取众数（R42）；starring 只取页面署名行，绝不从 IMDb 生成（R37）。
按 movie_id 分批，结果、实体标记与批次审计同事务提交（R53）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from etl_task5.cluster import person_id_for

from . import sources
from .rules import (CONTROLLED_GENRES, RuleSet, choose_billing_order, classify_genre_term, name_level, parse_name,
                    split_multi_person)
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, add_quality_issue, copy_rows, mark_batch, mark_entities,
                  next_batch_no, pending_entities, require_stage_complete)
from .selection import multi_selection

STAGE = "movie_multivalue"
RESULT_COLUMNS = ("extraction_id", "stage_key", "movie_id", "cast_coverage", "genre_count", "director_count",
                  "starring_count", "cast_count", "field_selections", "run_id")
GENRE_COLUMNS = ("extraction_id", "stage_key", "movie_id", "genre", "source_coverage", "raw_terms", "source_ref",
                 "run_id")
TERM_COLUMNS = ("extraction_id", "stage_key", "movie_id", "section", "term", "disposition", "mapped_genres", "asins",
                "run_id")
ALIGN_COLUMNS = ("extraction_id", "stage_key", "movie_id", "role", "page_person_id", "raw_name", "segment",
                 "segment_name", "resolved_person_id", "nconst", "level", "outcome", "run_id")
PERSON_COLUMNS = ("extraction_id", "stage_key", "movie_id", "person_id", "role", "source_coverage", "status",
                  "credit_order", "imdb_ordering", "nconst", "raw_names", "resolution_note", "run_id")
EXT_COLUMNS = ("extraction_id", "imdb_snapshot_id", "person_id", "nconst", "primary_name", "birth_year", "death_year",
               "in_task5_identity", "run_id")


# ---------------------------------------------------------------- 署名对齐（R38）

@dataclass(frozen=True)
class Alignment:
    outcome: str                              # aligned | tie_not_aligned | no_match
    level: str | None = None
    nconst: str | None = None                 # 唯一命中的 IMDb 人物
    tied: tuple[str, ...] = ()                # 并列最高等级的 nconst


def align_name(names: Sequence[str], candidates: Sequence[dict[str, Any]], levels: Sequence[str],
               relax: bool = False) -> Alignment:
    """页面一个人物（全部页面写法）与同作品同角色 IMDb 候选的对齐（R38）：每个候选取各写法中最严格的兼容等级，
    等级序 L1 > L2 > L3 > LI > L4（t7r2 末尾加 LS）；取唯一最高等级的命中；同等级多命中不对齐（tie_not_aligned）。
    John Cusack 同时近似 Joan Cusack 时严格命中胜出。relax（t7r2）：比较前预规范化格式，并认可 LS。"""
    parsed = [parse_name(n, relax) for n in names if n]
    rank = {lv: k for k, lv in enumerate(levels)}
    hits: list[tuple[int, str, str]] = []             # (等级序, 等级, nconst)
    for c in candidates:
        if not c.get("name"):
            continue
        cp = parse_name(c["name"], relax)
        best = None
        for p in parsed:
            lv = name_level(p, cp, relax)
            if lv is not None and (best is None or rank.get(lv, len(rank)) < rank.get(best, len(rank))):
                best = lv
        if best is not None:
            hits.append((rank.get(best, len(rank)), best, c["nconst"]))
    if not hits:
        return Alignment("no_match")
    top = min(h[0] for h in hits)
    tops = [h for h in hits if h[0] == top]
    if len(tops) > 1:
        return Alignment("tie_not_aligned", tops[0][1], None, tuple(sorted(h[2] for h in tops)))
    return Alignment("aligned", tops[0][1], tops[0][2])


# ---------------------------------------------------------------- 类型（R34–R36）

def _genre_part(rules: RuleSet, terms: list[dict[str, Any]], imdb_genres: list[str] | None,
                imdb_base: float | None = None) -> tuple[list[tuple], list[tuple], list[dict[str, Any]], dict[str, Any]]:
    """返回 (词面行, 成员行, 未映射词面, 选择记录)。imdb_genres 为 None 表示没有可用（未关联或已熔断）的 IMDb。"""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    page: dict[str, dict[str, set]] = {}
    for t in terms:
        term, disposition, mapped = classify_genre_term(t["term"])
        if not term:
            continue
        r = rows.setdefault((t["section"], term.casefold()), {"term": term, "disposition": disposition,
                                                              "mapped": mapped, "asins": set()})
        r["asins"].add(t["asin"])
        for g in mapped:
            e = page.setdefault(g, {"terms": set(), "asins": set(), "sections": set()})
            e["terms"].add(term)
            e["asins"].add(t["asin"])
            e["sections"].add(t["section"])
    imdb_only_terms = []
    imdb_set: set[str] = set()
    for g in imdb_genres or ():
        if g in CONTROLLED_GENRES:
            imdb_set.add(g)
        else:
            imdb_only_terms.append({"section": "imdb", "term": g})
    members = []
    for g in sorted(set(page) | imdb_set):
        cov = "both" if g in page and g in imdb_set else ("imdb_only" if g in imdb_set else "page_only")
        p = page.get(g, {"terms": set(), "asins": set(), "sections": set()})
        members.append((g, cov, sorted(p["terms"]), {"asins": sorted(p["asins"]), "sections": sorted(p["sections"])}))
    unmapped = [{"section": s, "term": r["term"], "asins": sorted(r["asins"])}
                for (s, _k), r in sorted(rows.items()) if r["disposition"] == "unmapped"] + imdb_only_terms
    term_rows = [(s, r["term"], r["disposition"], list(r["mapped"]), sorted(r["asins"]))
                 for (s, _k), r in sorted(rows.items())]
    if not members:
        sel = multi_selection("missing", None, "none", "G0", "没有可映射的类型词面，也没有可用的 IMDb 类型", 0,
                              {"terms": len(term_rows), "unmapped": len(unmapped)})
        return term_rows, members, unmapped, sel
    page_set = set(page)
    complementary = bool(page_set) and bool(imdb_set) and page_set != imdb_set
    conf = rules.conf_union if page_set else rules.conf_imdb_only
    if imdb_set and imdb_base is not None:
        conf = min(conf, imdb_base)
    sel = multi_selection(
        "resolved", "|".join(m[0] for m in members), "complementary_union" if complementary else "none",
        "G1" if imdb_set else "G2",
        "页面映射结果与未熔断 IMDb 类型取受控词并集，集合差异属互补描述" if imdb_set
        else "没有可用的 IMDb 类型（未关联或已熔断），只取页面映射结果", conf,
        {"both": sum(m[1] == "both" for m in members), "imdb_only": sum(m[1] == "imdb_only" for m in members),
         "page_only": sum(m[1] == "page_only" for m in members), "terms": len(term_rows),
         "unmapped": len(unmapped)})
    return term_rows, members, unmapped, sel


# ---------------------------------------------------------------- 演职员（R37–R43）

@dataclass
class Member:
    person_id: str
    role: str
    page: bool = False
    imdb: bool = False
    nconst: str | None = None
    imdb_ordering: int | None = None
    raw_names: list[str] = field(default_factory=list)
    page_person_ids: list[str] = field(default_factory=list)
    sources: list[tuple[str, int | None]] = field(default_factory=list)
    task5_orders: list[int] = field(default_factory=list)
    status: str = "adopted"
    level: str | None = None
    pending_reason: str | None = None
    credit_order: int | None = None
    note: dict[str, Any] = field(default_factory=dict)

    @property
    def coverage(self) -> str:
        return "both" if self.page and self.imdb else ("imdb_only" if self.imdb else "page_only")


@dataclass
class Outcome:
    """一个页面人物在某个对齐范围（director 或 cast）里的处理结果。"""
    kind: str                                  # linked | aligned | tie | nomatch | unsplit
    targets: list[dict[str, Any]] = field(default_factory=list)    # aligned：每段对齐到的 IMDb 人物
    rows: list[tuple] = field(default_factory=list)                # credit_alignment 行（不含运行列）
    tied: tuple[str, ...] = ()
    level: str | None = None


def _analyze_person(p: dict[str, Any], scope: str, cands: dict[str, dict[str, Any]], idmap: dict[str, str],
                    rules: RuleSet) -> Outcome:
    """R38、R39：一个页面人物。cands 为尚未被 nconst 关联占用的 IMDb 候选（nconst → 候选），空表示不可对齐。"""
    if p["nconst"]:
        return Outcome("linked")
    segments = split_multi_person(p["name"])
    pool = list(cands.values())
    if len(segments) == 1:
        if not pool:
            return Outcome("nomatch")
        a = align_name(p["names"], pool, rules.align_levels, rules.name_relax)
        row = (scope, p["person_id"], p["name"], 0, p["name"],
               idmap.get(a.nconst) if a.nconst else None, a.nconst, a.level, a.outcome)
        if a.outcome == "aligned":
            c = cands[a.nconst]
            return Outcome("aligned", [{"nconst": a.nconst, "person_id": idmap[a.nconst], "level": a.level,
                                        "ordering": c["ordinal"], "name": p["name"]}], [row], level=a.level)
        return Outcome("tie" if a.outcome == "tie_not_aligned" else "nomatch", rows=[row], tied=a.tied, level=a.level)
    # 多人串：只在有可对齐候选时逐段尝试，全部成段命中才取代原行（R39）
    results = [align_name([seg], pool, rules.align_levels, rules.name_relax) if pool else Alignment("no_match") for seg in segments]
    ok = bool(pool) and all(r.outcome == "aligned" for r in results) and len({r.nconst for r in results}) == len(results)
    rows, targets = [], []
    for n, (seg, r) in enumerate(zip(segments, results)):
        if ok:
            c = cands[r.nconst]
            targets.append({"nconst": r.nconst, "person_id": idmap[r.nconst], "level": r.level,
                            "ordering": c["ordinal"], "name": seg})
            rows.append((scope, p["person_id"], p["name"], n, seg, idmap[r.nconst], r.nconst, r.level, "aligned"))
        else:
            rows.append((scope, p["person_id"], p["name"], n, seg, None, r.nconst, r.level, "unsplit_multi_person"))
    return Outcome("aligned" if ok else "unsplit", targets, rows)


def _order_of(m: Member, edition_asins: set[str]) -> tuple[int | None, dict[str, Any]]:
    """R42：同人同角色跨版本署名顺序取出现商品数最多的序号，平票取最小序号；只统计该作品的 edition 商品。
    没有 edition 商品的署名行序号时回退任务 5 的署名顺序。"""
    per_asin: dict[str, int] = {}
    for asin, rank in m.sources:
        if rank is not None and asin in edition_asins:
            per_asin[asin] = min(rank, per_asin.get(asin, rank))
    if per_asin:
        order, dist = choose_billing_order(per_asin.values())
        note: dict[str, Any] = {"order_source": "edition_byline", "billing": {str(k): v for k, v in dist.items()}}
        if len(dist) > 1:
            note["billing_conflict"] = True
        return order, note
    if m.task5_orders:
        return min(m.task5_orders), {"order_source": "task5"}
    return None, {}


def _confidence(adopted: list[Member], link: dict[str, Any] | None, rules: RuleSet) -> float:
    conf = rules.conf_imdb_only if adopted and all(m.coverage == "imdb_only" for m in adopted) else rules.conf_union
    if link is not None and any(m.imdb for m in adopted):
        conf = min(conf, link["base_reliability"])
    return round(conf, 2)


def _person_field(role: str, members: list[Member], link: dict[str, Any] | None, rules: RuleSet,
                  disagreement: dict[str, Any] | None) -> dict[str, Any]:
    adopted = [m for m in members if m.status == "adopted"]
    pending = [m for m in members if m.status != "adopted"]
    if not members:
        return multi_selection("missing", None, "none", "P0", f"没有{role}成员", 0)
    detail = {"adopted": len(adopted), "conflict_pending": len(pending),
              "page_only": sum(m.coverage == "page_only" for m in members),
              "imdb_only": sum(m.coverage == "imdb_only" for m in members),
              "both": sum(m.coverage == "both" for m in members)}
    if not adopted:
        return multi_selection("conflict_unresolved", None, "intra_source_tie_unresolved", "P4",
                               "全部成员都无法可靠采用（多人串未能逐段对齐或同等级并列），保留冲突", 0, detail)
    ordered = sorted(adopted, key=lambda m: (m.credit_order is None, m.credit_order or 0, m.person_id))
    value = "|".join(m.person_id for m in ordered)
    if disagreement:
        ctype, rule = "director_source_disagreement", "P3"
        reason = "页面独有导演与 IMDb 导演并存：IMDb 导演标 adopted，页面独有导演标 conflict_pending"
        detail["disagreement"] = disagreement
    elif any(m.note.get("billing_conflict") for m in members):
        ctype, rule, reason = "billing_conflict_resolved", "P2", "跨版本署名顺序不一致，取出现商品数最多的序号，平票取最小序号"
    elif any(m.coverage == "imdb_only" for m in adopted) and any(m.page for m in adopted):
        ctype, rule, reason = "complementary_union", "P1", "页面与 IMDb 成员取可信并集，IMDb 独有成员排在页面成员之后"
    elif any(m.level for m in adopted):
        ctype, rule, reason = "formatting_or_alias", "P1", "页面署名与 IMDb 人物经同片署名对齐记为同一人"
    else:
        ctype, rule, reason = "none", "P1", "页面成员采用" if role == "starring" else "成员来源一致"
    return multi_selection("resolved", value, ctype, rule, reason, _confidence(adopted, link, rules), detail)


def resolve_credits(movie: str, page: dict[str, list[dict[str, Any]]], imdb_dir: list[dict[str, Any]] | None,
                    imdb_cast: list[dict[str, Any]] | None, idmap: dict[str, dict[str, Any]],
                    edition_asins: set[str], link: dict[str, Any] | None, rules: RuleSet
                    ) -> dict[str, Any]:
    """一部作品的演职员消解（纯计算）。imdb_dir、imdb_cast 为 None 表示没有可用的 IMDb 关联（未关联或已熔断）。
    idmap：nconst → {person_id, origin}（任务 5 已有或按公式登记）。返回成员、对齐行、质量问题与四个选择记录。"""
    usable = imdb_dir is not None
    pid_of = {n: v["person_id"] for n, v in idmap.items()}
    result: dict[str, Any] = {"members": {}, "align": [], "issues": [], "selections": {}}
    scope_outcomes: dict[str, dict[str, Outcome]] = {}
    scope_cands: dict[str, dict[str, dict[str, Any]]] = {}
    for scope, imdb_list, role_pages in (("director", imdb_dir, page.get("director", [])),
                                         ("cast", imdb_cast, page.get("cast", []) + page.get("starring", []))):
        by_nconst: dict[str, dict[str, Any]] = {}
        for c in imdb_list or ():
            by_nconst.setdefault(c["nconst"], c)
        consumed = {p["nconst"] for p in role_pages if p["nconst"]}
        cands = {n: c for n, c in by_nconst.items() if n not in consumed}
        outs: dict[str, Outcome] = {}
        for p in role_pages:
            if p["person_id"] not in outs:
                outs[p["person_id"]] = _analyze_person(p, scope, cands, pid_of, rules)
        scope_outcomes[scope], scope_cands[scope] = outs, by_nconst
        for o in outs.values():
            result["align"] += o.rows
    for role in ("director", "starring", "cast"):
        scope = "director" if role == "director" else "cast"
        by_nconst = scope_cands[scope]
        outs = scope_outcomes[scope]
        members: dict[str, Member] = {}

        def member(pid: str) -> Member:
            return members.setdefault(pid, Member(pid, role))

        for p in page.get(role, []):
            o = outs[p["person_id"]]
            if o.kind == "linked":
                m = member(p["person_id"])
                m.nconst = p["nconst"]
                if role != "starring" and p["nconst"] in by_nconst:
                    m.imdb, m.imdb_ordering = True, by_nconst[p["nconst"]]["ordinal"]
                targets = [m]
            elif o.kind == "aligned":
                targets = []
                for t in o.targets:
                    m = member(t["person_id"])
                    m.nconst, m.level = t["nconst"], t["level"]
                    if role != "starring":
                        m.imdb, m.imdb_ordering = True, t["ordering"]
                    m.raw_names.append(t["name"])
                    targets.append(m)
            else:
                m = member(p["person_id"])
                targets = [m]
                if o.kind == "tie":
                    m.status, m.pending_reason = "conflict_pending", "tie_not_aligned"
                    m.note.update(alignment={"outcome": "tie_not_aligned", "level": o.level, "tied": list(o.tied)})
                elif o.kind == "unsplit":
                    m.status, m.pending_reason = "conflict_pending", "unsplit_multi_person"
                    m.note.update(alignment={"outcome": "unsplit_multi_person",
                                             "segments": [r[4] for r in o.rows],
                                             "aligned_segments": sum(r[6] is not None for r in o.rows)})
                    result["issues"].append(("unsplit_multi_person", {"role": role, "person_id": p["person_id"],
                                                                     "raw_name": p["name"],
                                                                     "segments": [r[4] for r in o.rows],
                                                                     "imdb_available": bool(by_nconst)}))
            for m in targets:
                m.page = True
                if p["name"] not in m.raw_names and o.kind != "aligned":
                    m.raw_names.append(p["name"])
                m.page_person_ids.append(p["person_id"])
                m.sources += p["sources"]
                if p["credit_order"] is not None:
                    m.task5_orders.append(p["credit_order"])
        if role != "starring":
            used = {m.nconst for m in members.values() if m.imdb and m.nconst}
            for n, c in by_nconst.items():
                if n in used:
                    continue
                m = member(pid_of[n])
                m.imdb, m.nconst, m.imdb_ordering = True, n, c["ordinal"]
                if c.get("name") and c["name"] not in m.raw_names:
                    m.raw_names.append(c["name"])
        # 署名顺序（R42）
        for m in members.values():
            m.credit_order, note = _order_of(m, edition_asins)
            m.note.update(note)
        page_orders = [m.credit_order for m in members.values() if m.page and m.credit_order is not None]
        nxt = max(page_orders, default=0)
        for m in sorted((x for x in members.values() if not x.page), key=lambda x: (x.imdb_ordering or 0, x.person_id)):
            nxt += 1
            m.credit_order = nxt
            m.note["order_source"] = "imdb_after_page"
        disagreement = None
        if role == "director":
            imdb_members = [m for m in members.values() if m.imdb]
            page_only = [m for m in members.values() if m.coverage == "page_only" and m.status == "adopted"]
            if usable and imdb_members and page_only:
                for m in page_only:
                    m.status, m.pending_reason = "conflict_pending", "director_source_disagreement"
                disagreement = {"page_only": [m.person_id for m in page_only],
                                "imdb": [m.person_id for m in imdb_members]}
                result["issues"].append(("director_source_disagreement", disagreement))
        result["members"][role] = sorted(members.values(), key=lambda m: (m.credit_order is None, m.credit_order or 0, m.person_id))
        result["selections"][role] = _person_field(role, list(members.values()), link, rules, disagreement)
    return result


def cast_coverage(page_cast: bool, imdb_cast: bool) -> str:
    """R43：cast 的覆盖范围。IMDb 一侧是 title_principal 的主要演员，不是完整演员表。"""
    if page_cast and imdb_cast:
        return "page_and_imdb_principals"
    return "page_only" if page_cast else ("imdb_principals_only" if imdb_cast else "none")


# ---------------------------------------------------------------- 阶段入口

def resolve_movies_multivalue(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *,
                              batch_size: int = DEFAULT_BATCH, before_commit: BeforeCommit | None = None
                              ) -> dict[str, Any]:
    """R34–R43、R53：作品层 4 个多值字段，按 movie_id 分批。返回本次写入的计数。"""
    i = ctx.inputs
    key = i.stage_key(STAGE)
    movies = sources.movie_ids(conn, i.t4_mapping_version_id)
    require_stage_complete(conn, ctx, "link_assess", movies)
    todo = pending_entities(conn, ctx, STAGE, movies)
    counts: dict[str, Any] = {"movies": 0, "genres": 0, "genre_terms": {}, "alignment": {}, "people": {}, "ext_people": 0,
                              "status": {}, "conflict_types": {}, "cast_coverage": {}}
    batch_no = next_batch_no(conn, ctx, STAGE)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        links = sources.product_links(conn, i.t4_mapping_version_id, batch)
        lrows = sources.link_rows(conn, i, batch)
        usable = {m: r for m, r in lrows.items() if not r["cross_source_mismatch_halt"]}
        tconsts = [r["effective_tconst"] for r in usable.values()]
        imdb = sources.imdb_titles(conn, i.imdb_snapshot_id, tconsts)
        dirs = sources.imdb_directors(conn, i.imdb_snapshot_id, tconsts)
        principals = sources.imdb_principals(conn, i.imdb_snapshot_id, tconsts)
        credits = sources.page_credits(conn, i, batch)
        terms = sources.genre_terms(conn, i, links)
        nconsts = sorted({d["nconst"] for t in tconsts for d in (*dirs.get(t, ()), *principals.get(t, ()))})
        known = sources.imdb_person_ids(conn, i, nconsts)
        idmap = {n: known.get(n) or {"person_id": person_id_for(f"imdb:{n}"), "origin": "computed"} for n in nconsts}
        details = sources.imdb_names(conn, i.imdb_snapshot_id, nconsts)
        with conn.transaction():
            genre_rows, term_rows, align_rows, person_rows, result_rows, ext = [], [], [], [], [], {}
            entities: list[tuple[str, int]] = []
            for m in batch:
                link = lrows.get(m)
                use = usable.get(m)
                tc = use["effective_tconst"] if use else None
                title = imdb.get(tc) if tc else None
                good = use is not None and title is not None
                sels: dict[str, dict[str, Any]] = {}
                t_rows, g_members, unmapped, sels["genres"] = _genre_part(
                    rules, terms.get(m, []), title["genres"] if good else None, use["base_reliability"] if good else None)
                for section, term, disposition, mapped, asins in t_rows:
                    term_rows.append((i.extraction_id, key, m, section, term, disposition, mapped, asins, ctx.run_id))
                for g, cov, raw_terms, ref in g_members:
                    genre_rows.append((i.extraction_id, key, m, g, cov, raw_terms,
                                       Jsonb({**ref, **({"tconst": tc} if good and cov != "page_only" else {})}),
                                       ctx.run_id))
                for u in unmapped:
                    add_quality_issue(conn, ctx, STAGE, m, "unmapped_genre_term", u)
                edition_asins = {r["asin"] for r in links.get(m, ()) if r["relation_type"] == "edition"}
                cr = resolve_credits(m, credits.get(m, {}), dirs.get(tc, []) if good else None,
                                     principals.get(tc, []) if good else None, idmap, edition_asins, link if good else None,
                                     rules)
                sels.update(cr["selections"])
                for row in cr["align"]:
                    scope, ppid, raw, seg, seg_name, rpid, nconst, lv, outcome = row
                    align_rows.append((i.extraction_id, key, m, scope, ppid, raw, seg, seg_name, rpid, nconst, lv,
                                       outcome, ctx.run_id))
                for code, detail in cr["issues"]:
                    add_quality_issue(conn, ctx, STAGE, m, code, detail)
                n_people = {}
                for role, members in cr["members"].items():
                    n_people[role] = sum(x.status == "adopted" for x in members)
                    for x in members:
                        person_rows.append((i.extraction_id, key, m, x.person_id, role, x.coverage, x.status,
                                            x.credit_order, x.imdb_ordering, x.nconst, x.raw_names,
                                            Jsonb({**x.note, "page_person_ids": sorted(set(x.page_person_ids)),
                                                   **({"alignment_level": x.level} if x.level else {}),
                                                   **({"pending_reason": x.pending_reason} if x.pending_reason else {})}),
                                            ctx.run_id))
                        if x.imdb and x.nconst and idmap.get(x.nconst, {}).get("origin") != "task5_external":
                            d = details.get(x.nconst, {})
                            ext[x.nconst] = (i.extraction_id, i.imdb_snapshot_id, x.person_id, x.nconst,
                                             d.get("primary_name"), d.get("birth_year"), d.get("death_year"),
                                             idmap[x.nconst]["origin"] == "task5_identity", ctx.run_id)
                page_cast = bool(credits.get(m, {}).get("cast") or credits.get(m, {}).get("starring"))
                coverage = cast_coverage(page_cast, bool(good and principals.get(tc)))
                result_rows.append((i.extraction_id, key, m, coverage, len(g_members), n_people["director"],
                                    n_people["starring"], n_people["cast"], Jsonb(sels), ctx.run_id))
                entities.append((m, len(g_members) + len(t_rows) + len(cr["align"]) + sum(len(v) for v in cr["members"].values()) + 1))
                counts["cast_coverage"][coverage] = counts["cast_coverage"].get(coverage, 0) + 1
                for name, s in sels.items():
                    counts["status"][f"{name}:{s['status']}"] = counts["status"].get(f"{name}:{s['status']}", 0) + 1
                    counts["conflict_types"][s["conflict_type"]] = counts["conflict_types"].get(s["conflict_type"], 0) + 1
                for _t in t_rows:
                    counts["genre_terms"][_t[2]] = counts["genre_terms"].get(_t[2], 0) + 1
                for row in cr["align"]:
                    counts["alignment"][f"{row[0]}:{row[8]}:{row[7] or '-'}"] = \
                        counts["alignment"].get(f"{row[0]}:{row[8]}:{row[7] or '-'}", 0) + 1
                counts["genres"] += len(g_members)
                for role, members in cr["members"].items():
                    for x in members:
                        k = f"{role}:{x.coverage}:{x.status}"
                        counts["people"][k] = counts["people"].get(k, 0) + 1
            copy_rows(conn, "task7.movie_genre", GENRE_COLUMNS, genre_rows)
            copy_rows(conn, "task7.movie_genre_term", TERM_COLUMNS, term_rows)
            copy_rows(conn, "task7.credit_alignment", ALIGN_COLUMNS, align_rows)
            copy_rows(conn, "task7.movie_person_resolved", PERSON_COLUMNS, person_rows)
            if ext:
                with conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO task7.imdb_person_ext ({', '.join(EXT_COLUMNS)}) VALUES "
                        f"({', '.join(['%s'] * len(EXT_COLUMNS))}) ON CONFLICT DO NOTHING", list(ext.values()))
                    counts["ext_people"] += cur.rowcount        # 已由前面批次或前一版本登记的人物不重复计数
            copy_rows(conn, "task7.movie_multivalue_stage_result", RESULT_COLUMNS, result_rows)
            mark_entities(conn, ctx, STAGE, batch_no, entities)
            mark_batch(conn, ctx, STAGE, batch_no, batch[0], batch[-1],
                       len(genre_rows) + len(term_rows) + len(align_rows) + len(person_rows) + len(result_rows))
            if before_commit is not None:
                before_commit(STAGE, batch_no)
        counts["movies"] += len(batch)
        batch_no += 1
    return counts
