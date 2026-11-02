"""作品层标量字段消解（movie_scalar，spec R20–R33）：title、original_title、release_year、runtime_minutes、mpaa_rating。

读 link_assessment 决定是否采用 IMDb：已熔断的关联不采用 IMDb 值（R20–R22、R26、R28）。严格区分 edition 与 contains
候选（R21、R28、R33）：合集盒子的总标题、总片长、分级与类型不下推给成员作品（R05）。推断上界只放独立参考列，
绝不成为 release_year 候选（R27）。采集时间照实保存但不参与决胜（R10）。按 movie_id 分批，候选、阶段结果、逐版本
片长差异、别名、实体标记与批次审计同事务提交（R53）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task4.normalize import title_key

from . import sources
from .rules import (DIFF_CONFLICT_TYPE, PageRuntime, PageTitle, RuleSet, YearInputs, choose_page_title,
                    classify_runtime_diff, clean_page_display_title, decide_movie_rating, decide_release_year,
                    decide_runtime_without_imdb, has_pal, imdb_confidence, lit_signals, title_match_class,
                    worst_diff_class)
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, add_quality_issue, copy_rows, mark_batch, mark_entities,
                  next_batch_no, pending_entities, require_stage_complete)
from .selection import Cand, IdAllocator, new_cand, selection, settle, write_candidates

STAGE = "movie_scalar"
RESULT_COLUMNS = ("extraction_id", "stage_key", "movie_id", "title", "original_title", "release_year",
                  "runtime_minutes", "mpaa_rating", "effective_tconst", "tconst_source", "cross_source_mismatch_halt",
                  "inferred_upper_bound", "inferred_bound_precision", "inferred_bound_evidence", "field_selections",
                  "run_id")
DIFF_COLUMNS = ("extraction_id", "stage_key", "movie_id", "asin", "page_runtime", "imdb_runtime", "delta",
                "diff_class", "pal_formula_match", "has_pal_evidence", "cut_type", "discs", "run_id")
ALIAS_COLUMNS = ("extraction_id", "stage_key", "movie_id", "alias_kind", "alias_ordinal", "alias", "region",
                 "language", "source_ref", "run_id")
IMDB_SOURCES = ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur")
DEFAULT_PAGE_RELIABILITY = 0.90      # 任务 4 自动决策没有置信度记录时的页面匹配可靠度


def page_match_reliability(a: dict[str, Any]) -> float:
    """页面候选的匹配可靠度：singleton 与人工决策为 1.00，自动决策取任务 4 决策置信度。"""
    if a["basis"] in ("singleton", "human"):
        return 1.0
    return a["decision_confidence"] if a["decision_confidence"] is not None else DEFAULT_PAGE_RELIABILITY


def _imdb_cand(alloc, movie, field, link, value, ref, downloaded, precision="exact") -> Cand:
    return new_cand(alloc, "movie", movie, field, link["tconst_source"], link["effective_tconst"], value, value,
                    precision, {"snapshot": link["snapshot"], **ref}, link["base_reliability"], downloaded)


def _edition_cands(conn: psycopg.Connection, i, ids: list[int]) -> dict[int, dict[str, Any]]:
    """版本层候选 ID → 原始值与定位（作品层候选引用它们作来源）。"""
    out: dict[int, dict[str, Any]] = {}
    key = i.stage_key("edition_resolve")
    for chunk in [ids[k:k + 500] for k in range(0, len(ids), 500)]:
        for cid, raw, at, ref in conn.execute(
                "SELECT candidate_id, raw_value, source_acquired_at, source_ref FROM task7.field_candidate "
                "WHERE extraction_id = %s AND stage_key = %s AND candidate_id = ANY(%s)", (i.extraction_id, key, chunk)):
            out[cid] = {"raw_value": raw, "acquired_at": at, "source_ref": ref}
    return out


# ---------------------------------------------------------------- 标题与原名（R20–R23）

def _title(alloc, movie, link, imdb, akas, page, records, downloaded, rules, single_signal):
    """返回 (候选, 选择记录)。"""
    cands: list[Cand] = []
    page_c: list[Cand] = []
    for a in page:
        rec = records.get(a["source_record_id"]) or {}
        clean = clean_page_display_title(a["raw_value"] or "")
        member = "" if a["relation_type"] == "edition" else f"#{a['member_ordinal']}"
        page_c.append(new_cand(
            alloc, "movie", movie, "title", "page_title", f"{a['asin']}{member}:{a['assertion_id']}", a["raw_value"],
            clean, "exact",
            {"asin": a["asin"], "assertion_id": a["assertion_id"], "source_record_id": a["source_record_id"],
             "entity_type": a["entity_type"], "relation_type": a["relation_type"],
             "member_ordinal": a["member_ordinal"], "section": a["section"], "ordinal": a["page_ordinal"],
             "basis": a["basis"], "content_hash": rec.get("content_hash"), "snapshot_path": rec.get("snapshot_path")},
            page_match_reliability(a), rec.get("acquired_at")))
    cands += page_c
    imdb_ok = link is not None and imdb is not None and imdb["primary_title"]
    halted = bool(link and link["cross_source_mismatch_halt"])
    imdb_c = None
    if imdb_ok:
        imdb_c = _imdb_cand(alloc, movie, "title", link, imdb["primary_title"],
                            {"table": "imdb.title", "tconst": link["effective_tconst"],
                             "line_no": imdb["line_no"], "column": "primary_title"}, downloaded)
        cands.append(imdb_c)
    if not cands:
        return cands, selection("missing", None, [], [], "none", "T0", "没有标题候选", 0)
    if imdb_c is not None and not halted:
        title_keys_aka = [a["title"] for a in akas if a["title"]]
        classes = {c.cid: title_match_class(c.normalized_value or "", imdb["primary_title"], imdb["original_title"],
                                            title_keys_aka, rules.subset_min_words) for c in page_c}
        same = [c for c in page_c if classes[c.cid] == "same"]
        alias = [c for c in page_c if classes[c.cid] == "alias"]
        other = [c for c in page_c if classes[c.cid] == "other"]
        settle(page_c, adopted=same, superseded=alias, rejected=other, reason="unrelated_to_imdb_title")
        imdb_c.mark("adopted")
        ctype = "cross_source_override" if other else ("formatting_or_alias" if alias else "none")
        detail = {"page_classes": {c.source_key: classes[c.cid] for c in page_c}} if page_c else {}
        sel = selection("resolved", imdb["primary_title"], [imdb_c], cands, ctype, "T1",
                        "未熔断的 IMDb 关联：采用 primary_title；页面清洁标题相同、别名或无关分别标 adopted、"
                        "superseded_by_specific、rejected",
                        imdb_confidence(link["base_reliability"], rules, single_signal=single_signal,
                                        override=bool(other)), detail)
        return cands, sel
    # 无 IMDb 关联、没有可用 IMDb 标题或已熔断：只用页面候选（R21）
    if imdb_c is not None and page_c:
        imdb_c.mark("rejected", "cross_source_mismatch_halt")
    best, kind = choose_page_title([PageTitle(c.source_ref["asin"], c.normalized_value or "") for c in page_c])
    if best is None:
        if kind == "none":
            return cands, selection("conflict_unresolved", None, [], cands, "cross_source_mismatch_halt", "T4",
                                    "关联已熔断且页面没有标题候选", 0)
        return cands, selection("conflict_unresolved", None, [], cands, "intra_source_tie_unresolved", "T3",
                                "页面候选的规范标题键实质不同，没有可靠依据裁决", 0,
                                {"clean_titles": sorted({c.normalized_value for c in page_c})})
    same = [c for c in page_c if c.normalized_value == best]
    others = [c for c in page_c if c.normalized_value != best]
    settle(page_c, adopted=same, superseded=others, reason="alias_of_selected_title")
    first = min(same, key=lambda c: c.source_ref["asin"])
    ctype = "cross_source_mismatch_halt" if halted else ("formatting_or_alias" if kind == "alias" else "none")
    reason = ("关联已熔断，IMDb 标题候选 rejected，回退到页面清洁标题（频次最高，平票取最小 ASIN）" if halted
              else "无 IMDb 关联：页面规范标题键一致或互为副标题时取频次最高的清洁标题，平票取最小 ASIN")
    return cands, selection("resolved", best, [first], cands, ctype, "T2", reason, rules.conf_page_only,
                            {"page_cluster": kind})


def _original(alloc, movie, link, imdb, downloaded, rules, single_signal):
    if link is None or imdb is None or not imdb["original_title"]:
        return [], selection("missing", None, [], [], "none", "O0", "没有 IMDb 原名候选", 0)
    c = _imdb_cand(alloc, movie, "original_title", link, imdb["original_title"],
                   {"table": "imdb.title", "tconst": link["effective_tconst"], "line_no": imdb["line_no"],
                    "column": "original_title"}, downloaded)
    if link["cross_source_mismatch_halt"]:
        return [c], selection("conflict_unresolved", None, [], [c], "cross_source_mismatch_halt", "O2",
                              "关联已熔断，IMDb 原名不可信", 0)
    c.mark("adopted")
    distinct = (imdb["primary_title"] or "").casefold() != imdb["original_title"].casefold()
    return [c], selection("resolved", imdb["original_title"], [c], [c], "none", "O1",
                          "未熔断的 IMDb 关联：采用 original_title",
                          imdb_confidence(link["base_reliability"], rules, single_signal=single_signal),
                          {"distinct_from_primary": distinct})


def _aliases(movie, link, imdb, akas, page_titles, halted):
    """R23：IMDb primary、original、aka 与页面清洁标题变体；已熔断作品不收录 IMDb 别名。同名别名只保留先出现者。"""
    rows: list[tuple[str, int, str, str | None, str | None, dict[str, Any]]] = []
    seen: set[str] = set()

    def add(kind, ordinal, title, region, language, ref):
        k = (title or "").strip().casefold()
        if k and k not in seen:
            seen.add(k)
            rows.append((kind, ordinal, title.strip(), region, language, ref))

    if link is not None and imdb is not None and not halted:
        tc = link["effective_tconst"]
        add("primary", 1, imdb["primary_title"], None, None, {"tconst": tc, "table": "imdb.title", "line_no": imdb["line_no"]})
        add("original", 1, imdb["original_title"], None, None, {"tconst": tc, "table": "imdb.title", "line_no": imdb["line_no"]})
        for n, a in enumerate(akas, start=1):
            add("aka", n, a["title"], a["region"], a["language"],
                {"tconst": tc, "table": "imdb.title_aka", "ordering": a["ordering"], "line_no": a["line_no"],
                 "is_original": a["is_original"]})
    for n, (clean, ref) in enumerate(page_titles, start=1):
        add("page", n, clean, None, None, ref)
    return rows


# ---------------------------------------------------------------- 上映年份（R24–R27）

def _release_year(alloc, movie, link, imdb, sel6, cands6, close_runtime, rules, single_signal):
    cands: list[Cand] = []
    for cid in sel6["considered"] if sel6 else ():
        c6 = cands6.get(cid)
        if not c6:
            continue
        is_imdb = c6["source"] in IMDB_SOURCES
        reliability = rules.reliability(c6["source"]) if is_imdb else float(c6["confidence"])
        ref = {"task6_candidate_id": c6["candidate_id"], "task6_source": c6["source"], "flags": list(c6["flags"] or []),
               **(c6["source_ref"] or {})}
        cands.append(new_cand(alloc, "movie", movie, "release_year", c6["source"], c6["source_key"], c6["raw_value"],
                              str(c6["value_year"]), "year", ref, reliability, c6["source_acquired_at"]))
    if not cands:
        return cands, selection("missing", None, [], [], "none", "year.table:YR10", "没有任何年份候选", 0,
                                {"task6_missing_reason": (sel6 or {}).get("missing_reason"),
                                 "task6_status": (sel6 or {}).get("status")}), None
    eff = None
    if link is not None:
        eff = next((c for c in cands if c.source == link["tconst_source"] and c.source_key == link["effective_tconst"]), None)
    page_years = tuple(int(c.normalized_value) for c in cands if c.source == "amazon_title_year")
    imdb_year = int(eff.normalized_value) if eff is not None else None
    d = decide_release_year(YearInputs(
        imdb_year=imdb_year, weak=bool(link and link["is_weak"]), halted=bool(link and link["cross_source_mismatch_halt"]),
        page_years=page_years, sig_year=link["sig_year"] if link else "na",
        sig_director=link["sig_director"] if link else "na", sig_runtime=link["sig_runtime"] if link else "na",
        close_runtime=close_runtime))
    detail = {"task6_status": (sel6 or {}).get("status"), "page_years": sorted(set(page_years)),
              "imdb_year": imdb_year, "signals": ({k: link[k] for k in ("sig_year", "sig_runtime", "sig_director",
                                                                         "sig_title")} if link else {})}
    if d.status != "resolved":
        return cands, selection(d.status, None, [], cands, d.conflict_type, f"year.table:{d.rule}", d.reason, 0,
                                detail), d
    won = [c for c in cands if int(c.normalized_value) == d.year]
    rejected = [c for c in cands if int(c.normalized_value) != d.year]
    settle(cands, adopted=won, rejected=rejected, reason=d.conflict_type if d.conflict_type != "none" else "year_differs")
    selected = eff if d.source == "imdb" and eff is not None else won[0]
    conf = (imdb_confidence(link["base_reliability"], rules, single_signal=single_signal,
                            override=d.conflict_type == "cross_source_override")
            if d.source == "imdb" else min(c.reliability for c in won))
    if d.conflict_type == "minor_tolerance":
        conf = min(conf, rules.conf_tolerance)
    return cands, selection("resolved", d.year, [selected], cands, d.conflict_type, f"year.table:{d.rule}", d.reason,
                            conf, detail), d


# ---------------------------------------------------------------- 片长（R28–R32）

def _runtime(alloc, movie, link, imdb, editions, edcands, downloaded, rules, single_signal):
    """返回 (候选, 选择记录, runtime_diff 行, 严重问题)。editions：该作品 edition 商品的版本层阶段结果。"""
    cands: list[Cand] = []
    page_c: list[tuple[Cand, dict[str, Any]]] = []
    for e in editions:
        if e["runtime_minutes"] is None:
            continue
        sel_ids = (e["field_selections"].get("runtime_minutes") or {}).get("selected_candidate_ids") or []
        src = edcands.get(sel_ids[0], {}) if sel_ids else {}
        c = new_cand(alloc, "movie", movie, "runtime_minutes", "page_edition_runtime", e["asin"],
                     src.get("raw_value") or str(e["runtime_minutes"]), str(e["runtime_minutes"]), "minute",
                     {"asin": e["asin"], "edition_candidate_ids": list(sel_ids), "edition_stage": "edition_resolve",
                      "task4_source": (src.get("source_ref") or {})}, 1.0, src.get("acquired_at"))
        page_c.append((c, e))
        cands.append(c)
    imdb_c = None
    if link is not None and imdb is not None and imdb["runtime_minutes"] is not None:
        imdb_c = _imdb_cand(alloc, movie, "runtime_minutes", link, str(imdb["runtime_minutes"]),
                            {"table": "imdb.title", "tconst": link["effective_tconst"], "line_no": imdb["line_no"],
                             "column": "runtime_minutes"}, downloaded, "minute")
        cands.append(imdb_c)
    halted = bool(link and link["cross_source_mismatch_halt"])
    if not cands:
        return cands, selection("missing", None, [], [], "none", "RT0", "没有片长候选（仅经 contains 出现或页面无片长）", 0), [], None
    if halted:
        return cands, selection("conflict_unresolved", None, [], cands, "cross_source_mismatch_halt", "RT2",
                                "关联已熔断，IMDb 片长不可信；各版本片长保留在版本层", 0), [], None
    if imdb_c is not None:
        diffs, issue = [], None
        imdb_rt = int(imdb_c.normalized_value)
        adopted, rejected = [imdb_c], []
        classes = []
        for c, e in page_c:
            cls, pal_formula = classify_runtime_diff(int(c.normalized_value), imdb_rt, cut_types=e["cut_type"],
                                                     discs=e["discs"], has_pal_evidence=has_pal(e["video_system"]),
                                                     rules=rules)
            classes.append(cls)
            diffs.append((e["asin"], int(c.normalized_value), imdb_rt, int(c.normalized_value) - imdb_rt, cls,
                          pal_formula, has_pal(e["video_system"]), list(e["cut_type"]), e["discs"]))
            if cls == "exact":
                adopted.append(c)
            else:
                rejected.append(c)
                c.mark("rejected", cls)
        settle(cands, adopted=adopted)
        worst = worst_diff_class(classes)
        ctype = DIFF_CONFLICT_TYPE[worst] if worst else "none"
        issue = "unexplained_gt15" if "unexplained_gt15" in classes else None
        detail = {"worst_diff_class": worst, "diff_classes": {e["asin"]: cls for (c, e), cls in zip(page_c, classes)}}
        sel = selection("resolved", imdb_rt, [imdb_c], cands, ctype, "RT1",
                        "有 IMDb 片长：作品层采用 IMDb，各版本差异写入 runtime_diff",
                        imdb_confidence(link["base_reliability"], rules, single_signal=single_signal,
                                        override=ctype == "cross_source_override"), detail)
        return cands, sel, diffs, issue
    d = decide_runtime_without_imdb([PageRuntime(c.source_key, int(c.normalized_value),
                                                 bool(e["cut_type"])) for c, e in page_c], rules)
    if d.status != "resolved":
        return cands, selection(d.status, None, [], cands, d.conflict_type, f"runtime.noimdb:{d.rule}", d.reason, 0,
                                {"values": sorted({c.normalized_value for c in cands})}), [], None
    won = [c for c in cands if int(c.normalized_value) == d.value]
    settle(cands, adopted=won, rejected=[c for c in cands if c not in won],
           reason="not_selected_by_" + d.rule)
    conf = {"MN1": rules.conf_page_only, "MN2": rules.conf_tolerance, "MN3": rules.conf_priority}[d.rule]
    return cands, selection("resolved", d.value, [won[0]], cands, d.conflict_type, f"runtime.noimdb:{d.rule}",
                            d.reason, conf, {"values": sorted({c.normalized_value for c in cands})}), [], None


# ---------------------------------------------------------------- 分级（R33）

def _rating(alloc, movie, editions, edcands, rules):
    cands: list[tuple[Cand, str]] = []
    for e in editions:
        if e["mpaa_rating"] is None:
            continue
        sel_ids = (e["field_selections"].get("mpaa_rating") or {}).get("selected_candidate_ids") or []
        src = edcands.get(sel_ids[0], {}) if sel_ids else {}
        cands.append((new_cand(alloc, "movie", movie, "mpaa_rating", "edition_rating", e["asin"],
                               src.get("raw_value") or e["mpaa_rating"], e["mpaa_rating"], "exact",
                               {"asin": e["asin"], "edition_candidate_ids": list(sel_ids),
                                "task3_source": (src.get("source_ref") or {})}, 1.0, src.get("acquired_at")),
                      e["asin"]))
    if not cands:
        return [], selection("missing", None, [], [], "none", "MR0", "没有 edition 商品分级", 0)
    plain = [c for c, _ in cands]
    d = decide_movie_rating([c.normalized_value for c in plain])
    if d.status != "resolved":
        return plain, selection(d.status, None, [], plain, d.conflict_type, f"rating:{d.rule}", d.reason, 0,
                                {"ratings": sorted({c.normalized_value for c in plain})})
    won = [c for c in plain if c.normalized_value == d.value]
    rest = [c for c in plain if c not in won]
    settle(plain, adopted=won, superseded=rest, reason="unrated_vs_rated" if rest else None)
    conf = rules.conf_alias if d.conflict_type == "unrated_vs_rated" else (rules.conf_agree if len(plain) > 1
                                                                          else rules.conf_single)
    return plain, selection("resolved", d.value, [won[0]], plain, d.conflict_type, f"rating:{d.rule}", d.reason, conf,
                            {"ratings": sorted({c.normalized_value for c in plain})})


# ---------------------------------------------------------------- 阶段入口

def resolve_movies_scalar(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *, batch_size: int = DEFAULT_BATCH,
                          before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R20–R33、R53：作品层 5 个标量字段，按 movie_id 分批。返回本次写入的计数。"""
    i = ctx.inputs
    key = i.stage_key(STAGE)
    movies = sources.movie_ids(conn, i.t4_mapping_version_id)
    require_stage_complete(conn, ctx, "link_assess", movies)
    todo = pending_entities(conn, ctx, STAGE, movies)
    downloaded = sources.snapshot_downloaded_at(conn, i.imdb_snapshot_id)
    counts: dict[str, Any] = {"movies": 0, "candidates": 0, "runtime_diff": {}, "status": {}, "conflict_types": {}}
    batch_no = next_batch_no(conn, ctx, STAGE)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        links = sources.product_links(conn, i.t4_mapping_version_id, batch)
        lrows = {m: {**r, "snapshot": i.imdb_snapshot_id} for m, r in sources.link_rows(conn, i, batch).items()}
        sel6 = sources.release_selections(conn, i.t6_release_version_id, batch)
        cands6 = sources.release_candidates(conn, [c for s in sel6.values() for c in s["considered"]])
        bounds = sources.inferred_bounds(conn, i.t6_release_version_id, batch)
        tconsts = [r["effective_tconst"] for r in lrows.values()]
        imdb = sources.imdb_titles(conn, i.imdb_snapshot_id, tconsts)
        akas = sources.imdb_akas(conn, i.imdb_snapshot_id, tconsts)
        titles = sources.page_titles(conn, i, links)
        edition_asins = sorted({r["asin"] for rows in links.values() for r in rows if r["relation_type"] == "edition"})
        ed = sources.edition_results(conn, i, edition_asins)
        ed_cand_ids = [c for e in ed.values() for f in ("mpaa_rating", "runtime_minutes")
                       for c in ((e["field_selections"].get(f) or {}).get("selected_candidate_ids") or [])]
        edcands = _edition_cands(conn, i, ed_cand_ids)
        records = sources.source_records(conn, [a["source_record_id"] for rows in titles.values() for a in rows
                                                if a["source_record_id"]])
        with conn.transaction():
            alloc = IdAllocator(conn)
            all_cands: list[Cand] = []
            result_rows: list[tuple] = []
            diff_rows: list[tuple] = []
            alias_rows: list[tuple] = []
            entities: list[tuple[str, int]] = []
            for m in batch:
                link = lrows.get(m)
                title = imdb.get(link["effective_tconst"]) if link else None
                lit = lit_signals(link["sig_year"], link["sig_runtime"], link["sig_director"],
                                  link["sig_title"]) if link else []
                single = bool(link and link["is_weak"] and not link["cross_source_mismatch_halt"] and len(lit) == 1)
                eds = [{"asin": r["asin"], **ed[r["asin"]]} for r in links.get(m, ())
                       if r["relation_type"] == "edition" and r["asin"] in ed]
                sels: dict[str, dict[str, Any]] = {}
                c_title, sels["title"] = _title(
                    alloc, m, link, title, akas.get(link["effective_tconst"], []) if link else [], titles.get(m, []),
                    records, downloaded, rules, single)
                c_orig, sels["original_title"] = _original(alloc, m, link, title, downloaded, rules, single)
                close_rt = bool(title and title["runtime_minutes"] is not None and any(
                    e["runtime_minutes"] is not None and abs(e["runtime_minutes"] - title["runtime_minutes"]) <= rules.runtime_gap
                    for e in eds))
                c_year, sels["release_year"], year_decision = _release_year(
                    alloc, m, link, title, sel6.get(m), cands6, close_rt, rules, single)
                c_run, sels["runtime_minutes"], diffs, severe = _runtime(alloc, m, link, title, eds, edcands,
                                                                          downloaded, rules, single)
                c_rate, sels["mpaa_rating"] = _rating(alloc, m, eds, edcands, rules)
                for d in diffs:
                    diff_rows.append((i.extraction_id, key, m, *d, ctx.run_id))
                    counts["runtime_diff"][d[4]] = counts["runtime_diff"].get(d[4], 0) + 1
                if severe:
                    add_quality_issue(conn, ctx, STAGE, m, "severe_runtime_conflict",
                                      {"imdb_runtime": title["runtime_minutes"],
                                       "editions": [{"asin": d[0], "page": d[1], "delta": d[3]} for d in diffs
                                                    if d[4] == "unexplained_gt15"]})
                if year_decision is not None and year_decision.issue:
                    add_quality_issue(conn, ctx, STAGE, m, year_decision.issue,
                                      {"field": "release_year", "rule": year_decision.rule, "imdb_year": year_decision.year,
                                       "page_years": sels["release_year"]["disagreement_detail"].get("page_years")})
                halted = bool(link and link["cross_source_mismatch_halt"])
                page_refs = [(a["raw_value"] and clean_page_display_title(a["raw_value"]),
                              {"asin": a["asin"], "assertion_id": a["assertion_id"], "table": "task4.field_assertion"})
                             for a in titles.get(m, ()) if a["raw_value"]]
                for kind, ordinal, alias, region, language, ref in _aliases(
                        m, link, title, akas.get(link["effective_tconst"], []) if link else [], page_refs, halted):
                    alias_rows.append((i.extraction_id, key, m, kind, ordinal, alias, region, language, Jsonb(ref),
                                       ctx.run_id))
                b = bounds.get(m)
                win = lambda f: sels[f]["winning_value"]  # noqa: E731
                result_rows.append((
                    i.extraction_id, key, m, win("title"), win("original_title"),
                    None if win("release_year") is None else int(win("release_year")),
                    None if win("runtime_minutes") is None else int(win("runtime_minutes")), win("mpaa_rating"),
                    link["effective_tconst"] if link else None, link["tconst_source"] if link else None, halted,
                    b["bound_time"] if b else None, b["bound_precision"] if b else None,
                    b["chosen_evidence"] if b else None, Jsonb(sels), ctx.run_id))
                cands = c_title + c_orig + c_year + c_run + c_rate
                all_cands += cands
                entities.append((m, len(cands) + 1))
                for name, s in sels.items():
                    counts["status"][f"{name}:{s['status']}"] = counts["status"].get(f"{name}:{s['status']}", 0) + 1
                    counts["conflict_types"][s["conflict_type"]] = counts["conflict_types"].get(s["conflict_type"], 0) + 1
            n = write_candidates(conn, ctx, key, all_cands)
            copy_rows(conn, "task7.movie_scalar_stage_result", RESULT_COLUMNS, result_rows)
            copy_rows(conn, "task7.runtime_diff", DIFF_COLUMNS, diff_rows)
            copy_rows(conn, "task7.movie_title_alias", ALIAS_COLUMNS, alias_rows)
            mark_entities(conn, ctx, STAGE, batch_no, entities)
            mark_batch(conn, ctx, STAGE, batch_no, batch[0], batch[-1], n + len(result_rows) + len(diff_rows) + len(alias_rows))
            if before_commit is not None:
                before_commit(STAGE, batch_no)
        counts["movies"] += len(batch)
        counts["candidates"] += n
        batch_no += 1
    return counts
