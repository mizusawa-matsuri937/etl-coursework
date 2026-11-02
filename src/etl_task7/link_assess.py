"""关联评估（link_assess，spec R14–R19）：字段消解前先判断每部作品的 IMDb 关联是否可信。

IMDb 值的权威性建立在“这确实是同一部片”之上，所以评估排在字段消解之前并独立持久化：movie_scalar 与 movie_multivalue
读取的是同一条判定（AC21）。有效 tconst 取任务 6 采用候选的三类来源（R14）；每部有效关联作品计算四个信号：
年份 Y、片长 RT、导演 D、标题 T（R15）；导演比较复用任务 5 的姓名解析与兼容等级（R16）；弱匹配满足熔断条件时
cross_source_mismatch_halt = true（R17），单信号只写质量问题不熔断（R18）。共享 tconst 在全部作品评估后，按
(tconst, movie_id) 去重逐对分类（R19），只记录，不回写任务 4。按 movie_id 分批，分类步骤在最后一个事务里完成。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task4.normalize import title_key
from etl_task5.names import level, parse

from . import sources
from .rules import (WEAK_SOURCES, RuleSet, classify_shared_pair, clean_page_display_title, decide_halt, lit_signals,
                    runtime_signal_lit, split_multi_person, title_signal)
from .run import (DEFAULT_BATCH, SHARED_MARK, BeforeCommit, RunContext, add_quality_issue, copy_rows, mark_batch,
                  mark_entities, next_batch_no, pending_entities, require_stage_complete)

STAGE = "link_assess"
LINK_COLUMNS = ("extraction_id", "stage_key", "movie_id", "effective_tconst", "tconst_source", "base_reliability",
                "is_weak", "sig_year", "sig_runtime", "sig_director", "sig_title", "cross_source_mismatch_halt",
                "halt_reason", "signal_detail", "run_id")
SHARED_COLUMNS = ("extraction_id", "stage_key", "tconst", "movie_id", "other_movie_id", "link_class", "basis",
                  "source_issue_ids", "run_id")
IMDB_SOURCES = ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur")
KEYS_SHOWN = 40                       # signal_detail 里最多列出的 IMDb 标题键（别名很多的作品只列前若干条）


def year_signal(imdb_year: int, title_years: list[int], bound_years: list[int], rules: RuleSet
                ) -> tuple[str, dict[str, Any]]:
    """Y：页面标题年份与 IMDb 年份差 ≥ year_gap，或评论、发售日证据年早于 IMDb 年 ≥ year_gap（含被任务 6 条件拦下的）。"""
    lit_t = sorted({y for y in title_years if abs(y - imdb_year) >= rules.year_gap})
    lit_b = sorted({y for y in bound_years if y <= imdb_year - rules.year_gap})
    state = "bad" if lit_t or lit_b else ("ok" if title_years or bound_years else "na")
    return state, {"imdb_year": imdb_year, "title_years": sorted(set(title_years)),
                   "bound_years": sorted(set(bound_years)), "lit_title_years": lit_t, "lit_bound_years": lit_b}


def runtime_signal(imdb_runtime: int | None, editions: list[dict[str, Any]], rules: RuleSet
                   ) -> tuple[str, dict[str, Any]]:
    """RT：存在 edition 商品与 IMDb 片长差 > runtime_gap 分钟，且无剪辑版标记、非多碟花絮版。"""
    rows, lit = [], False
    for e in editions:
        if imdb_runtime is None or e["runtime_minutes"] is None:
            continue
        on = runtime_signal_lit(e["runtime_minutes"], imdb_runtime, cut_types=e["cut_type"], discs=e["discs"],
                                rules=rules)
        lit = lit or on
        rows.append({"asin": e["asin"], "page": e["runtime_minutes"], "delta": e["runtime_minutes"] - imdb_runtime,
                     "cut_type": list(e["cut_type"]), "discs": e["discs"], "lit": on})
    state = "bad" if lit else ("ok" if rows else "na")
    return state, {"imdb_runtime": imdb_runtime, "editions": rows}


def director_signal(page: list[dict[str, Any]], imdb: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """D：双方都有导演且无一人兼容。先按 nconst，再把页面署名按 ;、and、& 拆段后用任务 5 的姓名兼容等级逐段比较（R16）；
    只比 nconst 会把大量页面人物没有 nconst 的弱匹配作品误判为导演不同。"""
    parsed_imdb = [(d, parse(d["name"])) for d in imdb if d.get("name")]
    usable: list[dict[str, Any]] = []
    for p in page:
        segs = [(n, seg) for n in p["names"] for seg in split_multi_person(n)]
        if p.get("nconst") or any(not parse(seg).placeholder for _, seg in segs):
            usable.append({**p, "segments": segs})
    detail: dict[str, Any] = {"page": [{"person_id": p["person_id"], "names": p["names"], "nconst": p.get("nconst")}
                                       for p in usable],
                              "imdb": [{"nconst": d["nconst"], "name": d.get("name")} for d in imdb], "matches": []}
    if not usable or not imdb:
        return "na", detail
    imdb_nconsts = {d["nconst"] for d in imdb}
    for p in usable:
        if p.get("nconst") in imdb_nconsts:
            detail["matches"].append({"page_person_id": p["person_id"], "via": "nconst", "nconst": p["nconst"]})
        for _name, seg in p["segments"]:
            ps = parse(seg)
            for d, pd in parsed_imdb:
                lv = level(ps, pd)
                if lv:
                    detail["matches"].append({"page_person_id": p["person_id"], "via": "name", "segment": seg,
                                              "nconst": d["nconst"], "level": lv})
    return ("ok" if detail["matches"] else "bad"), detail


def assess_links(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *, batch_size: int = DEFAULT_BATCH,
                 before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R14–R19：按 movie_id 分批写 link_assessment；全部批次完成后在一个事务里写 shared_tconst_result。"""
    i = ctx.inputs
    key = i.stage_key(STAGE)
    require_stage_complete(conn, ctx, "edition_resolve", [a for a, _ in sources.matched_editions(conn, i)])
    movies = sources.movie_ids(conn, i.t4_mapping_version_id)
    todo = pending_entities(conn, ctx, STAGE, movies)
    counts: dict[str, Any] = {"movies": 0, "assessed": 0, "halted": 0, "weak": 0, "single_signal": 0,
                              "signals": {}, "shared": {}}
    batch_no = next_batch_no(conn, ctx, STAGE)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        sel = sources.release_selections(conn, i.t6_release_version_id, batch)
        cand_ids = [c for s in sel.values() for c in s["considered"]]
        cands = sources.release_candidates(conn, cand_ids)
        links = sources.product_links(conn, i.t4_mapping_version_id, batch)
        effective: dict[str, dict[str, Any]] = {}
        for m in batch:
            s = sel.get(m)
            c = cands.get(s["selected_candidate_id"]) if s and s["selected_candidate_id"] else None
            if c and c["source"] in IMDB_SOURCES:
                effective[m] = c
        tconsts = [c["source_key"] for c in effective.values()]
        imdb = sources.imdb_titles(conn, i.imdb_snapshot_id, tconsts)
        dirs = sources.imdb_directors(conn, i.imdb_snapshot_id, tconsts)
        keys = sources.imdb_title_keys(conn, i, tconsts)
        bounds = sources.bound_evidence(conn, i.t6_release_version_id, list(effective))
        titles = sources.page_titles(conn, i, {m: links.get(m, []) for m in effective})
        pdirs = sources.page_directors(conn, i, list(effective))
        edition_asins = sorted({r["asin"] for m in effective for r in links.get(m, ())
                                if r["relation_type"] == "edition"})
        ed = sources.edition_results(conn, i, edition_asins)
        with conn.transaction():
            rows: list[tuple] = []
            entities: list[tuple[str, int]] = []
            for m in batch:
                c = effective.get(m)
                if c is None:
                    entities.append((m, 0))
                    continue
                tconst, source = c["source_key"], c["source"]
                title = imdb.get(tconst)
                if title is None or title["start_year"] is None:
                    entities.append((m, 0))
                    continue
                weak = source in WEAK_SOURCES
                t_years = [x["value_year"] for x in (cands.get(cid) for cid in sel[m]["considered"])
                           if x and x["source"] == "amazon_title_year"]
                y_state, y_detail = year_signal(title["start_year"], t_years,
                                                [b["evidence_time"].year for b in bounds.get(m, ())], rules)
                eds = [{"asin": r["asin"], **ed[r["asin"]]} for r in links.get(m, ())
                       if r["relation_type"] == "edition" and r["asin"] in ed]
                rt_state, rt_detail = runtime_signal(title["runtime_minutes"], eds, rules)
                d_state, d_detail = director_signal(pdirs.get(m, []), dirs.get(tconst, []))
                page_keys = sorted({title_key(clean_page_display_title(a["raw_value"] or ""))
                                    for a in titles.get(m, ()) if a["raw_value"]})
                t_state = title_signal(page_keys, keys.get(tconst, []), rules.subset_min_words)
                halted, reason = decide_halt(weak, y_state == "bad", rt_state == "bad", d_state == "bad",
                                             t_state == "bad")
                lit = lit_signals(y_state, rt_state, d_state, t_state)
                detail = {"year": y_detail, "runtime": rt_detail, "director": d_detail,
                          "title": {"page_keys": page_keys, "imdb_keys": keys.get(tconst, [])[:KEYS_SHOWN],
                                    "imdb_key_count": len(keys.get(tconst, []))},
                          "lit": lit, "task6_source": source, "task6_candidate_id": c["candidate_id"]}
                rows.append((i.extraction_id, key, m, tconst, source, rules.reliability(source), weak, y_state,
                             rt_state, d_state, t_state, halted, reason, Jsonb(detail), ctx.run_id))
                entities.append((m, 1))
                counts["assessed"] += 1
                counts["weak"] += weak
                for name in lit:
                    counts["signals"][name] = counts["signals"].get(name, 0) + 1
                if halted:
                    counts["halted"] += 1
                    add_quality_issue(conn, ctx, STAGE, m, "cross_source_mismatch_halt",
                                      {"tconst": tconst, "source": source, "signals": lit, "halt_reason": reason})
                elif weak and len(lit) == 1:
                    counts["single_signal"] += 1
                    add_quality_issue(conn, ctx, STAGE, m, "weak_link_single_signal",
                                      {"tconst": tconst, "source": source, "signal": lit[0]})
            copy_rows(conn, "task7.link_assessment", LINK_COLUMNS, rows)
            mark_entities(conn, ctx, STAGE, batch_no, entities)
            mark_batch(conn, ctx, STAGE, batch_no, batch[0], batch[-1], len(rows))
            if before_commit is not None:
                before_commit(STAGE, batch_no)
        counts["movies"] += len(batch)
        batch_no += 1
    counts["shared"] = classify_shared_tconst(conn, ctx, rules, batch_no)
    return counts


