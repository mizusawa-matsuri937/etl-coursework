"""任务 7 的只读取数辅助：任务 3/4/5/6 与 IMDb 的统一入口（spec R01：全部带输入键）。

各阶段模块从这里取数，不在任务 1–6 的表上做任何写操作。断言与标题键沿用任务 4 的规范化版本（t4n1）；
页面字段不带 source_record_id，用 task4.product_edition 的快照哈希对上 task4.source_record 取定位（design 第 6 节）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

import psycopg

from .run import RunInputs

ASSERTION_KEYS = ("assertion_id", "entity_type", "entity_id", "field_name", "ordinal", "raw_value",
                  "normalized_value", "qualifier", "source_record_id", "source_locator")


def t3_rule_of(conn: psycopg.Connection, t4: str) -> str:
    return conn.execute("SELECT t3_rule_version FROM task4.mapping_version WHERE mapping_version_id = %s",
                        (t4,)).fetchone()[0]


def _chunks(items: list, size: int = 500) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------- 版本层（商品）

def matched_editions(conn: psycopg.Connection, i: RunInputs) -> list[tuple[str, str]]:
    """R06：版本层范围 = task4.product_edition 中 match_scope = 'matched' 的商品（含无关联作品的合集），按 ASIN 排序。"""
    t3 = t3_rule_of(conn, i.t4_mapping_version_id)
    return conn.execute("SELECT asin, product_kind FROM task4.product_edition WHERE extraction_id = %s AND "
                        "t3_rule_version = %s AND match_scope = 'matched' ORDER BY asin COLLATE \"C\"",
                        (i.extraction_id, t3)).fetchall()


def edition_movies(conn: psycopg.Connection, t4: str, asins: list[str]) -> dict[str, list[str]]:
    """asin → 关联作品 movie_id（按成员序号、movie_id 排序，去重）；合集没有关联作品时不出现。"""
    out: dict[str, list[str]] = defaultdict(list)
    for asin, movie in conn.execute(
            "SELECT asin, movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = ANY(%s) "
            "ORDER BY asin, member_ordinal, movie_id", (t4, asins)):
        if movie not in out[asin]:
            out[asin].append(movie)
    return out


def assertions(conn: psycopg.Connection, i: RunInputs, fields: list[str], entity_ids: list[str]
               ) -> dict[str, list[dict[str, Any]]]:
    """entity_id → 断言列表（按字段、序号排序）。区域取 source_locator 的 section，序号取页面字段序号。"""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(entity_ids):
        for r in conn.execute(
                "SELECT assertion_id, entity_type, entity_id, field_name, ordinal, raw_value, normalized_value, "
                "qualifier, source_record_id, source_locator FROM task4.field_assertion WHERE extraction_id = %s "
                "AND parser_version = %s AND norm_version = %s AND field_name = ANY(%s) AND entity_id = ANY(%s) "
                "ORDER BY entity_id, field_name, ordinal, assertion_id",
                (i.extraction_id, i.parser_version, i.norm_version, fields, chunk)):
            a = dict(zip(ASSERTION_KEYS, r))
            loc = a["source_locator"] or {}
            a["section"] = loc.get("section")
            a["page_ordinal"] = loc.get("ordinal")
            out[a["entity_id"]].append(a)
    return out


def source_records(conn: psycopg.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    """source_record_id → 页面快照来源（哈希、采集时间、快照路径）。"""
    out: dict[int, dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(ids))):
        for sid, ch, at, loc in conn.execute(
                "SELECT source_record_id, content_hash, acquired_at, locator FROM task4.source_record "
                "WHERE source_record_id = ANY(%s)", (chunk,)):
            out[sid] = {"content_hash": ch, "acquired_at": at, "snapshot_path": (loc or {}).get("snapshot_path")}
    return out


def page_sources(conn: psycopg.Connection, i: RunInputs, asins: list[str]) -> dict[str, dict[str, Any]]:
    """asin → 页面来源（source_record_id、快照哈希、采集时间、最终地址）：任务 3 页面字段没有 source_record_id，
    用 task4.product_edition 的快照哈希对上 task4.source_record（source_type = 'page'）。"""
    t3 = t3_rule_of(conn, i.t4_mapping_version_id)
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(asins):
        for asin, sid, ch, at, url in conn.execute(
                "SELECT e.asin, r.source_record_id, e.content_hash, coalesce(r.acquired_at, e.fetched_at), e.final_url "
                "FROM task4.product_edition e LEFT JOIN task4.source_record r ON r.source_type = 'page' "
                "AND r.content_hash = e.content_hash AND r.locator->>'asin' = e.asin "
                "AND r.locator->>'extraction_id' = e.extraction_id WHERE e.extraction_id = %s "
                "AND e.t3_rule_version = %s AND e.asin = ANY(%s)", (i.extraction_id, t3, chunk)):
            out[asin] = {"source_record_id": sid, "content_hash": ch, "acquired_at": at, "final_url": url}
    return out


def page_fields(conn: psycopg.Connection, i: RunInputs, asins: list[str], field_keys: list[str]
                ) -> dict[str, list[dict[str, Any]]]:
    """asin → 任务 3 页面字段（区域、序号、标签、原值、字段键），按区域序号排序。"""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(asins):
        for asin, section, ordinal, label, value, key in conn.execute(
                "SELECT asin, section, ordinal, label_raw, value_raw, field_key FROM task3.page_field "
                "WHERE extraction_id = %s AND parser_version = %s AND asin = ANY(%s) AND field_key = ANY(%s) "
                "ORDER BY asin, section, ordinal", (i.extraction_id, i.parser_version, chunk, field_keys)):
            out[asin].append({"section": section, "ordinal": ordinal, "label_raw": label, "value_raw": value,
                              "field_key": key})
    return out


def product_dates(conn: psycopg.Connection, i: RunInputs, asins: list[str]) -> dict[str, list[dict[str, Any]]]:
    """asin → 任务 6 商品日期（kind、日期、标记、区域、断言 ID）。"""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(asins):
        for r in conn.execute(
                "SELECT asin, kind, value_date, flags, section, page_ordinal, assertion_id, raw_value, label_raw, "
                "source_record_id FROM task6.product_release_date WHERE extraction_id = %s AND parser_version = %s "
                "AND asin = ANY(%s) ORDER BY asin, value_date, assertion_id", (i.extraction_id, i.parser_version, chunk)):
            out[r[0]].append(dict(zip(("asin", "kind", "value_date", "flags", "section", "page_ordinal",
                                       "assertion_id", "raw_value", "label_raw", "source_record_id"), r)))
    return out


# ---------------------------------------------------------------- 作品层（任务 4 映射、任务 6 上映版本、IMDb、任务 5 人物）

def movie_ids(conn: psycopg.Connection, t4: str) -> list[str]:
    """映射版本的全部作品（含只经 contains 出现的），按 movie_id 排序。"""
    return [r[0] for r in conn.execute("SELECT movie_id FROM task4.movie WHERE mapping_version_id = %s "
                                       "ORDER BY movie_id COLLATE \"C\"", (t4,))]


def product_links(conn: psycopg.Connection, t4: str, movies: list[str]) -> dict[str, list[dict[str, Any]]]:
    """movie → 关联商品行（asin、成员序号、关系、依据、任务 4 决策置信度）；按 asin、成员序号排序。"""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(movies):
        for movie, asin, ordinal, rel, basis, dec, conf in conn.execute(
                "SELECT pm.movie_id, pm.asin, pm.member_ordinal, pm.relation_type, pm.basis, pm.decision_id, "
                "d.confidence FROM task4.product_movie pm LEFT JOIN task4.merge_decision d "
                "ON d.decision_id = pm.decision_id WHERE pm.mapping_version_id = %s AND pm.movie_id = ANY(%s) "
                "ORDER BY pm.movie_id, pm.asin, pm.member_ordinal", (t4, chunk)):
            out[movie].append({"asin": asin, "member_ordinal": ordinal, "relation_type": rel, "basis": basis,
                               "decision_id": dec, "confidence": None if conf is None else float(conf)})
    return out


def page_titles(conn: psycopg.Connection, i: RunInputs, links: dict[str, list[dict[str, Any]]]
                ) -> dict[str, list[dict[str, Any]]]:
    """movie → 页面标题断言：edition 读 entity_id = asin，contains 读 collection_member 的 asin#成员序号
    （R21，严禁读盒子总标题）。每条带 asin、成员序号、关系、依据、任务 4 决策置信度。"""
    wanted: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for movie, rows in links.items():
        for r in rows:
            entity = r["asin"] if r["relation_type"] == "edition" else f"{r['asin']}#{r['member_ordinal']}"
            wanted[entity].append((movie, r))
    found = assertions(conn, i, ["title"], list(wanted))
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity, owners in wanted.items():
        for a in found.get(entity, ()):
            if a["entity_type"] != ("product_edition" if "#" not in entity else "collection_member"):
                continue
            for movie, r in owners:
                out[movie].append({**a, "asin": r["asin"], "member_ordinal": r["member_ordinal"],
                                   "relation_type": r["relation_type"], "basis": r["basis"],
                                   "decision_confidence": r["confidence"]})
    return out


def release_selections(conn: psycopg.Connection, rv: str, movies: list[str]) -> dict[str, dict[str, Any]]:
    """movie → 任务 6 选择记录（状态、缺失原因、年份、采用候选、考虑过的候选）。"""
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(movies):
        for movie, status, reason, year, sel, considered, rule, disagreement in conn.execute(
                "SELECT movie_id, status, missing_reason, release_year, selected_candidate_id, "
                "considered_candidate_ids, selection_rule, disagreement FROM task6.release_selection "
                "WHERE release_version_id = %s AND movie_id = ANY(%s)", (rv, chunk)):
            out[movie] = {"status": status, "missing_reason": reason, "release_year": year,
                          "selected_candidate_id": sel, "considered": list(considered), "rule": rule,
                          "disagreement": disagreement}
    return out


def release_candidates(conn: psycopg.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    """task6 候选 ID → 候选行。"""
    out: dict[int, dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(ids))):
        for r in conn.execute(
                "SELECT candidate_id, source, source_key, raw_value, value_year, flags, source_ref, "
                "source_acquired_at, confidence FROM task6.release_candidate WHERE candidate_id = ANY(%s)", (chunk,)):
            out[r[0]] = dict(zip(("candidate_id", "source", "source_key", "raw_value", "value_year", "flags",
                                  "source_ref", "source_acquired_at", "confidence"), r))
    return out


def bound_evidence(conn: psycopg.Connection, rv: str, movies: list[str]) -> dict[str, list[dict[str, Any]]]:
    """movie → 任务 6 推断上界两类证据行（含被条件拦下的）。"""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(movies):
        for movie, ev, t, passed, failed in conn.execute(
                "SELECT movie_id, evidence, evidence_time, passed, failed_checks FROM task6.bound_evidence "
                "WHERE release_version_id = %s AND movie_id = ANY(%s) ORDER BY movie_id, evidence", (rv, chunk)):
            out[movie].append({"evidence": ev, "evidence_time": t, "passed": passed, "failed_checks": list(failed)})
    return out


def inferred_bounds(conn: psycopg.Connection, rv: str, movies: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(movies):
        for movie, t, prec, ev in conn.execute(
                "SELECT movie_id, bound_time, bound_precision, chosen_evidence FROM task6.inferred_release_bound "
                "WHERE release_version_id = %s AND movie_id = ANY(%s)", (rv, chunk)):
            out[movie] = {"bound_time": t, "bound_precision": prec, "chosen_evidence": ev}
    return out


def imdb_titles(conn: psycopg.Connection, snapshot: str, tconsts: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(tconsts))):
        for tc, primary, original, year, runtime, genres, line in conn.execute(
                "SELECT tconst, primary_title, original_title, start_year, runtime_minutes, genres, line_no "
                "FROM imdb.title WHERE snapshot_id = %s AND tconst = ANY(%s)", (snapshot, chunk)):
            out[tc] = {"primary_title": primary, "original_title": original, "start_year": year,
                       "runtime_minutes": runtime, "genres": list(genres or []), "line_no": int(line)}
    return out


def imdb_akas(conn: psycopg.Connection, snapshot: str, tconsts: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(sorted(set(tconsts))):
        for tc, ordering, title, region, language, is_original, line in conn.execute(
                "SELECT tconst, ordering, title, region, language, is_original, line_no FROM imdb.title_aka "
                "WHERE snapshot_id = %s AND tconst = ANY(%s) ORDER BY tconst, ordering", (snapshot, chunk)):
            out[tc].append({"ordering": ordering, "title": title, "region": region, "language": language,
                            "is_original": is_original, "line_no": int(line)})
    return out


def imdb_directors(conn: psycopg.Connection, snapshot: str, tconsts: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(sorted(set(tconsts))):
        for tc, ordinal, nconst, name in conn.execute(
                "SELECT d.tconst, d.ordinal, d.nconst, n.primary_name FROM imdb.title_director d "
                "LEFT JOIN imdb.name n ON n.snapshot_id = d.snapshot_id AND n.nconst = d.nconst "
                "WHERE d.snapshot_id = %s AND d.tconst = ANY(%s) ORDER BY d.tconst, d.ordinal", (snapshot, chunk)):
            out[tc].append({"ordinal": ordinal, "nconst": nconst, "name": name})
    return out


def imdb_title_keys(conn: psycopg.Connection, i: RunInputs, tconsts: list[str]) -> dict[str, list[str]]:
    """tconst → IMDb 全部标题键（primary、original、aka）。"""
    out: dict[str, list[str]] = defaultdict(list)
    for chunk in _chunks(sorted(set(tconsts))):
        for tc, key in conn.execute(
                "SELECT tconst, title_key FROM task4.imdb_title_key WHERE imdb_snapshot_id = %s AND norm_version = %s "
                "AND tconst = ANY(%s) ORDER BY tconst, key_source, aka_ordering", (i.imdb_snapshot_id, i.norm_version,
                                                                                 chunk)):
            out[tc].append(key)
    return out


def snapshot_downloaded_at(conn: psycopg.Connection, snapshot: str):
    row = conn.execute("SELECT downloaded_at FROM imdb.snapshot WHERE snapshot_id = %s", (snapshot,)).fetchone()
    return row[0] if row else None


def page_directors(conn: psycopg.Connection, i: RunInputs, movies: list[str]) -> dict[str, list[dict[str, Any]]]:
    """movie → 任务 5 人物映射里的页面导演（person_id、规范名与页面别名、nconst）；R16：导演比较的页面一侧。"""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pmap = i.t5_person_mapping_version_id
    for chunk in _chunks(movies):
        rows = conn.execute(
            "SELECT mp.movie_id, mp.person_id, p.canonical_name, e.external_id FROM task5.movie_person mp "
            "JOIN task5.person p ON p.person_mapping_version_id = mp.person_mapping_version_id "
            "AND p.person_id = mp.person_id LEFT JOIN task5.person_external_id e "
            "ON e.person_mapping_version_id = mp.person_mapping_version_id AND e.person_id = mp.person_id "
            "AND e.system = 'imdb' WHERE mp.person_mapping_version_id = %s AND mp.movie_id = ANY(%s) "
            "AND mp.role = 'director' ORDER BY mp.movie_id, mp.person_id", (pmap, chunk)).fetchall()
        aliases: dict[str, list[str]] = defaultdict(list)
        pids = sorted({r[1] for r in rows})
        if pids:
            for pid, alias in conn.execute(
                    "SELECT person_id, alias FROM task5.person_alias WHERE person_mapping_version_id = %s "
                    "AND source = 'page' AND person_id = ANY(%s) ORDER BY person_id, alias", (pmap, pids)):
                aliases[pid].append(alias)
        for movie, pid, name, nconst in rows:
            names = [name] + [a for a in aliases.get(pid, ()) if a != name]
            out[movie].append({"person_id": pid, "names": names, "nconst": nconst})
    return out


# ---------------------------------------------------------------- 本任务的阶段结果

def edition_results(conn: psycopg.Connection, i: RunInputs, asins: list[str]) -> dict[str, dict[str, Any]]:
    """asin → edition_resolve 阶段结果（只读本输入键下的阶段键）。"""
    key = i.stage_key("edition_resolve")
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(asins))):
        for r in conn.execute(
                "SELECT asin, product_kind, movie_ids, runtime_minutes, mpaa_rating, discs, cut_type, video_system, "
                "field_selections FROM task7.edition_stage_result WHERE extraction_id = %s AND stage_key = %s "
                "AND asin = ANY(%s)", (i.extraction_id, key, chunk)):
            out[r[0]] = dict(zip(("asin", "product_kind", "movie_ids", "runtime_minutes", "mpaa_rating", "discs",
                                  "cut_type", "video_system", "field_selections"), r))
    return out


def link_rows(conn: psycopg.Connection, i: RunInputs, movies: list[str]) -> dict[str, dict[str, Any]]:
    """movie → link_assess 阶段的关联评估行（movie_scalar 与 movie_multivalue 读同一判定，AC21）。"""
    key = i.stage_key("link_assess")
    out: dict[str, dict[str, Any]] = {}
    cols = ("movie_id", "effective_tconst", "tconst_source", "base_reliability", "is_weak", "sig_year", "sig_runtime",
            "sig_director", "sig_title", "cross_source_mismatch_halt", "halt_reason", "signal_detail")
    for chunk in _chunks(movies):
        for r in conn.execute(
                f"SELECT {', '.join(cols)} FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s "
                "AND movie_id = ANY(%s)", (i.extraction_id, key, chunk)):
            d = dict(zip(cols, r))
            d["base_reliability"] = float(d["base_reliability"])
            out[d["movie_id"]] = d
    return out
