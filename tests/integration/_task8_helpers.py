r"""任务 8 集成测试辅助：把一组 `task8.world.Rec` 写成 gzip，用任务 1 的装载器装进 etl_test（上游表由任务 1 的代码
生成，与正式库同形），再按需逐阶段运行任务 8。每个测试开始时 `reset` 重建 task8 表结构。

上游表的写入只存在于测试里（etl_test）；任务 8 代码只读任务 1 的表。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import build_fixtures as bf
from etl_task1.loader import load_extraction
from etl_task1.source import verify_source
from etl_task2.crawl.crawler import apply_crawl_schema
from etl_task3.classify import apply_task3_schema
from etl_task4.imdb import apply_imdb_schema
from etl_task4.run import apply_task4_schema
from etl_task5.run import apply_task5_schema
from etl_task6.run import apply_task6_schema
from etl_task7.run import apply_task7_schema
from etl_task8.rules import RuleSet
from etl_task8.run import RunInputs, apply_task8_schema
from etl_task8.runner import execute_dedup, run_stages
from task8.world import Rec

PUBLIC_TABLES = ("review_record", "review_staging", "quarantine_record", "quality_issue", "etl_batch", "extraction")


def reset(conn: psycopg.Connection) -> None:
    """重建 task8（它的大表不引用 public.extraction，不随 conn 夹具清空）；前置的任务 2–7 结构按需建好。"""
    for apply in (apply_crawl_schema, apply_task3_schema, apply_imdb_schema, apply_task4_schema, apply_task5_schema,
                  apply_task6_schema, apply_task7_schema):
        apply(conn)
    conn.execute("DROP SCHEMA IF EXISTS task8 CASCADE")
    conn.execute("DELETE FROM public.schema_migration WHERE version = '701'")
    apply_task8_schema(conn)


def load(conn: psycopg.Connection, make_settings, tmp_path: Path, recs: list[Rec], *, name: str = "full",
         sample_max: int | None = None) -> str:
    """装载一个抽取，返回 extraction_id。sample_max 给出时按样本范围装载同一份数据（另一个抽取）。"""
    src = tmp_path / f"t8src_{name}"
    src.mkdir(parents=True, exist_ok=True)
    gz = bf.gzip_bytes(bf.build_bytes([r.lines() for r in recs]))
    (src / "s.txt.gz").write_bytes(gz)
    bf.write_manifest(src / "s.manifest.json", gz)
    over: dict[str, Any] = {"source_gzip_path": src / "s.txt.gz", "source_manifest_path": src / "s.manifest.json"}
    if sample_max is not None:
        over.update(scope="sample", sample_max_records=sample_max)
    settings = make_settings(**over)
    return load_extraction(settings, verify_source(src / "s.txt.gz", src / "s.manifest.json"), conn).extraction_id


def setup(conn: psycopg.Connection, make_settings, tmp_path: Path, recs: list[Rec], **kw) -> str:
    reset(conn)
    return load(conn, make_settings, tmp_path, recs, **kw)


def run(conn: psycopg.Connection, eid: str, through: str, rules: RuleSet | None = None, **kw) -> dict[str, Any]:
    inputs = RunInputs(extraction_id=eid, rule_version=rules.version if rules else RunInputs().rule_version)
    return run_stages(conn, inputs, rules, through=through, **kw)


def record_nos(conn: psycopg.Connection, eid: str) -> dict[int, str]:
    """record_no → source_review_id（全部原始记录）。"""
    return dict(conn.execute("SELECT record_no, source_review_id FROM public.review_record WHERE extraction_id = %s",
                             (eid,)).fetchall())


def public_digest(conn: psycopg.Connection) -> dict[str, tuple[int, str]]:
    """任务 1 各表的行数与内容摘要：任务 8 运行前后必须相同（AC01）。"""
    out = {}
    for t in PUBLIC_TABLES:
        out[t] = conn.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                              f"FROM public.{t} x").fetchone()
    return out


def stage_key(conn: psycopg.Connection, eid: str, stage: str) -> str:
    """该抽取下某阶段最近登记的短键（测试里每个阶段只有一个规则版本时唯一）。"""
    return conn.execute("SELECT stage_key FROM task8.stage_key WHERE extraction_id = %s AND stage = %s "
                        "ORDER BY created_at DESC LIMIT 1", (eid, stage)).fetchone()[0]


# ---------------------------------------------------------------- 归属阶段的上游：任务 2 清单、任务 3 分类、任务 7 版本

SNAPSHOT, T4, PMAP, RV = "imdb_2026-10-29", "map0002", "pmap0001", "rv0001"
T7_RUN = "t8fix_t7run"
CATEGORIES = ("movie", "collection", "tv_series", "concert_other", "non_movie", "pending", "not_collected")
# 与正式库 t3r2 的真实组合一致（2026-11-03 只读查询）：(input_group, category, basis_level, rule_id, pending_reason)
_CLASS = {"movie": ("dp_page", "movie", "inferred", "MV2", None),
          "collection": ("dp_page", "collection", "inferred", "CL2", None),
          "tv_series": ("dp_page", "tv_series", "structured", "TV1", None),
          "concert_other": ("dp_page", "concert_other", "inferred", "CO2", None),
          "non_movie": ("dp_page", "non_movie", "structured", "NM1", None),
          "pending": ("dp_page", "pending", "missing", "PD1", "insufficient_evidence"),
          "not_collected": ("not_collected", "pending", "missing", "NP1", "not_collected")}


@dataclass(frozen=True)
class Product:
    """一个商品在上游的形态：t3r2 类别（not_collected 为未采集的待确认），以及 cv 的 movie_ids（单品 1 个，合集可为 0 个或多个）。"""
    asin: str
    category: str
    movies: tuple[str, ...] = ()


def mid(name: str) -> str:
    """合成作品 ID（与任务 4 的 mv_ + 16 位十六进制同形）。"""
    return "mv_" + hashlib.sha256(name.encode()).hexdigest()[:16]


def seed_upstream(conn: psycopg.Connection, eid: str, products: list[Product],
                  dups: list[tuple[str, str]] = (), *, cv: str = "cv0003", status: str = "verified",
                  base: bool = True) -> str:
    """写入归属阶段读的上游表，形状与正式库相同：task2.product_manifest（计数取自任务 1 的 review_record）、
    task3.page_parse 与 product_classification（t3r2）、任务 4–6 的版本行与 IMDb 快照（只为满足外键）、task7.run、
    conflict_version（verified，link_stage_key 为长串）、edition_resolved（movie_ids 为 text[]）、shared_tconst_result
    （likely_duplicate_movie，带 stage_key 与来源问题）。base=False 时只再加一个 cv 版本（另一组商品与重复作品对）。
    返回该 cv 版本的 link_stage_key。"""
    if base:
        _seed_base(conn, eid, products)
    link = f"{eid}|{T4}|{RV}|t3p1|t4n1|e00000000000|{PMAP}|{SNAPSHOT}|l{hashlib.sha256(cv.encode()).hexdigest()[:12]}"
    conn.execute("INSERT INTO task7.conflict_version (conflict_version_id, extraction_id, run_id, input_key, "
                 "t4_mapping_version_id, t5_person_mapping_version_id, t6_release_version_id, parser_version, "
                 "norm_version, imdb_snapshot_id, rule_version, edition_stage_key, link_stage_key, scalar_stage_key, "
                 "multivalue_stage_key, status, verified_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 't3p1', 't4n1', %s, "
                 "'t7r3', %s, %s, %s, %s, %s, %s)",
                 (cv, eid, T7_RUN, f"fix|{cv}", T4, PMAP, RV, SNAPSHOT, f"e|{cv}", link, f"s|{cv}", f"m|{cv}", status,
                  datetime.now(timezone.utc) if status == "verified" else None))
    for p in products:
        if p.category in ("movie", "collection"):
            conn.execute("INSERT INTO task7.edition_resolved (conflict_version_id, asin, product_kind, movie_ids) "
                         "VALUES (%s, %s, %s, %s)", (cv, p.asin, p.category, list(p.movies)))
    for n, (a, b) in enumerate(dups, start=1):
        conn.execute("INSERT INTO task7.shared_tconst_result (extraction_id, stage_key, tconst, movie_id, other_movie_id, "
                     "link_class, basis, source_issue_ids, run_id) VALUES (%s, %s, %s, %s, %s, 'likely_duplicate_movie', "
                     "%s, %s, %s)", (eid, link, f"tt{9000000 + n:07d}", a, b,
                                     Jsonb({"owners": [b], "origins": ["cooccur"]}), [n], T7_RUN))
    # 干扰：同一 link_stage_key 下的其他类别不进统计组；别的 stage_key 下的 likely_duplicate_movie 不被读到
    conn.execute("INSERT INTO task7.shared_tconst_result (extraction_id, stage_key, tconst, movie_id, other_movie_id, "
                 "link_class, basis, source_issue_ids, run_id) VALUES (%s, %s, 'tt8999999', 'mv_decoy_a', 'mv_decoy_b', "
                 "'false_positive_link', '{}', '{99}', %s), (%s, %s, 'tt8999998', 'mv_decoy_c', 'mv_decoy_d', "
                 "'likely_duplicate_movie', '{}', '{98}', %s)", (eid, link, T7_RUN, eid, link + "|other", T7_RUN))
    return link


def _seed_base(conn: psycopg.Connection, eid: str, products: list[Product]) -> None:
    from etl_task3 import LABEL_MAP_VERSION, PARSER_VERSION
    conn.execute("INSERT INTO imdb.snapshot (snapshot_id, base_url, downloaded_at, status, loader_version, loaded_at) "
                 "VALUES (%s, 'https://example.invalid/', now(), 'loaded', 't8fixture', now()) ON CONFLICT DO NOTHING",
                 (SNAPSHOT,))
    build = hashlib.sha256(f"build|{eid}".encode()).hexdigest()
    conn.execute("INSERT INTO task2.manifest_build (build_id, extraction_id, scope, url_template, url_template_version, "
                 "rule_version) VALUES (%s, %s, 'full', 'x', 'v1', 'r1')", (build, eid))
    conn.execute("INSERT INTO task3.classify_run (run_id, extraction_id, parser_version, label_map_version, rule_version, "
                 "status, finished_at) VALUES ('t8fix_t3', %s, %s, %s, 't3r2', 'succeeded', now())",
                 (eid, PARSER_VERSION, LABEL_MAP_VERSION))
    counts = {a: row for a, *row in conn.execute(
        "SELECT r.product_id_observed, count(*), count(*) FILTER (WHERE r.classification = 'valid'), "
        "count(*) FILTER (WHERE r.classification = 'quarantined'), min(r.record_no), max(r.record_no) "
        "FROM public.review_record r WHERE r.extraction_id = %s AND r.product_id_observed IS NOT NULL GROUP BY 1", (eid,))}
    for p in products:
        assert p.category in CATEGORIES, p.category
        total, valid, quar, first, last = counts[p.asin]       # 每个上游商品都有评论（与任务 2 清单的来源一致）
        collected = p.category != "not_collected"
        ch = hashlib.sha256(p.asin.encode()).hexdigest()
        conn.execute(
            "INSERT INTO task2.product_manifest (extraction_id, asin, original_url, url_template_version, first_build_id, "
            "in_valid_records, records_total, records_valid, records_quarantined, first_record_no, last_record_no, status, "
            "final_url, http_status, attempt_count, fetched_at, snapshot_path, content_hash) "
            "VALUES (%s, %s, %s, 'v1', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (eid, p.asin, f"https://example.invalid/dp/{p.asin}", build, valid > 0, total, valid, quar, first, last,
             "success" if collected else "not_collected", f"https://example.invalid/dp/{p.asin}" if collected else None,
             200 if collected else None, 1 if collected else 0, datetime.now(timezone.utc) if collected else None,
             f"snapshots/{p.asin}.html.gz" if collected else None, ch if collected else None))
        group, category, basis, rule_id, reason = _CLASS[p.category]
        parser = PARSER_VERSION if group == "dp_page" else None
        if parser:
            conn.execute("INSERT INTO task3.page_parse (extraction_id, asin, parser_version, content_hash, snapshot_path, "
                         "final_url, fetched_at, attempt_no, parse_status, sections_present, field_count, run_id) "
                         "VALUES (%s, %s, %s, %s, %s, %s, now(), 1, 'parsed', '{}', 0, 't8fix_t3')",
                         (eid, p.asin, parser, ch, f"snapshots/{p.asin}.html.gz", f"https://example.invalid/dp/{p.asin}"))
        conn.execute("INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, category, "
                     "basis_level, rule_id, pending_reason, parser_version, run_id) "
                     "VALUES (%s, %s, 't3r2', %s, %s, %s, %s, %s, %s, 't8fix_t3')",
                     (eid, p.asin, group, category, basis, rule_id, reason, parser))
    conn.execute("INSERT INTO task4.run (run_id, kind, extraction_id, t3_rule_version, parser_version, norm_version, "
                 "rule_version, imdb_snapshot_id, human_set_hash, input_key, status, finished_at) "
                 "VALUES ('t8fix_t4', 'match', %s, 't3r2', %s, 't4n1', 't4r2', %s, 'none', 'fix', 'succeeded', now())",
                 (eid, PARSER_VERSION, SNAPSHOT))
    conn.execute("INSERT INTO task4.mapping_version (mapping_version_id, extraction_id, run_id, input_key, t3_rule_version, "
                 "parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, counts) "
                 "VALUES (%s, %s, 't8fix_t4', 'fix', 't3r2', %s, 't4n1', 't4r2', %s, 'none', '{}')",
                 (T4, eid, PARSER_VERSION, SNAPSHOT))
    conn.execute("INSERT INTO task5.run (run_id, kind, extraction_id, t4_mapping_version_id, parser_version, norm_version, "
                 "rule_version, imdb_snapshot_id, human_set_hash, input_key, status, finished_at) "
                 "VALUES ('t8fix_t5', 'person', %s, %s, %s, 't5n1', 't5r1', %s, 'none', 'fix', 'succeeded', now())",
                 (eid, T4, PARSER_VERSION, SNAPSHOT))
    conn.execute("INSERT INTO task5.person_mapping_version (person_mapping_version_id, extraction_id, run_id, input_key, "
                 "t4_mapping_version_id, parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, "
                 "counts) VALUES (%s, %s, 't8fix_t5', 'fix', %s, %s, 't5n1', 't5r1', %s, 'none', '{}')",
                 (PMAP, eid, T4, PARSER_VERSION, SNAPSHOT))
    conn.execute("INSERT INTO task6.run (run_id, kind, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                 "parser_version, rule_version, imdb_snapshot_id, input_key, status, finished_at) "
                 "VALUES ('t8fix_t6', 'release', %s, %s, %s, %s, 't6r1', %s, 'fix', 'succeeded', now())",
                 (eid, T4, PMAP, PARSER_VERSION, SNAPSHOT))
    conn.execute("INSERT INTO task6.release_version (release_version_id, extraction_id, run_id, input_key, "
                 "t4_mapping_version_id, t5_person_mapping_version_id, parser_version, rule_version, imdb_snapshot_id, "
                 "counts) VALUES (%s, %s, 't8fix_t6', 'fix', %s, %s, %s, 't6r1', %s, '{}')",
                 (RV, eid, T4, PMAP, PARSER_VERSION, SNAPSHOT))
    conn.execute("INSERT INTO task7.run (run_id, kind, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                 "t6_release_version_id, parser_version, norm_version, imdb_snapshot_id, rule_version, input_key, status, "
                 "finished_at) VALUES (%s, 'resolve', %s, %s, %s, %s, 't3p1', 't4n1', %s, 't7r3', 'fix', 'succeeded', now())",
                 (T7_RUN, eid, T4, PMAP, RV, SNAPSHOT))


def inputs(eid: str, rules: RuleSet | None = None, **over: Any) -> RunInputs:
    return RunInputs(extraction_id=eid, rule_version=rules.version if rules else RunInputs().rule_version, **over)


def execute(conn: psycopg.Connection, eid: str, rules: RuleSet | None = None, *, cv: str = "cv0003", **kw):
    """完整运行（四个阶段、物化版本、核对）。"""
    return execute_dedup(conn, inputs(eid, rules, conflict_version_id=cv), rules, **kw)


def version(conn: psycopg.Connection, rd: str) -> dict[str, Any]:
    cols = ("clean_stage_key", "family_stage_key", "dedup_stage_key", "attribution_stage_key", "status", "counts")
    return dict(zip(cols, conn.execute(f"SELECT {', '.join(cols)} FROM task8.dedup_version "
                                       "WHERE review_dedup_version_id = %s", (rd,)).fetchone()))


# ---------------------------------------------------------------- 覆盖 R44 全部实例类别的小世界（报告、DAG 测试与 e2e 种子共用）

def small_world() -> tuple[list[Rec], list[Product], list[tuple[str, str]]]:
    """返回 (源记录, 上游商品, likely_duplicate_movie 对)。含 D1、D2、D3、多 ASIN 的 weak_support 家族、C1、家族内与
    统计组内的 C2、helpfulness 异常、不完整与隔离记录、合集评论、跨作品共享与多作品统计组。全部为合成数据。"""
    from task8.world import copies, unix
    e = {k: mid(f"E{k}") for k in range(1, 8)}
    w20 = ("this long film was one wonderful journey through history with great acting beautiful sets and moving "
           "music plus clever writing").split()
    a1, a2, c1, t1, n1 = "B0000E2EA1", "B0000E2EA2", "B0000E2EC1", "B0000E2ET1", "B0000E2EN1"
    s = {k: f"B0000E2ES{k}" for k in range(1, 6)}
    w1, w2, q1, q2 = "B0000E2EW1", "B0000E2EW2", "B0000E2EQ1", "B0000E2EQ2"
    recs: list[Rec] = []
    for i in range(4):                                   # A1、A2、C1 同一家族（单品与合集）
        recs += copies([a1, a2, c1], f"EF{i}", text=f"filler review number {i}", time=unix(i))
    recs += copies([a1, a2], "EL1", text="platform copy on both singles")                 # D3
    recs += [Rec(c1, "EL2", text="only on the collection")]                               # 只在合集
    recs += copies([c1, a1], "EL3", text="on collection and single")                      # 合集与单品
    recs += [Rec(t1, "EL4", text="tv series review"), Rec(n1, "EL5", text="not collected product review")]
    recs += [Rec(s[1], "EU1", score="5.0", text="u1 on s1"), Rec(s[1], "EU2", score="3.0", text="u2 on s1"),
             Rec(s[1], "EU7", score="5.0", text=" ".join(w20)),
             Rec(s[2], "EU3", score="4.0", text="u3 on s2"),
             Rec(s[2], "EU7", score="3.0", text=" ".join(w20[:-1]), time=unix(3)),          # 统计组内 C2
             *copies([s[3], s[4]], "EUX", score="5.0", text="shared by two movies"),       # 跨作品共享
             Rec(s[3], "EU6", score="1.0", text="only on s3"),
             Rec(s[5], "EU8", score="4.0", text="exact twice"), Rec(s[5], "EU8", score="4.0", text="exact twice"),  # D1
             Rec(s[5], "EU9", score="2.0", text="c"),
             Rec(s[5], "EU10", helpfulness="1/2", text="votes differ"),
             Rec(s[5], "EU10", helpfulness="3/4", text="votes differ"),                      # D2
             Rec(s[5], "EU11", helpfulness="8/1", text="eight of one"),                     # helpfulness 异常
             Rec(s[5], "EU12", helpfulness=None, score=None, time=None, summary=None, text=None),   # 不完整
             Rec(s[5], "EU13", raw_text=b"bad \xff bytes")]                                 # 隔离 invalid_utf8
    recs += copies([w1, w2], "EU14", text="single shared review")                          # weak_support 多 ASIN 家族
    recs += [Rec(q1, f"EQ1{i}", text=f"q1 own {i}") for i in range(3)]
    recs += [Rec(q2, f"EQ2{i}", text=f"q2 own {i}") for i in range(3)]
    recs += copies([q1, q2], "EUC", text="We love it!")                                   # C1
    recs += [Rec(q1, "EU15", text=" ".join(w20)), Rec(q1, "EU15", text=" ".join(w20[:-1]), time=unix(4))]  # 家族内 C2
    products = [Product(a1, "movie", (e[1],)), Product(a2, "movie", (e[1],)), Product(c1, "collection", (e[1], e[2])),
                Product(t1, "tv_series"), Product(n1, "not_collected"),
                *(Product(s[k], "movie", (e[k + 2],)) for k in range(1, 6)),
                Product(w1, "concert_other"), Product(w2, "concert_other"), Product(q1, "pending"), Product(q2, "pending")]
    return recs, products, [(e[3], e[4])]


def seed_small_world(conn: psycopg.Connection, make_settings, tmp_path: Path) -> str:
    recs, products, dups = small_world()
    eid = setup(conn, make_settings, tmp_path, recs)
    seed_upstream(conn, eid, products, dups)
    return eid