def classify_shared_tconst(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, batch_no: int
                           ) -> dict[str, int]:
    """R19：任务 6 的 shared_tconst_with_movie 质量问题按 (tconst, movie_id) 去重后逐对分类。
    已熔断为 false_positive_link；未熔断且两边标题相等或子集、导演不矛盾为 likely_duplicate_movie；其余 undetermined。
    只记录，不回写任务 4：likely_duplicate_movie 在目标层仍是两个 movie_id（任务 8 统计须知悉）。"""
    i = ctx.inputs
    key = i.stage_key(STAGE)
    done = {r[0] for r in conn.execute("SELECT entity_id FROM task7.entity_stage WHERE extraction_id = %s "
                                       "AND stage = %s AND stage_key = %s AND entity_id = %s",
                                       (i.extraction_id, STAGE, key, SHARED_MARK))}
    if done:
        return {}
    run6 = conn.execute("SELECT run_id FROM task6.release_version WHERE release_version_id = %s",
                        (i.t6_release_version_id,)).fetchone()[0]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for issue_id, movie, detail in conn.execute(
            "SELECT issue_id, entity_id, detail FROM task6.quality_issue WHERE extraction_id = %s AND run_id = %s "
            "AND issue_code = 'shared_tconst_with_movie' ORDER BY issue_id", (i.extraction_id, run6)):
        g = groups.setdefault((detail["tconst"], movie), {"issues": [], "owners": [], "origins": []})
        g["issues"].append(issue_id)
        if detail["owner_movie_id"] not in g["owners"]:
            g["owners"].append(detail["owner_movie_id"])
        origin = detail.get("origin") or "candidate_shared"
        if origin not in g["origins"]:
            g["origins"].append(origin)
    involved = sorted({m for (_t, m) in groups} | {o for g in groups.values() for o in g["owners"]})
    rows_by_movie = sources.link_rows(conn, i, involved)

    def view(movie: str, tconst: str) -> dict[str, Any]:
        r = rows_by_movie.get(movie)
        if r is None or r["effective_tconst"] != tconst:
            return {"movie_id": movie, "assessed": False, "halt": False, "sig_title": "na", "sig_director": "na"}
        return {"movie_id": movie, "assessed": True, "halt": r["cross_source_mismatch_halt"],
                "sig_title": r["sig_title"], "sig_director": r["sig_director"], "tconst_source": r["tconst_source"]}

    rows: list[tuple] = []
    tally: dict[str, int] = {}
    for (tconst, movie), g in sorted(groups.items()):
        other = g["owners"][0]
        a, b = view(movie, tconst), view(other, tconst)
        cls = classify_shared_pair(a["halt"], b["halt"], a["sig_title"], b["sig_title"], a["sig_director"],
                                   b["sig_director"])
        basis = {"movie": a, "other": b, "origins": g["origins"], "owners": g["owners"]}
        rows.append((i.extraction_id, key, tconst, movie, other, cls, Jsonb(basis), g["issues"], ctx.run_id))
        tally[cls] = tally.get(cls, 0) + 1
    with conn.transaction():
        copy_rows(conn, "task7.shared_tconst_result", SHARED_COLUMNS, rows)
        mark_entities(conn, ctx, STAGE, batch_no, [(SHARED_MARK, len(rows))])
        mark_batch(conn, ctx, STAGE, batch_no, SHARED_MARK, SHARED_MARK, len(rows))
    tally["pairs"] = len(rows)
    tally["source_issues"] = sum(len(g["issues"]) for g in groups.values())
    return tally
