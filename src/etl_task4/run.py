"""任务 4 运行的公共部分：迁移、输入键、运行审计、批次标记与锁（spec R01、R38、R39、R41）。

输入键 = 抽取 × 任务 3 规则版本 × 解析版本 × 标准化版本 × 规则版本 × IMDb 快照 × 人工决策集。
各阶段（标准化、候选与判定、聚类）都只处理自己输入键下还没有标记的实体，所以中断后重跑只补剩余批次；
同一抽取同时只允许一个写入运行（会话级咨询锁），拿不到锁立即失败。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import psycopg
from psycopg.types.json import Jsonb

from etl_task1.db import sql_dir
from etl_task3 import PARSER_VERSION

from . import DEFAULT_T3_RULE_VERSION, NORM_VERSION, RULE_VERSION

SCHEMA_VERSION = "302"
DEFAULT_BATCH = 500
NO_HUMAN_SET = "none"

# 测试注入故障与批次观察用：before_commit(阶段, 批号) 在该批事务提交前调用，抛异常即整批回滚
BeforeCommit = Callable[[str, int], None]


class Task4InputError(RuntimeError):
    """输入不合格：抽取、任务 3 分类或 IMDb 快照缺失或不完整（确定性错误）。"""


class RunBusy(RuntimeError):
    """同一抽取已有写入运行持有锁。按 AC10 属于确定性失败，不自动重试。"""


def apply_task4_schema(conn: psycopg.Connection) -> bool:
    """应用 task4 表结构（须先有任务 2、3 的 101–103、201 与 IMDb 的 301）；已应用时不做任何事。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task4_schema'))")
        have = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
        missing = {"101", "102", "103", "201", "301"} - have
        if missing:
            raise RuntimeError(f"前置表结构 {sorted(missing)} 尚未应用，不能创建 task4 表")
        if SCHEMA_VERSION in have:
            return False
        conn.execute((sql_dir() / "302_task4_schema.sql").read_text(encoding="utf-8"))
    return True


@dataclass(frozen=True)
class RunInputs:
    extraction_id: str
    imdb_snapshot_id: str
    t3_rule_version: str = DEFAULT_T3_RULE_VERSION
    parser_version: str = PARSER_VERSION
    norm_version: str = NORM_VERSION
    rule_version: str = RULE_VERSION
    human_set_hash: str = NO_HUMAN_SET

    def key(self) -> str:
        return "|".join((self.extraction_id, self.t3_rule_version, self.parser_version, self.norm_version,
                         self.rule_version, self.imdb_snapshot_id, self.human_set_hash))

    def stage_key(self, stage: str) -> str:
        """某阶段的输入版本串：标准化只取决于解析与标准化版本；判定还取决于规则版本、IMDb 快照和任务 3 规则版本。"""
        if stage == "normalize":
            return f"{self.parser_version}|{self.norm_version}"
        return "|".join((self.t3_rule_version, self.parser_version, self.norm_version, self.rule_version,
                         self.imdb_snapshot_id))


@dataclass(frozen=True)
class RunContext:
    run_id: str
    inputs: RunInputs


def human_set_hash(conn: psycopg.Connection, extraction_id: str) -> str:
    """人工决策集的哈希：空集为 'none'，否则是排序后全部决策（类别与双方）的 SHA-256 前 16 位。"""
    rows = conn.execute("SELECT kind, left_entity, coalesce(right_entity, '') FROM task4.human_decision "
                        "WHERE extraction_id = %s ORDER BY kind, left_entity, 3", (extraction_id,)).fetchall()
    if not rows:
        return NO_HUMAN_SET
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]


def check_inputs(conn: psycopg.Connection, inputs: RunInputs) -> dict[str, int]:
    """R01：抽取存在，任务 3 分类覆盖整张清单，IMDb 快照已装载；返回小计数。不符即确定性失败。"""
    if not conn.execute("SELECT 1 FROM public.extraction WHERE extraction_id = %s",
                        (inputs.extraction_id,)).fetchone():
        raise Task4InputError(f"抽取 {inputs.extraction_id} 不存在")
    manifest = conn.execute("SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %s",
                            (inputs.extraction_id,)).fetchone()[0]
    classified = conn.execute(
        "SELECT count(*) FROM task3.product_classification WHERE extraction_id = %s AND rule_version = %s",
        (inputs.extraction_id, inputs.t3_rule_version)).fetchone()[0]
    if manifest == 0:
        raise Task4InputError(f"抽取 {inputs.extraction_id} 没有采集清单")
    if classified != manifest:
        raise Task4InputError(f"任务 3 分类 {classified} 行 ≠ 清单 {manifest} 行（规则 {inputs.t3_rule_version}）")
    status = conn.execute("SELECT status FROM imdb.snapshot WHERE snapshot_id = %s",
                          (inputs.imdb_snapshot_id,)).fetchone()
    if not status or status[0] != "loaded":
        raise Task4InputError(f"IMDb 快照 {inputs.imdb_snapshot_id} 未装载（状态 {status[0] if status else '不存在'}）")
    return {"manifest_rows": manifest, "classification_rows": classified}


