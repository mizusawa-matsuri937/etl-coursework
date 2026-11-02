r"""任务 7 集成测试辅助：把 `task7.world.World` 直接写成任务 3–6 与 IMDb 的上游表行（不运行上游流水线），
再按需逐阶段运行任务 7。每个案例只含自己需要的作品与商品；每个测试开始时 `seed` 清空 task7 与上游表。

上游表的写入只存在于测试里（测试夹具，非正式库）；任务 7 代码只读这些表。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task2.crawl.crawler import apply_crawl_schema
from etl_task3 import LABEL_MAP_VERSION, PARSER_VERSION, RULE_VERSION as T3_RULE
from etl_task3.classify import apply_task3_schema
from etl_task4.imdb import apply_imdb_schema
from etl_task4.normalize import title_key
from etl_task4.run import apply_task4_schema
from etl_task5.run import apply_task5_schema
from etl_task6.run import apply_task6_schema
from etl_task7.rules import ruleset
from etl_task7.run import DEFAULT_BATCH, RunContext, RunInputs, apply_task7_schema, start_run
from task7 import world as w7

ACQUIRED = datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc)
DOWNLOADED = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
SNAPSHOT = "imdb_2026-10-29"
OLD_SNAPSHOT = "imdb_2026-01-01"
T4, PMAP, RV = "map0002", "pmap0001", "rv0001"
NORM = "t4n1"
EXTRACTION = hashlib.sha256(b"task7-fixture").hexdigest()
_FILE = hashlib.sha256(b"task7-fixture-file").hexdigest()


@dataclass
class Seeded:
    eid: str
    world: w7.World

    def mid(self, name: str) -> str:
        return w7.movie_id(name)

    def inputs(self, **over: Any) -> RunInputs:
        base = dict(extraction_id=self.eid, t4_mapping_version_id=T4, t5_person_mapping_version_id=PMAP,
                    t6_release_version_id=RV, imdb_snapshot_id=SNAPSHOT)
        return RunInputs(**{**base, **over})


def reset(conn: psycopg.Connection) -> None:
    """清空 task7 与全部上游表（它们不随 conn 夹具清空）；重新建好 task3–task7 的结构。"""
    apply_crawl_schema(conn)
    apply_task3_schema(conn)
    apply_imdb_schema(conn)
    apply_task4_schema(conn)
    apply_task5_schema(conn)
    apply_task6_schema(conn)
    conn.execute("DROP SCHEMA IF EXISTS task7 CASCADE")
    conn.execute("DELETE FROM public.schema_migration WHERE version = '601'")
    conn.execute("TRUNCATE imdb.snapshot CASCADE")
    conn.execute("TRUNCATE task3.classify_run CASCADE")
    conn.execute("TRUNCATE task4.run CASCADE")
    conn.execute("TRUNCATE task4.source_record, task4.movie_identity, task4.human_decision CASCADE")
    conn.execute("TRUNCATE task5.run CASCADE")
    conn.execute("TRUNCATE task6.run CASCADE")
    apply_task7_schema(conn)


def _snapshot(conn: psycopg.Connection, snapshot: str, downloaded: datetime) -> None:
    conn.execute("INSERT INTO imdb.snapshot (snapshot_id, base_url, downloaded_at, status, loader_version, loaded_at) "
                 "VALUES (%s, 'https://example.invalid/', %s, 'loaded', 't7fixture', now())", (snapshot, downloaded))


def _imdb(conn: psycopg.Connection, snapshot: str, titles: list[w7.Imdb], *, with_keys: bool = True) -> None:
    line = 2
    names: dict[str, str] = {}
    for t in titles:
        conn.execute("INSERT INTO imdb.title (snapshot_id, tconst, title_type, primary_title, original_title, "
                     "start_year, runtime_minutes, genres, line_no) VALUES (%s, %s, 'movie', %s, %s, %s, %s, %s, %s)",
                     (snapshot, t.tconst, t.primary, t.original, t.year, t.runtime, list(t.genres), line))
        line += 1
        keys = [("primary", 0, t.primary, None, None)]
        if t.original:
            keys.append(("original", 0, t.original, None, None))
        for n, (title, region, language) in enumerate(t.akas, start=1):
            conn.execute("INSERT INTO imdb.title_aka (snapshot_id, tconst, ordering, title, region, language, "
                         "is_original, line_no) VALUES (%s, %s, %s, %s, %s, %s, false, %s)",
                         (snapshot, t.tconst, n, title, region, language, line))
            line += 1
            keys.append(("aka", n, title, region, language))
        if with_keys:
            for src, order, title, region, language in keys:
                conn.execute("INSERT INTO task4.imdb_title_key (imdb_snapshot_id, norm_version, tconst, key_source, "
                             "aka_ordering, title_key, title_class, region, language, source_line) "
                             "VALUES (%s, %s, %s, %s, %s, %s, 'movie', %s, %s, %s)",
                             (snapshot, NORM, t.tconst, src, order, title_key(title), region, language, line))
        for n, (nconst, name) in enumerate(t.directors, start=1):
            conn.execute("INSERT INTO imdb.title_director (snapshot_id, tconst, ordinal, nconst, line_no) "
                         "VALUES (%s, %s, %s, %s, %s)", (snapshot, t.tconst, n, nconst, line))
            names[nconst] = name
            line += 1
        for n, (nconst, name, category) in enumerate(t.principals, start=1):
            conn.execute("INSERT INTO imdb.title_principal (snapshot_id, tconst, ordering, nconst, category, line_no) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (snapshot, t.tconst, n, nconst, category, line))
            names[nconst] = name
            line += 1
    for n, (nconst, name) in enumerate(sorted(names.items()), start=1):
        conn.execute("INSERT INTO imdb.name (snapshot_id, nconst, primary_name, line_no) VALUES (%s, %s, %s, %s)",
                     (snapshot, nconst, name, n + 1))


def _page_source(conn: psycopg.Connection, eid: str, asin: str) -> int:
    row = conn.execute(
        "INSERT INTO task4.source_record (source_type, locator_key, locator, content_hash, acquired_at) "
        "VALUES ('page', %s, %s, %s, %s) RETURNING source_record_id",
        (f"page:{eid}:{asin}:{_hash(asin)}", Jsonb({"asin": asin, "extraction_id": eid,
                                                    "snapshot_path": f"snapshots/{asin}.html.gz"}),
         _hash(asin), ACQUIRED)).fetchone()
    return row[0]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _assert(conn, eid, norm, parser, entity_type, entity_id, field, ordinal, raw, normalized, section, page_ordinal,
            source_record_id, qualifier=None, rule_id="page.attribute") -> int:
    row = conn.execute(
        "INSERT INTO task4.field_assertion (extraction_id, parser_version, norm_version, entity_type, entity_id, "
        "field_name, ordinal, raw_value, normalized_value, qualifier, source_record_id, source_locator, rule_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING assertion_id",
        (eid, parser, norm, entity_type, entity_id, field, ordinal, raw, normalized, qualifier, source_record_id,
         Jsonb({"section": section, "ordinal": page_ordinal, "field_key": field}), rule_id)).fetchone()
    return row[0]


def seed(conn: psycopg.Connection, world: w7.World) -> Seeded:
    """写入一个案例世界：抽取与商品清单、任务 3 页面字段、IMDb 快照、任务 4 映射与断言、任务 5 人物映射、
    任务 6 上映版本。world.decoys 时再加入其他版本的干扰行。"""
    reset(conn)
    eid = EXTRACTION
    conn.execute("INSERT INTO public.source_file (source_file_id, path, source_url, verified_at, recorded_size_bytes, "
                 "measured_size_bytes, recorded_sha256, measured_sha256) VALUES (%s, 'x', 'u', now(), 10, 10, %s, %s)",
                 (_FILE, _FILE, _FILE))
    conn.execute("INSERT INTO public.extraction (extraction_id, source_file_id, parser_version, rules_version, config, "
                 "scope, sample_max_records, scope_complete, status, completed_at) "
                 "VALUES (%s, %s, 'p', 'r', '{}', 'sample', 100, true, 'complete', now())", (eid, _FILE))
    build = _hash("build")
    conn.execute("INSERT INTO task2.manifest_build (build_id, extraction_id, scope, url_template, url_template_version, "
                 "rule_version) VALUES (%s, %s, 'sample', 'x', 'v1', 'r1')", (build, eid))
    _snapshot(conn, SNAPSHOT, DOWNLOADED)
    _imdb(conn, SNAPSHOT, world.imdb)
    if world.decoys:
        _snapshot(conn, OLD_SNAPSHOT, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _imdb(conn, OLD_SNAPSHOT, [w7.Imdb(t.tconst, "Decoy " + t.primary, "Decoy " + (t.original or t.primary),
                                          (t.year or 2000) + 7, (t.runtime or 90) + 40, (), (), (("nm9999999", "Decoy Director"),))
                                   for t in world.imdb])
    # 任务 4 运行与映射版本
    for pmap_run, snap in (("t4fix", SNAPSHOT),):
        conn.execute("INSERT INTO task4.run (run_id, kind, extraction_id, t3_rule_version, parser_version, norm_version, "
                     "rule_version, imdb_snapshot_id, human_set_hash, input_key, status, finished_at) "
                     "VALUES (%s, 'match', %s, %s, %s, %s, 't4r2', %s, 'none', 'fix', 'succeeded', now())",
                     (pmap_run, eid, T3_RULE, PARSER_VERSION, NORM, snap))
    for mv in ([T4, "map0001"] if world.decoys else [T4]):
        conn.execute("INSERT INTO task4.mapping_version (mapping_version_id, extraction_id, run_id, input_key, "
                     "t3_rule_version, parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, "
                     "counts) VALUES (%s, %s, 't4fix', %s, %s, %s, %s, 't4r2', %s, 'none', '{}')",
                     (mv, eid, f"fix|{mv}", T3_RULE, PARSER_VERSION, NORM, SNAPSHOT))
    # 作品
    movies = {m.name: m for m in world.movies}
    for m in world.movies:
        mid = w7.movie_id(m.name)
        conn.execute("INSERT INTO task4.movie_identity (anchor_key, movie_id, created_run) VALUES (%s, %s, 't4fix')",
                     (f"fix:{m.name}", mid))
        linked = m.release.source == "imdb_t4_link"
        for mv in ([T4, "map0001"] if world.decoys else [T4]):
            conn.execute("INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, tconst, "
                         "display_title) VALUES (%s, %s, %s, %s, %s, %s)",
                         (mv, mid, "imdb_linked" if linked else "page_only", f"fix:{m.name}",
                          m.release.tconst if linked else None, m.name))
    # 商品：清单、任务 3 页面、任务 4 商品与关系、来源记录与断言
    run3 = "t3fix"
    conn.execute("INSERT INTO task3.classify_run (run_id, extraction_id, parser_version, label_map_version, "
                 "rule_version, status, finished_at) VALUES (%s, %s, %s, %s, %s, 'succeeded', now())",
                 (run3, eid, PARSER_VERSION, LABEL_MAP_VERSION, T3_RULE))
    run6 = "t6fix"
    for ed in world.editions:
        _seed_edition(conn, eid, build, run3, ed, world)
    seed_task5(conn, eid, world)
    _seed_task6(conn, eid, world, run6)
    if world.decoys:
        _seed_decoys(conn, eid, world)
    return Seeded(eid, world)


def _seed_edition(conn, eid, build, run3, ed: w7.Ed, world: w7.World) -> None:
    asin = ed.asin
    ch = _hash(asin)
    conn.execute(
        "INSERT INTO task2.product_manifest (extraction_id, asin, original_url, url_template_version, first_build_id, "
        "in_valid_records, records_total, records_valid, records_quarantined, first_record_no, last_record_no, status, "
        "final_url, http_status, attempt_count, fetched_at, snapshot_path, content_hash) "
        "VALUES (%s, %s, 'u', 'v1', %s, true, 1, 1, 0, 1, 1, 'success', %s, 200, 1, %s, %s, %s)",
        (eid, asin, build, f"https://example.invalid/dp/{asin}", ACQUIRED, f"snapshots/{asin}.html.gz", ch))
    conn.execute("INSERT INTO task3.page_parse (extraction_id, asin, parser_version, content_hash, snapshot_path, "
                 "final_url, fetched_at, attempt_no, parse_status, sections_present, field_count, run_id) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'parsed', '{}', 0, %s)",
                 (eid, asin, PARSER_VERSION, ch, f"snapshots/{asin}.html.gz", f"https://example.invalid/dp/{asin}",
                  ACQUIRED, run3))
    scope = "matched" if ed.kind in ("movie", "collection") else "not_applicable"
    conn.execute("INSERT INTO task4.product_edition (extraction_id, t3_rule_version, asin, product_kind, basis_level, "
                 "match_scope, parser_version, content_hash, final_url, fetched_at, run_id) "
                 "VALUES (%s, %s, %s, %s, 'structured', %s, %s, %s, %s, %s, 't4fix')",
                 (eid, T3_RULE, asin, ed.kind, scope, PARSER_VERSION, ch, f"https://example.invalid/dp/{asin}", ACQUIRED))
    if ed.movie:
        conn.execute("INSERT INTO task4.product_movie (mapping_version_id, asin, member_ordinal, movie_id, relation_type, "
                     "basis) VALUES (%s, %s, 0, %s, 'edition', %s)", (T4, asin, w7.movie_id(ed.movie), ed.basis))
    for ordinal, name, _title in ed.members:
        conn.execute("INSERT INTO task4.product_movie (mapping_version_id, asin, member_ordinal, movie_id, relation_type, "
                     "basis) VALUES (%s, %s, %s, %s, 'contains', %s)", (T4, asin, ordinal, w7.movie_id(name), ed.basis))
    rec = _page_source(conn, eid, asin)
    n = {"page": 0}

    def page_ordinal() -> int:
        n["page"] += 1
        return n["page"]

    def add(field, ordinal, raw, normalized, section, entity_type="product_edition", entity=None):
        return _assert(conn, eid, NORM, PARSER_VERSION, entity_type, entity or asin, field, ordinal, raw, normalized,
                       section, page_ordinal(), rec)

    if ed.title is not None:
        add("title", 1, ed.title, title_key(ed.title), "title")
    for ordinal, _name, member_title in ed.members:
        _assert(conn, eid, NORM, PARSER_VERSION, "collection_member", f"{asin}#{ordinal}", "title", 1,
                member_title, title_key(member_title), "title", 1, rec, rule_id="title.key")
    for k, (minutes, section) in enumerate(ed.runtime, start=1):
        add("runtime_min", k, f"{minutes} minutes", str(minutes), section)
    for k, (raw, section) in enumerate(ed.publisher, start=1):
        add("edition.publisher", k, raw, raw.casefold(), section)
    for k, (n_discs, section) in enumerate(ed.discs, start=1):
        add("edition.discs", k, str(n_discs), str(n_discs), section)
    for k, raw in enumerate(ed.packaging, start=1):
        add("edition.packaging", k, raw, raw.casefold(), "title")
    for field in ("media_format", "language", "subtitles", "aspect_ratio", "region", "cut_type", "video_system"):
        for k, (raw, section) in enumerate(getattr(ed, field), start=1):
            add(f"edition.{field}", k, raw, raw.casefold(), section)
    for k, (raw, section) in enumerate(ed.rating, start=1):
        conn.execute("INSERT INTO task3.page_field (extraction_id, asin, parser_version, section, ordinal, label_raw, "
                     "value_raw, field_key) VALUES (%s, %s, %s, %s, %s, 'Rated:', %s, 'mpaa_rating')",
                     (eid, asin, PARSER_VERSION, section, k, raw))
    for k, raw in enumerate(ed.swatch, start=1):
        conn.execute("INSERT INTO task3.page_field (extraction_id, asin, parser_version, section, ordinal, label_raw, "
                     "value_raw, field_key) VALUES (%s, %s, %s, 'format_swatch', %s, NULL, %s, 'format_swatch')",
                     (eid, asin, PARSER_VERSION, k, raw))


def seed_task5(conn: psycopg.Connection, eid: str, world: w7.World) -> None:
    """任务 5 人物映射版本与页面导演（movie_person 的 director 行）。"""
    conn.execute("INSERT INTO task5.run (run_id, kind, extraction_id, t4_mapping_version_id, parser_version, norm_version, "
                 "rule_version, imdb_snapshot_id, human_set_hash, input_key, status, finished_at) "
                 "VALUES ('t5fix', 'person', %s, %s, %s, 't5n1', 't5r1', %s, 'none', 'fix', 'succeeded', now())",
                 (eid, T4, PARSER_VERSION, SNAPSHOT))
    conn.execute("INSERT INTO task5.person_mapping_version (person_mapping_version_id, extraction_id, run_id, input_key, "
                 "t4_mapping_version_id, parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, "
                 "counts) VALUES (%s, %s, 't5fix', 'fix', %s, %s, 't5n1', 't5r1', %s, 'none', '{}')",
                 (PMAP, eid, T4, PARSER_VERSION, SNAPSHOT))
    for m in world.movies:
        for name, nconst in m.directors:
            pid = w7.person_id(name)
            if not conn.execute("SELECT 1 FROM task5.person WHERE person_mapping_version_id = %s AND person_id = %s",
                                (PMAP, pid)).fetchone():
                conn.execute("INSERT INTO task5.person_identity (anchor_key, person_id, created_run) "
                             "VALUES (%s, %s, 't5fix') ON CONFLICT DO NOTHING", (f"mention:{name}", pid))
                conn.execute("INSERT INTO task5.person (person_mapping_version_id, person_id, status, has_external_id, "
                             "external_route, anchor_key, canonical_name, canonical_source, mention_count, work_count) "
                             "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 1)",
                             (PMAP, pid, "external_verified" if nconst else "page_only", bool(nconst),
                              "credits" if nconst else None, f"mention:{name}", name, f"mention:{name}"))
                if nconst:
                    conn.execute("INSERT INTO task5.person_external_id (person_mapping_version_id, person_id, system, "
                                 "external_id, snapshot_id) VALUES (%s, %s, 'imdb', %s, %s)",
                                 (PMAP, pid, nconst, SNAPSHOT))
            conn.execute("INSERT INTO task5.movie_person (person_mapping_version_id, movie_id, person_id, role, "
                         "credit_order, source_labels, mention_count) VALUES (%s, %s, %s, 'director', 1, %s, 1)",
                         (PMAP, w7.movie_id(m.name), pid, ["byline:Director"]))


def _seed_task6(conn, eid, world: w7.World, run6: str) -> None:
    conn.execute("INSERT INTO task6.run (run_id, kind, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                 "parser_version, rule_version, imdb_snapshot_id, input_key, status, finished_at) "
                 "VALUES (%s, 'release', %s, %s, %s, %s, 't6r1', %s, 'fix', 'succeeded', now())",
                 (run6, eid, T4, PMAP, PARSER_VERSION, SNAPSHOT))
    _release_version(conn, eid, run6, RV, T4, world, primary=True)
    conn.execute("UPDATE task6.run SET release_version_id = %s WHERE run_id = %s", (RV, run6))
    # 商品发售日期：需要 task4 断言 ID（task6.product_release_date 引用 field_assertion）
    for ed in world.editions:
        if not ed.dates:
            continue
        rec = conn.execute("SELECT source_record_id FROM task4.source_record WHERE locator->>'asin' = %s",
                           (ed.asin,)).fetchone()[0]
        for k, (kind, iso, flags) in enumerate(ed.dates, start=1):
            aid = _assert(conn, eid, NORM, PARSER_VERSION, "product_edition", ed.asin, "edition.product_release_date",
                          k, iso, iso, "detail_bullets", 50 + k, rec)
            conn.execute("INSERT INTO task6.product_release_date (extraction_id, parser_version, assertion_id, asin, "
                         "kind, label_raw, raw_value, value_date, precision, flags, section, page_ordinal, "
                         "source_record_id, run_id) VALUES (%s, %s, %s, %s, %s, 'Release date', %s, %s, 'day', %s, "
                         "'detail_bullets', %s, %s, %s)", (eid, PARSER_VERSION, aid, ed.asin, kind, iso, iso, list(flags),
                                                           50 + k, rec, run6))
    # 共享 tconst 的任务 6 质量问题
    for movie, tconst, owner, origin in world.shared:
        detail = {"tconst": tconst, "owner_movie_id": w7.movie_id(owner)}
        if origin == "cooccur":
            detail["origin"] = "cooccur"
        else:
            detail.update(asin="B0X", mention_keys=[])
        conn.execute("INSERT INTO task6.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                     "VALUES (%s, %s, %s, 'shared_tconst_with_movie', %s)",
                     (run6, eid, w7.movie_id(movie), Jsonb(detail)))


def _release_version(conn, eid, run6, rv, t4, world: w7.World, *, primary: bool, year_shift: int = 0) -> None:
    conn.execute("INSERT INTO task6.release_version (release_version_id, extraction_id, run_id, input_key, "
                 "t4_mapping_version_id, t5_person_mapping_version_id, parser_version, rule_version, imdb_snapshot_id, "
                 "counts) VALUES (%s, %s, %s, %s, %s, %s, %s, 't6r1', %s, '{}')",
                 (rv, eid, run6, f"fix|{rv}", t4, PMAP, PARSER_VERSION, SNAPSHOT))
    imdb_of = {t.tconst: t for t in world.imdb}
    stage = f"fix|{rv}"
    for m in world.movies:
        r, mid = m.release, w7.movie_id(m.name)
        ids: list[int] = []
        selected: int | None = None
        year = None
        if r.source in w7.IMDB_SOURCES:
            y = (imdb_of[r.tconst].year if r.imdb_year is None else r.imdb_year)
            if y is not None:
                y += year_shift
                selected = _cand(conn, eid, stage, mid, r.source, r.tconst, y, 0.95 if r.source == "imdb_t4_link" else
                                 (0.80 if r.source == "imdb_t5_hint" else 0.75), run6)
                ids.append(selected)
                year = y
        for k, ty in enumerate(r.title_years, start=1):
            cid = _cand(conn, eid, stage, mid, "amazon_title_year", f"B0T:{k}", ty + year_shift, 0.60, run6)
            ids.append(cid)
            if r.source == "page_reported" and selected is None:
                selected, year = cid, ty + year_shift
        status = w7.STATUS_OF_SOURCE.get(r.source or "", "missing") if selected else "missing"
        conn.execute(
            "INSERT INTO task6.release_selection (release_version_id, movie_id, status, missing_reason, release_year, "
            "precision, release_type, regional_release_missing_reason, selected_candidate_id, considered_candidate_ids, "
            "selection_rule, reason, confidence) VALUES (%s, %s, %s, %s, %s, 'year', 'first_release', "
            "'no_permitted_dated_source', %s, %s, 'fix', 'fix', 0.9)",
            (rv, mid, status, (r.missing_reason or "no_candidate") if status == "missing" else None, year, selected, ids))
        for evidence, iso in r.bounds:
            conn.execute("INSERT INTO task6.bound_evidence (release_version_id, movie_id, evidence, evidence_time, "
                         "evidence_precision, passed, failed_checks, confidence) VALUES (%s, %s, %s, %s, 'day', "
                         "false, '{}', 0.6)", (rv, mid, evidence, iso))
        if r.inferred:
            iso, precision, evidence = r.inferred
            conn.execute("INSERT INTO task6.inferred_release_bound (release_version_id, movie_id, bound_time, "
                         "bound_precision, bound_kind, chosen_evidence, confidence, status, rule_version) "
                         "VALUES (%s, %s, %s, %s, 'not_later_than', %s, 0.6, 'inferred', 't6r1')",
                         (rv, mid, iso, precision, evidence))


def _cand(conn, eid, stage, mid, source, source_key, year, confidence, run6) -> int:
    return conn.execute(
        "INSERT INTO task6.release_candidate (extraction_id, stage_key, movie_id, source, source_key, raw_value, "
        "value_year, precision, semantic, source_ref, source_acquired_at, confidence, rule_version, run_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'year', 'first_release', %s, %s, %s, 't6r1', %s) RETURNING candidate_id",
        (eid, stage, mid, source, source_key, str(year), year, Jsonb({"fixture": True}), ACQUIRED, confidence,
         run6)).fetchone()[0]


def _seed_decoys(conn, eid, world: w7.World) -> None:
    """其他版本的干扰行：映射版本 map0001（商品挂到别的作品）、上映版本 rv0002（年份整体偏移）、
    规范化版本 t4n0 与解析版本 t3p0 的断言（片长与标题不同）。任务 7 只应读 map0002、rv0001、t4n1、t3p1。"""
    conn.execute("INSERT INTO task6.run (run_id, kind, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                 "parser_version, rule_version, imdb_snapshot_id, input_key, status, finished_at) "
                 "VALUES ('t6decoy', 'release', %s, 'map0001', %s, %s, 't6r1', %s, 'decoy', 'succeeded', now())",
                 (eid, PMAP, PARSER_VERSION, SNAPSHOT))
    _release_version(conn, eid, "t6decoy", "rv0002", "map0001", world, primary=False, year_shift=9)
    other = w7.movie_id("Decoy Movie")
    conn.execute("INSERT INTO task4.movie_identity (anchor_key, movie_id, created_run) VALUES ('fix:Decoy', %s, 't4fix')",
                 (other,))
    conn.execute("INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, display_title) "
                 "VALUES ('map0001', %s, 'page_only', 'fix:Decoy', 'Decoy Movie')", (other,))
    for ed in world.editions:
        if ed.movie:
            conn.execute("INSERT INTO task4.product_movie (mapping_version_id, asin, member_ordinal, movie_id, "
                         "relation_type, basis) VALUES ('map0001', %s, 0, %s, 'edition', 'singleton')", (ed.asin, other))
            rec = conn.execute("SELECT source_record_id FROM task4.source_record WHERE locator->>'asin' = %s",
                               (ed.asin,)).fetchone()[0]
            _assert(conn, eid, "t4n0", PARSER_VERSION, "product_edition", ed.asin, "runtime_min", 1, "999 minutes", "999",
                    "detail_bullets", 1, rec)
            _assert(conn, eid, NORM, "t3p0", "product_edition", ed.asin, "runtime_min", 1, "888 minutes", "888",
                    "detail_bullets", 1, rec)
            _assert(conn, eid, "t4n0", PARSER_VERSION, "product_edition", ed.asin, "title", 1, "Decoy Title [DVD]",
                    "decoy title", "title", 1, rec)


# ---------------------------------------------------------------- 运行与查询

def run_stages(conn: psycopg.Connection, seeded: Seeded, *, upto: str = "movie_scalar", rules=None,
               batch_size: int = DEFAULT_BATCH, before_commit=None, ctx: RunContext | None = None,
               **over: Any) -> RunContext:
    """按依赖链逐阶段运行（测试用的直通驱动；runner 在 T7-05 实现）：edition_resolve → link_assess → movie_scalar。"""
    from etl_task7 import editions, link_assess, movies_scalar

    ctx = ctx or start_run(conn, seeded.inputs(**over))
    rules = rules if rules is not None else ruleset(ctx.inputs.rule_version)
    order = ("edition_resolve", "link_assess", "movie_scalar")
    for stage in order[:order.index(upto) + 1]:
        if stage == "edition_resolve":
            editions.resolve_editions(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        elif stage == "link_assess":
            link_assess.assess_links(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        else:
            movies_scalar.resolve_movies_scalar(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
    return ctx


def movie_sel(conn: psycopg.Connection, ctx: RunContext, seeded: Seeded, movie: str, field: str) -> dict[str, Any]:
    """作品层某字段的选择记录（movie_scalar 阶段结果里的 field_selections）。"""
    row = conn.execute("SELECT field_selections -> %s FROM task7.movie_scalar_stage_result WHERE extraction_id = %s "
                       "AND stage_key = %s AND movie_id = %s",
                       (field, ctx.inputs.extraction_id, ctx.inputs.stage_key("movie_scalar"), seeded.mid(movie))).fetchone()
    assert row is not None, f"作品 {movie} 没有 movie_scalar 阶段结果"
    return row[0]


def edition_sel(conn: psycopg.Connection, ctx: RunContext, asin: str, field: str) -> dict[str, Any]:
    row = conn.execute("SELECT field_selections -> %s FROM task7.edition_stage_result WHERE extraction_id = %s "
                       "AND stage_key = %s AND asin = %s",
                       (field, ctx.inputs.extraction_id, ctx.inputs.stage_key("edition_resolve"), asin)).fetchone()
    assert row is not None, f"商品 {asin} 没有 edition_resolve 阶段结果"
    return row[0]


def candidates(conn: psycopg.Connection, ctx: RunContext, stage: str, entity_id: str, field: str) -> list[dict[str, Any]]:
    """某实体某字段的全部候选（按 candidate_id）：字典列表。"""
    cols = ("candidate_id", "source", "source_key", "raw_value", "normalized_value", "precision", "source_ref",
            "source_acquired_at", "match_reliability", "status", "status_reason")
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s "
                        "AND entity_id = %s AND field_name = %s ORDER BY candidate_id",
                        (ctx.inputs.extraction_id, ctx.inputs.stage_key(stage), entity_id, field)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["match_reliability"] = float(d["match_reliability"])
        out.append(d)
    return out


def status_of(cands: list[dict[str, Any]]) -> dict[str, str]:
    """source_key → 候选状态，便于断言。"""
    return {c["source_key"]: c["status"] for c in cands}


UPSTREAM_TABLES = ("task3.page_field", "task4.field_assertion", "task4.product_movie", "task4.product_edition",
                   "task4.movie", "task4.source_record", "task4.imdb_title_key", "task5.movie_person", "task5.person",
                   "task6.release_selection", "task6.release_candidate", "task6.product_release_date",
                   "task6.bound_evidence", "task6.quality_issue", "imdb.title", "imdb.title_aka",
                   "imdb.title_director", "imdb.name")


def upstream_digest(conn: psycopg.Connection) -> dict[str, tuple[int, str]]:
    """上游表的行数与内容摘要：AC02 用它证明任务 7 没有改动上游行（spec R07）。"""
    out = {}
    for t in UPSTREAM_TABLES:
        out[t] = conn.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                              f"FROM {t} x").fetchone()
    return out
