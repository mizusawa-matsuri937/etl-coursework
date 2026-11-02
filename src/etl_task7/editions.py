"""版本层消解（edition_resolve，spec R04–R08、R44–R48）：每个 matched 商品一行，6 个标量字段与 7 项多值特征。

范围取 task4.product_edition 中 match_scope = 'matched' 的商品（R06），不取 task4.product_movie：没有关联作品的合集
照常消解，movie_ids 为空数组。版本物理差异（载体、语言、字幕、碟数、地区码、制式、画幅、剪辑版、发行商与发售日）
留在版本层，不进入作品层冲突（R04）；合集的总片长、分级与类型属于合集版本自身（R05）。
按 ASIN 分批（R53），候选、阶段结果、实体标记与批次审计同事务提交。
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task4.normalize import title_key

from . import sources
from .rules import (RuleSet, clean_page_display_title, decide_publisher, discs_from_packaging, normalize_mpaa,
                    pick_by_section, publisher_key, section_rank)
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, add_quality_issue, copy_rows, mark_batch, mark_entities,
                  next_batch_no, pending_entities)
from .selection import Cand, IdAllocator, new_cand, selection, settle, write_candidates

STAGE = "edition_resolve"
RESULT_COLUMNS = ("extraction_id", "stage_key", "asin", "product_kind", "movie_ids", "edition_title",
                  "edition_clean_title", "runtime_minutes", "mpaa_rating", "publisher", "product_release_date",
                  "media_release_date", "listing_first_available_date", "date_flags", "discs", "media_format",
                  "language", "subtitles", "aspect_ratio", "region", "cut_type", "video_system",
                  "attributes_detail", "field_selections", "run_id")
FEATURES = ("media_format", "language", "subtitles", "aspect_ratio", "region", "cut_type", "video_system")
PAGE_RELIABILITY = 1.0           # 版本层候选来自该 ASIN 自己的页面


# ---------------------------------------------------------------- 多值特征的规范化（R48）

_MEDIA = (("blu-ray", "Blu-ray"), ("blu ray", "Blu-ray"), ("bluray", "Blu-ray"), ("hd dvd", "HD DVD"),
          ("hd-dvd", "HD DVD"), ("dvd", "DVD"), ("vhs", "VHS"), ("umd", "UMD"), ("laserdisc", "LaserDisc"),
          ("laser disc", "LaserDisc"), ("multiple formats", "Multiple Formats"), ("4k", "4K"), ("ultra hd", "4K"),
          ("digital", "Digital"))
_VIDEO_SYSTEMS = {"ntsc": "NTSC", "pal": "PAL", "secam": "SECAM"}
_ASPECT_WORDS = (("widescreen", "Widescreen"), ("full screen", "Full Screen"), ("fullscreen", "Full Screen"),
                 ("anamorphic", "Anamorphic"), ("letterbox", "Letterboxed"), ("pan & scan", "Pan & Scan"),
                 ("pan and scan", "Pan & Scan"))
_CUT_NAMES = {"directors cut": "Director's Cut", "final cut": "Final Cut", "extended": "Extended",
              "unrated": "Unrated", "uncut": "Uncut", "remastered": "Remastered", "restored": "Restored",
              "colorized": "Colorized", "theatrical": "Theatrical"}


def media_label(token: str) -> str | None:
    low = token.casefold()
    for needle, label in _MEDIA:
        if needle in low:
            return label
    return None


def aspect_words(text: str) -> list[str]:
    low = text.casefold()
    return [label for needle, label in _ASPECT_WORDS if needle in low]


def _split_tokens(value: str | None) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _display(value: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in value.split())


def feature_values(asin: str, assertions: list[dict[str, Any]], fields: list[dict[str, Any]]
                   ) -> dict[str, dict[str, list[str]]]:
    """R48：7 项多值特征取 task4.field_assertion 与 task3.page_field 的规范化并集，每个值保留来源区域（按优先级排序）。
    media_format 的逗号串先拆开：载体词归 media_format，NTSC/PAL 归 video_system，Widescreen 等画幅词归 aspect_ratio；
    其余描述词（Color、Dolby）不属于这 7 项特征。"""
    found: dict[str, dict[str, set[str]]] = {f: defaultdict(set) for f in FEATURES}

    def add(feature: str, value: str | None, section: str | None) -> None:
        if value:
            found[feature][value].add(section or "unknown")

    for a in assertions:
        name, sec, norm, raw = a["field_name"], a["section"], a["normalized_value"], a["raw_value"]
        if name == "edition.media_format":
            for tok in _split_tokens(norm):
                if (label := media_label(tok)):
                    add("media_format", label, sec)
                if tok.casefold() in _VIDEO_SYSTEMS:
                    add("video_system", _VIDEO_SYSTEMS[tok.casefold()], sec)
            for w in aspect_words(raw or ""):
                add("aspect_ratio", w, sec)
        elif name == "edition.video_system":
            for tok in _split_tokens(norm):
                if tok.casefold() in _VIDEO_SYSTEMS:
                    add("video_system", _VIDEO_SYSTEMS[tok.casefold()], sec)
        elif name in ("edition.language", "edition.subtitles"):
            feat = "language" if name == "edition.language" else "subtitles"
            for tok in _split_tokens(norm):
                if not tok.casefold().startswith("unqualified"):
                    add(feat, _display(tok), sec)
        elif name == "edition.aspect_ratio":
            add("aspect_ratio", norm, sec)
        elif name == "edition.region":
            add("region", norm.upper() if norm in ("usa", "uk") else _display(norm or ""), sec)
        elif name == "edition.cut_type":
            add("cut_type", _CUT_NAMES.get(norm or "", _display(norm or "")), sec)
        elif name == "edition.packaging":
            for w in aspect_words(raw or ""):
                add("aspect_ratio", w, sec)
    for f in fields:
        if f["field_key"] == "format_swatch" and (label := media_label(f["value_raw"])):
            add("media_format", label, f["section"])
    return {feat: {v: sorted(secs, key=lambda s: (section_rank(s), s)) for v, secs in sorted(vals.items())}
            for feat, vals in found.items()}


# ---------------------------------------------------------------- 候选构造

def _assertion_cand(alloc: IdAllocator, asin: str, a: dict[str, Any], field: str, source: str, normalized: str | None,
                    precision: str, records: dict[int, dict[str, Any]], page: dict[str, Any]) -> Cand:
    rec = records.get(a["source_record_id"]) or {}
    ref = {"asin": asin, "assertion_id": a["assertion_id"], "source_record_id": a["source_record_id"],
           "task4_field": a["field_name"], "section": a["section"], "ordinal": a["page_ordinal"],
           "content_hash": rec.get("content_hash") or page.get("content_hash"),
           "snapshot_path": rec.get("snapshot_path")}
    return new_cand(alloc, "edition", asin, field, source, f"{asin}:{a['assertion_id']}", a["raw_value"], normalized,
                    precision, ref, PAGE_RELIABILITY, rec.get("acquired_at") or page.get("acquired_at"))


def _winner_index(cands: list[Cand], rules: RuleSet, value: str) -> int:
    ok = [k for k, c in enumerate(cands) if c.normalized_value == value]
    return min(ok, key=lambda k: (section_rank(cands[k].source_ref.get("section"), rules), k))


def _agree_conf(rules: RuleSet, cands: list[Cand]) -> float:
    return rules.conf_agree if len(cands) > 1 else rules.conf_single


# ---------------------------------------------------------------- 六个标量字段

def _title(alloc, asin, items, records, page, rules):
    cands = [_assertion_cand(alloc, asin, a, "edition_title", "page_title", clean_page_display_title(a["raw_value"] or ""),
                             "exact", records, page) for a in items if a["raw_value"]]
    if not cands:
        return cands, selection("missing", None, [], [], "none", "ET0", "页面没有标题", 0)
    keys = {title_key(c.normalized_value or "") for c in cands}
    if len(keys) > 1:
        return cands, selection("conflict_unresolved", None, [], cands, "intra_source_tie_unresolved", "ET2",
                                "同一 ASIN 的标题断言实质不同", 0, {"clean_titles": sorted({c.normalized_value for c in cands})})
    settle(cands, adopted=cands)
    return cands, selection("resolved", cands[0].raw_value, [cands[0]], cands, "none", "ET1",
                            "页面标题（原始标题与清洁规范名都保存）", _agree_conf(rules, cands),
                            {"clean_title": cands[0].normalized_value})


def _runtime(alloc, asin, items, records, page, rules):
    cands = [_assertion_cand(alloc, asin, a, "runtime_minutes", "page_runtime", a["normalized_value"], "minute",
                             records, page) for a in items if a["normalized_value"]]
    if not cands:
        return cands, selection("missing", None, [], [], "none", "ER0", "页面没有片长", 0)
    values = sorted({int(c.normalized_value) for c in cands})
    top = pick_by_section([(int(c.normalized_value), c.source_ref.get("section")) for c in cands], rules)
    win = cands[top]
    adopted = [c for c in cands if c.normalized_value == win.normalized_value]
    settle(cands, adopted=adopted, rejected=[c for c in cands if c not in adopted], reason="intra_asin_runtime_conflict"
           if len(values) > 1 else None)
    if len(values) == 1:
        return cands, selection("resolved", win.normalized_value, [win], cands, "none", "ER1", "各区域片长一致",
                                _agree_conf(rules, cands))
    return cands, selection("resolved", win.normalized_value, [win], cands, "intra_asin_runtime_conflict", "ER2",
                            "同一 ASIN 各区域片长不同，按区域优先级 detail_bullets > product_overview > product_details 取值",
                            rules.conf_priority, {"values": values})


def _mpaa(conn, ctx, alloc, asin, items, page, rules):
    cands: list[Cand] = []
    for f in items:
        raw = f["value_raw"]
        c = new_cand(alloc, "edition", asin, "mpaa_rating", "page_rating", f"{asin}:{f['section']}:{f['ordinal']}", raw,
                     normalize_mpaa(raw), "exact",
                     {"asin": asin, "section": f["section"], "ordinal": f["ordinal"], "label_raw": f["label_raw"],
                      "field_key": f["field_key"], "task3_table": "page_field",
                      "source_record_id": page.get("source_record_id"), "content_hash": page.get("content_hash")},
                     PAGE_RELIABILITY, page.get("acquired_at"))
        cands.append(c)
        if c.normalized_value is None:
            add_quality_issue(conn, ctx, STAGE, asin, "unknown_mpaa_rating",
                              {"raw_value": raw, "section": f["section"], "ordinal": f["ordinal"]})
    if not cands:
        return cands, selection("missing", None, [], [], "none", "EM0", "页面没有分级", 0)
    known = [c for c in cands if c.normalized_value is not None]
    if not known:
        return cands, selection("conflict_unresolved", None, [], cands, "intra_source_tie_unresolved", "EM4",
                                "页面分级写法无法规范化为受控分级，不猜测", 0,
                                {"unknown_raw": sorted({c.raw_value for c in cands})})
    for c in cands:
        if c.normalized_value is None:
            c.mark("rejected", "unknown_mpaa_rating")
    values = sorted({c.normalized_value for c in known})
    top = pick_by_section([(c.normalized_value, c.source_ref["section"]) for c in known], rules)
    win = known[top]
    adopted = [c for c in known if c.normalized_value == win.normalized_value]
    if len(values) == 1:
        settle(known, adopted=adopted)
        alias = len({c.raw_value for c in known}) > 1
        return cands, selection("resolved", win.normalized_value, [win], cands,
                                "formatting_or_alias" if alias else "none", "EM2" if alias else "EM1",
                                "简称与全称写法规范化后同一分级" if alias else "各区域分级一致",
                                rules.conf_alias if alias else _agree_conf(rules, known))
    settle(known, adopted=adopted, rejected=[c for c in known if c not in adopted], reason="intra_edition_rating_conflict")
    return cands, selection("resolved", win.normalized_value, [win], cands, "intra_edition_rating_conflict", "EM3",
                            "同一 ASIN 的分级不一致，优先 detail_bullets", rules.conf_priority, {"values": values})


def _publisher(alloc, asin, items, records, page, rules):
    cands = [_assertion_cand(alloc, asin, a, "publisher", "page_publisher", publisher_key(a["raw_value"] or ""),
                             "exact", records, page) for a in items if a["raw_value"]]
    if not cands:
        return cands, selection("missing", None, [], [], "none", "EP0", "页面没有发行商", 0)
    d = decide_publisher([(c.raw_value, c.source_ref.get("section")) for c in cands], rules)
    win_k = min((k for k, c in enumerate(cands) if c.raw_value == d.value),
                key=lambda k: (section_rank(cands[k].source_ref.get("section"), rules), k))
    win = cands[win_k]
    same = [c for c in cands if c.normalized_value == win.normalized_value]
    settle(cands, adopted=same, superseded=[cands[k] for k in d.superseded if cands[k] not in same],
           rejected=[cands[k] for k in d.rejected if cands[k] not in same],
           reason={"PB2": "formatting_or_alias", "PB3": "intra_asin_publisher_conflict"}.get(d.rule))
    conf = {"PB1": _agree_conf(rules, cands), "PB2": rules.conf_alias, "PB3": rules.conf_priority}[d.rule]
    detail = {"values": sorted({c.raw_value for c in cands})} if d.conflict_type != "none" else {}
    return cands, selection("resolved", d.value, [win], cands, d.conflict_type, f"EP:{d.rule}", d.reason, conf, detail)


def _release_date(alloc, asin, rows, records, page, rules):
    cands: list[Cand] = []
    for r in rows:
        rec = records.get(r["source_record_id"]) or {}
        cands.append(new_cand(
            alloc, "edition", asin, "product_release_date", f"page_{r['kind']}", f"{asin}:{r['assertion_id']}",
            r["raw_value"], r["value_date"].isoformat(), "day",
            {"asin": asin, "assertion_id": r["assertion_id"], "section": r["section"], "ordinal": r["page_ordinal"],
             "kind": r["kind"], "label_raw": r["label_raw"], "flags": list(r["flags"] or []),
             "source_record_id": r["source_record_id"], "content_hash": rec.get("content_hash") or page.get("content_hash")},
            PAGE_RELIABILITY, rec.get("acquired_at") or page.get("acquired_at")))
    media = [c for c in cands if c.source_ref["kind"] == "media_release"]
    listing = [c for c in cands if c.source_ref["kind"] == "listing_first_available"]
    if not cands:
        return cands, selection("missing", None, [], [], "none", "ED0", "页面没有发售日期", 0), [], None, None
    media_dates = sorted({c.normalized_value for c in media})
    listing_dates = sorted({c.normalized_value for c in listing})
    media_date = media_dates[0] if len(media_dates) == 1 else None
    listing_date = listing_dates[0] if len(listing_dates) == 1 else None
    pool, kind, fallback = (media, "media_release", False) if media else (listing, "listing_first_available", True)
    dates = media_dates if media else listing_dates
    if len(dates) > 1:
        return (cands, selection("conflict_unresolved", None, [], cands, "intra_source_tie_unresolved", "ED4",
                                 f"同一 ASIN 的 {kind} 有多个不同日期，没有可靠依据裁决", 0, {"dates": dates}),
                [], media_date, listing_date)
    win_date = dates[0]
    adopted = [c for c in pool if c.normalized_value == win_date]
    other = [c for c in cands if c not in pool]
    conflict_type = "none"
    detail: dict[str, Any] = {}
    differing = [c for c in other if c.normalized_value != win_date]
    if differing:
        conflict_type = "semantic_distinction"
        detail = {"media_release": media_date, "listing_first_available": listing_date}
        settle(differing, rejected=differing, reason="listing_first_available_is_not_release_date")
    settle([c for c in other if c not in differing], adopted=[c for c in other if c not in differing])
    settle(adopted, adopted=adopted)
    flags = sorted({f for c in adopted for f in c.source_ref.get("flags", [])})
    reason = ("优先 media_release 发售日" if not fallback
              else "没有 media_release，回退到上架日 listing_first_available")
    if fallback:
        detail["fallback"] = "listing_first_available"
    if flags:
        detail["date_flags"] = flags
        conf = rules.conf_flagged_date
        reason += f"；任务 6 标记 {','.join(flags)}，值不改动，置信度降为 {conf:.2f}"
    else:
        conf = rules.conf_semantic if conflict_type == "semantic_distinction" else _agree_conf(rules, adopted)
    sel = selection("resolved", win_date, [adopted[0]], cands, conflict_type, "ED1" if not fallback else "ED2",
                    reason, conf, detail)
    return cands, sel, flags, media_date, listing_date


def _discs(alloc, asin, assertions, packaging, records, page, rules):
    primary = [_assertion_cand(alloc, asin, a, "discs", "page_discs", a["normalized_value"], "exact", records, page)
               for a in assertions if a["normalized_value"] and a["normalized_value"].isdigit()]
    derived: list[Cand] = []
    for a in packaging:
        n = discs_from_packaging(a["raw_value"])
        if n is not None:
            derived.append(_assertion_cand(alloc, asin, a, "discs", "page_packaging_discs", str(n), "exact",
                                           records, page))
    cands = primary + derived
    if not cands:
        return cands, selection("missing", None, [], [], "none", "EC0", "页面没有碟数", 0)
    if primary:
        values = sorted({c.normalized_value for c in primary}, key=int)
        top = pick_by_section([(c.normalized_value, c.source_ref.get("section")) for c in primary], rules)
        win = primary[top]
        adopted = [c for c in cands if c.normalized_value == win.normalized_value]
        settle(cands, adopted=adopted, rejected=[c for c in cands if c not in adopted],
               reason="discs_disagrees_with_selected")
        if len(values) == 1:
            return cands, selection("resolved", win.normalized_value, [win], cands, "none", "EC1",
                                    "edition.discs 断言一致", _agree_conf(rules, cands))
        return cands, selection("resolved", win.normalized_value, [win], cands, "intra_asin_runtime_conflict", "EC2",
                                "同一 ASIN 各区域碟数不同，按区域优先级取值", rules.conf_priority,
                                {"field": "discs", "values": values})
    values = sorted({c.normalized_value for c in derived}, key=int)
    if len(values) > 1:
        return cands, selection("conflict_unresolved", None, [], cands, "intra_source_tie_unresolved", "EC4",
                                "没有 edition.discs 断言，包装串给出的碟数不一致", 0, {"values": values})
    settle(derived, adopted=derived)
    return cands, selection("resolved", values[0], [derived[0]], cands, "none", "EC3",
                            "没有 edition.discs 断言，从 edition.packaging 提取整数", rules.conf_alias,
                            {"derived_from": "edition.packaging"})


# ---------------------------------------------------------------- 阶段入口

def resolve_editions(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *, batch_size: int = DEFAULT_BATCH,
                     before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R44–R48、R53：每个 matched 商品一行，按 ASIN 分批；返回本次写入的计数。"""
    i = ctx.inputs
    key = i.stage_key(STAGE)
    editions = sources.matched_editions(conn, i)
    kind_of = dict(editions)
    todo = pending_entities(conn, ctx, STAGE, [a for a, _ in editions])
    counts: dict[str, Any] = {"editions": 0, "candidates": 0, "status": {}, "conflict_types": {}}
    batch_no = next_batch_no(conn, ctx, STAGE)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        movies = sources.edition_movies(conn, i.t4_mapping_version_id, batch)
        asrt = sources.assertions(conn, i, ["title", "runtime_min", "edition.publisher", "edition.discs",
                                            "edition.packaging", "edition.media_format", "edition.language",
                                            "edition.subtitles", "edition.aspect_ratio", "edition.region",
                                            "edition.cut_type", "edition.video_system"], batch)
        fields = sources.page_fields(conn, i, batch, ["mpaa_rating", "format_swatch"])
        dates = sources.product_dates(conn, i, batch)
        pages = sources.page_sources(conn, i, batch)
        records = sources.source_records(conn, [a["source_record_id"] for rows in asrt.values() for a in rows
                                                if a["source_record_id"]] +
                                         [r["source_record_id"] for rows in dates.values() for r in rows
                                          if r["source_record_id"]])
        with conn.transaction():
            alloc = IdAllocator(conn)
            all_cands: list[Cand] = []
            result_rows: list[tuple] = []
            entities: list[tuple[str, int]] = []
            for asin in batch:
                rows = asrt.get(asin, [])
                by = lambda name: [a for a in rows if a["field_name"] == name]  # noqa: E731
                page = pages.get(asin, {})
                sels: dict[str, dict[str, Any]] = {}
                c_title, sels["edition_title"] = _title(alloc, asin, by("title"), records, page, rules)
                c_run, sels["runtime_minutes"] = _runtime(alloc, asin, by("runtime_min"), records, page, rules)
                mp_items = [f for f in fields.get(asin, []) if f["field_key"] == "mpaa_rating"]
                c_rate, sels["mpaa_rating"] = _mpaa(conn, ctx, alloc, asin, mp_items, page, rules)
                c_pub, sels["publisher"] = _publisher(alloc, asin, by("edition.publisher"), records, page, rules)
                c_date, sels["product_release_date"], flags, media_date, listing_date = _release_date(
                    alloc, asin, dates.get(asin, []), records, page, rules)
                c_disc, sels["discs"] = _discs(alloc, asin, by("edition.discs"), by("edition.packaging"), records,
                                               page, rules)
                cands = c_title + c_run + c_rate + c_pub + c_date + c_disc
                feats = feature_values(asin, rows, fields.get(asin, []))
                win = lambda f: sels[f]["winning_value"]  # noqa: E731
                result_rows.append((
                    i.extraction_id, key, asin, kind_of[asin], movies.get(asin, []), win("edition_title"),
                    (c_title[0].normalized_value if sels["edition_title"]["status"] == "resolved" else None),
                    None if win("runtime_minutes") is None else int(win("runtime_minutes")), win("mpaa_rating"),
                    win("publisher"), None if win("product_release_date") is None else date.fromisoformat(win("product_release_date")),
                    None if media_date is None else date.fromisoformat(media_date),
                    None if listing_date is None else date.fromisoformat(listing_date), flags,
                    None if win("discs") is None else int(win("discs")),
                    *[list(feats[f]) for f in FEATURES], Jsonb({f: feats[f] for f in FEATURES}),
                    Jsonb(sels), ctx.run_id))
                all_cands += cands
                entities.append((asin, len(cands) + 1))
                for name, s in sels.items():
                    counts["status"][s["status"]] = counts["status"].get(s["status"], 0) + 1
                    counts["conflict_types"][s["conflict_type"]] = counts["conflict_types"].get(s["conflict_type"], 0) + 1
            n = write_candidates(conn, ctx, key, all_cands)
            copy_rows(conn, "task7.edition_stage_result", RESULT_COLUMNS, result_rows)
            mark_entities(conn, ctx, STAGE, batch_no, entities)
            mark_batch(conn, ctx, STAGE, batch_no, batch[0], batch[-1], n + len(result_rows))
            if before_commit is not None:
                before_commit(STAGE, batch_no)
        counts["editions"] += len(batch)
        counts["candidates"] += n
        batch_no += 1
    return counts