def _lock_key(extraction_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"etl_task4:{extraction_id}".encode()).digest()[:8], "big", signed=True)


@contextmanager
def extraction_lock(conn: psycopg.Connection, extraction_id: str) -> Iterator[None]:
    """R39：同一抽取同时只允许一个写入运行。"""
    key = _lock_key(extraction_id)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise RunBusy(f"抽取 {extraction_id} 正由另一个任务 4 运行处理")
    try:
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except psycopg.Error:
            pass


def close_abandoned(conn: psycopg.Connection, extraction_id: str, keep_airflow_run_id: str | None = None) -> None:
    """持锁后仍是 running 的运行，说明上次进程中止。keep_airflow_run_id：分步骤的 DAG 运行在两个步骤之间也是 running，
    属于本次 DAG run 的那条不能关掉。"""
    conn.execute("UPDATE task4.run SET status = 'interrupted', finished_at = clock_timestamp(), "
                 "failure_reason = '上一次运行未正常结束（进程中止）' "
                 "WHERE extraction_id = %s AND status = 'running' AND airflow_run_id IS DISTINCT FROM %s",
                 (extraction_id, keep_airflow_run_id))


def start_run(conn: psycopg.Connection, inputs: RunInputs, kind: str = "match", *, params: dict[str, Any] | None = None,
              dag_id: str | None = None, airflow_run_id: str | None = None) -> RunContext:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO task4.run (run_id, kind, extraction_id, t3_rule_version, parser_version, norm_version, "
        "rule_version, imdb_snapshot_id, human_set_hash, input_key, params, dag_id, airflow_run_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, kind, inputs.extraction_id, inputs.t3_rule_version, inputs.parser_version, inputs.norm_version,
         inputs.rule_version, inputs.imdb_snapshot_id, inputs.human_set_hash, inputs.key(), Jsonb(params or {}),
         dag_id, airflow_run_id))
    return RunContext(run_id, inputs)


def finish_run(conn: psycopg.Connection, run_id: str, status: str, counts: dict[str, Any] | None = None, *,
               failure_reason: str | None = None, mapping_version_id: str | None = None) -> None:
    conn.execute("UPDATE task4.run SET status = %s, counts = %s, failure_reason = %s, "
                 "mapping_version_id = coalesce(%s, mapping_version_id), finished_at = clock_timestamp() "
                 "WHERE run_id = %s",
                 (status, Jsonb(counts or {}), failure_reason, mapping_version_id, run_id))


def reusable_run(conn: psycopg.Connection, inputs: RunInputs) -> tuple[str, str] | None:
    """R38：同一输入键已有成功的匹配运行时返回 (run_id, mapping_version_id)。"""
    row = conn.execute(
        "SELECT run_id, mapping_version_id FROM task4.run WHERE extraction_id = %s AND input_key = %s "
        "AND kind = 'match' AND status = 'succeeded' AND mapping_version_id IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1", (inputs.extraction_id, inputs.key())).fetchone()
    return (row[0], row[1]) if row else None


def next_batch_no(conn: psycopg.Connection, ctx: RunContext, stage: str) -> int:
    """同一运行内重新进入某阶段（重试）时批号接着编。"""
    return conn.execute("SELECT coalesce(max(batch_no), 0) + 1 FROM task4.run_batch WHERE run_id = %s AND stage = %s",
                        (ctx.run_id, stage)).fetchone()[0]


def mark_batch(conn: psycopg.Connection, ctx: RunContext, stage: str, batch_no: int, first_asin: str | None,
               last_asin: str | None, rows: int) -> None:
    conn.execute("INSERT INTO task4.run_batch (run_id, stage, batch_no, first_asin, last_asin, rows_written) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (ctx.run_id, stage, batch_no, first_asin, last_asin, rows))


def mark_entities(conn: psycopg.Connection, ctx: RunContext, stage: str, batch_no: int,
                  entities: list[tuple[str, int]]) -> None:
    """同一事务里标记本批已处理的实体及各自写入的行数。"""
    if not entities:
        return
    key = ctx.inputs.stage_key(stage)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO task4.entity_stage (extraction_id, stage, stage_key, entity_id, run_id, batch_no, rows_written) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(ctx.inputs.extraction_id, stage, key, e, ctx.run_id, batch_no, n) for e, n in entities])


def copy_rows(conn: psycopg.Connection, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
