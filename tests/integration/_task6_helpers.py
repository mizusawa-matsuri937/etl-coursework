r"""任务 6 集成测试辅助：合成抽取（评论时间与正文自定义）、装载合成 IMDb、真正运行任务 4 匹配、
写任务 5 结构的人物映射版本与升级线索，然后按需逐阶段运行任务 6。

任务 4 无法自动产生三种形态（startYear 为 \N 的关联、作品内标题年份 1985/1987 与 1990/1994 的分歧），
由 helper 在匹配后直接改写 task4 的表（测试夹具，非正式库）；真实数据里没有这些形态，见 design 的实测说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import build_fixtures as bf
import _task4_helpers as t4h
import _task3_helpers as t3h
from task6 import world as w6
from etl_task1.loader import load_extraction
from etl_task1.source import verify_source
from etl_task2.manifest import build_manifest
from etl_task3 import LABEL_MAP_VERSION, PARSER_VERSION, RULE_VERSION as T3_RULE
from etl_task2.crawl.crawler import apply_crawl_schema
from etl_task3.classify import apply_task3_schema
from etl_task4 import match as t4m
from etl_task4.runner import RunResult, execute_match
from etl_task5.run import apply_task5_schema
from etl_task6.rules import ruleset
from etl_task6.run import DEFAULT_BATCH, RunContext, RunInputs, apply_task6_schema, finish_run, start_run


@dataclass
class World:
    fx: object                 # _task3_helpers.Fixture
    sid: str                   # IMDb 快照
    t4: RunResult              # 任务 4 匹配结果

    @property
    def mapping(self) -> str:
        return self.t4.mapping_version_id

    @property
    def extraction_id(self) -> str:
        return self.fx.eid

    pmap_with_hints = "pmap0001"     # 线索的阶段键有线索
    pmap_alt = "pmap0002"            # t5r9 的阶段键没有线索


def reset_task6(conn: psycopg.Connection) -> None:
    """task6 的表不随 conn 夹具清空（本任务自己建的迁移），每个测试开始时手动清。"""
    apply_task6_schema(conn)
    tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'task6'").fetchall()]
    conn.execute("TRUNCATE " + ", ".join(f"task6.{t}" for t in tables) + " RESTART IDENTITY CASCADE")


def _iso_to_unix(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def _movie_of(conn: psycopg.Connection, mapping: str, asin: str, ordinal: int = 0) -> str:
    return conn.execute("SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s "
                        "AND member_ordinal = %s", (mapping, asin, ordinal)).fetchone()[0]


def make_world(conn: psycopg.Connection, make_settings, tmp_path: Path, *, name: str = "a",
               date: str = "2026-10-31") -> World:
    """装一个任务 6 世界：合成抽取 → 任务 3 结构 → IMDb → 任务 4 匹配 → 形态改造 → 任务 5 线索 → 清空 task6。"""
    products = w6.products()
    conn.execute("DROP SCHEMA IF EXISTS task6 CASCADE")
    conn.execute("DELETE FROM public.schema_migration WHERE version = '501'")   # 与 DROP 配套，见 runbook 的清空做法
    from etl_task2.manifest import apply_task2_schema
    apply_task2_schema(conn)

    # 抽取：评论的时间与正文按夹具（其余与 _task3_helpers.setup 一致）
    records = []
    plan, quarantine = w6.review_plan(), w6.quarantined_plan()
    for p in products:
        if p.asin in quarantine:                             # 只写隔离记录（含 NUL 字节），商品进清单但没有有效评论
            for j, (iso, text) in enumerate(quarantine[p.asin]):
                records.append(bf.record(product=p.asin.encode(), user=f"Q{j}_{p.asin}".encode(),
                                         time=str(_iso_to_unix(iso)).encode(), text=text.encode() + b"\x00", score=b"6"))
            continue
        for j, (iso, text) in enumerate(plan.get(p.asin, [])):
            records.append(bf.record(product=p.asin.encode(), user=f"U{j}_{p.asin}".encode(),
                                     time=str(_iso_to_unix(iso)).encode(), text=text.encode()))
    records.append(bf.record(product=None, user=b"NO_PRODUCT"))
    src = tmp_path / f"t6src_{name}"
    src.mkdir(parents=True, exist_ok=True)
    gz = bf.gzip_bytes(bf.build_bytes(records))
    (src / "s.txt.gz").write_bytes(gz)
    bf.write_manifest(src / "s.manifest.json", gz)
    settings = make_settings(source_gzip_path=src / "s.txt.gz", source_manifest_path=src / "s.manifest.json")
    eid = load_extraction(settings, verify_source(src / "s.txt.gz", src / "s.manifest.json"), conn).extraction_id
    build_manifest(conn, eid, allow_sample=True)
    apply_crawl_schema(conn)
    apply_task3_schema(conn)
    t4h.reset_task4(conn)
    fx = t3h.Fixture(eid, tmp_path / f"snapshots_{name}",
                     [t3h.Product(p.asin, "dp_page" if p.kind != "nopage" else p.group,
                                  None if p.kind == "nopage" else f"<html>{p.asin}</html>".encode(), p.reviews)
                      for p in products], len(records))
    t3h.simulate_crawl(conn, fx)

    # 任务 3 行（t4h.build 的后半段）
    run = f"t6fix_{name}"
    conn.execute("INSERT INTO task3.classify_run (run_id, extraction_id, parser_version, label_map_version, "
                 "rule_version, status, finished_at) VALUES (%s, %s, %s, %s, %s, 'succeeded', now())",
                 (run, eid, PARSER_VERSION, LABEL_MAP_VERSION, T3_RULE))
    for p in products:
        rows = t4h.page_fields(p)
        m = conn.execute("SELECT content_hash, snapshot_path, final_url, fetched_at FROM task2.product_manifest "
                         "WHERE extraction_id = %s AND asin = %s", (eid, p.asin)).fetchone()
        if m is None:
            raise RuntimeError(f"商品 {p.asin} 不在清单里")
        conn.execute("INSERT INTO task3.page_parse (extraction_id, asin, parser_version, content_hash, snapshot_path, "
                     "final_url, fetched_at, attempt_no, parse_status, sections_present, field_count, run_id) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'parsed', '{}', %s, %s)",
                     (eid, p.asin, PARSER_VERSION, m[0], m[1], m[2], m[3], len(rows), run))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task3.page_field (extraction_id, asin, parser_version, section, ordinal, "
                            "label_raw, value_raw, field_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            [(eid, p.asin, PARSER_VERSION, *r) for r in rows])
        conn.execute("INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, "
                     "category, basis_level, rule_id, parser_version, run_id) VALUES (%s, %s, %s, 'dp_page', %s, "
                     "'structured', 'MV1', %s, %s)", (eid, p.asin, T3_RULE, p.kind, PARSER_VERSION, run))

    # IMDb 与任务 4 匹配（t4r2 与正式库 map0002 同规则：导演严格重叠权重 4，使“标题 + 导演”达到自动阈值，
    # 合集成员才能满足 R22 自动关联、产生 contains 关系）
    sid = t4h.load_imdb(conn, tmp_path, w6.IMDB, date=date)
    t4 = execute_match(conn, t4h.inputs_for(fx, sid, rule_version="t4r2"), rules=t4m.ruleset("t4r2"))

    # 形态改造（映射流程无法自动产生的三处）
    for asin, tconst in w6.FORCE_LINK:
        conn.execute("UPDATE task4.movie SET status = 'imdb_linked', tconst = %s WHERE mapping_version_id = %s "
                     "AND movie_id = %s", (tconst, t4.mapping_version_id, _movie_of(conn, t4.mapping_version_id, asin)))
    for main_asin, other_asin in w6.FORCE_MERGE:
        mapping = t4.mapping_version_id
        main, other = _movie_of(conn, mapping, main_asin), _movie_of(conn, mapping, other_asin)
        conn.execute("UPDATE task4.product_movie SET movie_id = %s, basis = 'auto_decision' "
                     "WHERE mapping_version_id = %s AND asin = %s AND member_ordinal = 0", (main, mapping, other_asin))
        conn.execute("UPDATE task4.movie SET edition_count = edition_count + 1 WHERE mapping_version_id = %s "
                     "AND movie_id = %s", (mapping, main))
        conn.execute("DELETE FROM task4.movie WHERE mapping_version_id = %s AND movie_id = %s", (mapping, other))

    seed_task5(conn, eid, sid, t4.mapping_version_id)
    reset_task6(conn)
    return World(fx, sid, t4)


def seed_task5(conn: psycopg.Connection, eid: str, sid: str, mapping: str) -> None:
    """两个任务 5 人物映射版本（pmap0001 的阶段键有线索、pmap0002 用 t5r9 没有线索）与升级线索行。"""
    apply_task5_schema(conn)
    conn.execute("TRUNCATE task5.run, task5.person_mapping_version, task5.t4_upgrade_hint RESTART IDENTITY CASCADE")
    for pmap, rule in ((World.pmap_with_hints, "t5r1"), (World.pmap_alt, "t5r9")):
        conn.execute(
            "INSERT INTO task5.run (run_id, kind, extraction_id, t4_mapping_version_id, parser_version, norm_version, "
            "rule_version, imdb_snapshot_id, human_set_hash, input_key, status, finished_at) "
            "VALUES (%s, 'human_import', %s, %s, %s, 't5n1', %s, %s, 'none', %s, 'succeeded', now())",
            (f"t5seed_{pmap}", eid, mapping, PARSER_VERSION, rule, sid, f"seed|{pmap}"))
        conn.execute(
            "INSERT INTO task5.person_mapping_version (person_mapping_version_id, extraction_id, run_id, input_key, "
            "t4_mapping_version_id, parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, "
            "counts) VALUES (%s, %s, %s, %s, %s, %s, 't5n1', %s, %s, 'none', '{}')",
            (pmap, eid, f"t5seed_{pmap}", f"seed|{pmap}", mapping, PARSER_VERSION, rule, sid))
    stage_key = f"{mapping}|{PARSER_VERSION}|t5n1|t5r1|{sid}"
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO task5.t4_upgrade_hint (extraction_id, stage_key, asin, member_ordinal, tconst, "
                        "mention_keys, link_decision_ids, mention_count) VALUES (%s, %s, %s, %s, %s, %s, %s, 2)",
                        [(eid, stage_key, asin, ordinal, tconst, keys, ids)
                         for asin, ordinal, tconst, keys, ids in w6.HINTS])


def inputs_for(world: World, **over) -> RunInputs:
    base = dict(extraction_id=world.extraction_id, t4_mapping_version_id=world.mapping,
                t5_person_mapping_version_id=world.pmap_with_hints, imdb_snapshot_id=world.sid)
    return RunInputs(**{**base, **over})


def start6(conn: psycopg.Connection, world: World, **over) -> RunContext:
    return start_run(conn, inputs_for(world, **over))


def run_stages(conn: psycopg.Connection, world: World, *, rules: Any = None, stages: tuple[str, ...] | None = None,
               batch_size: int = DEFAULT_BATCH, before_commit=None, **over) -> tuple[RunContext, str | None]:
    """按 DAG 顺序逐阶段运行（测试用的直通驱动；T6-05 的 runner 与它执行同一组函数）。
    stages 省略时运行全部；返回 (ctx, release_version_id)。"""
    from etl_task6 import bounds, candidates, cooccur, product_dates, review_years, reviews, select

    ctx = start6(conn, world, **over)
    rules = rules if rules is not None else ruleset(ctx.inputs.rule_version)
    all_stages = ("candidates", "cooccur", "product_dates", "product_reviews", "review_years", "select", "bounds")
    for stage in all_stages:
        if stages is not None and stage not in stages:
            continue
        if stage == "candidates":
            candidates.extract_candidates(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        elif stage == "cooccur":
            cooccur.score_movies(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        elif stage == "product_dates":
            product_dates.extract_product_dates(conn, ctx, batch_size=batch_size, before_commit=before_commit)
        elif stage == "product_reviews":
            reviews.product_review_times(conn, ctx, batch_size=batch_size, before_commit=before_commit)
            reviews.movie_review_times(conn, ctx)
        elif stage == "review_years":
            review_years.extract_mentions(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        elif stage == "select":
            select.build_selection(conn, ctx, rules)
        elif stage == "bounds":
            bounds.build_bounds(conn, ctx, rules)
    rv = conn.execute("SELECT release_version_id FROM task6.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0]
    return ctx, rv


def movie_status(conn: psycopg.Connection, world: World, **over) -> dict[str, tuple]:
    """ASIN → (作品状态, tconst)，核对任务 4 的映射符合夹具设计。"""
    t4 = over.get("t4_mapping_version_id", world.mapping)
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT pm.asin, m.status, m.tconst FROM task4.product_movie pm JOIN task4.movie m "
        "ON m.mapping_version_id = pm.mapping_version_id AND m.movie_id = pm.movie_id "
        "WHERE pm.mapping_version_id = %s AND pm.member_ordinal <= 1 ORDER BY 1", (t4,)).fetchall()}
