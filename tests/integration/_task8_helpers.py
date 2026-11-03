r"""任务 8 集成测试辅助：把一组 `task8.world.Rec` 写成 gzip，用任务 1 的装载器装进 etl_test（上游表由任务 1 的代码
生成，与正式库同形），再按需逐阶段运行任务 8。每个测试开始时 `reset` 重建 task8 表结构。

上游表的写入只存在于测试里（etl_test）；任务 8 代码只读任务 1 的表。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

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
from etl_task8.runner import run_stages
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
