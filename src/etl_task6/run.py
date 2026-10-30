"""任务 6 运行的公共部分：迁移、输入键、运行审计、批次标记与锁（spec R01、R27–R29）。

输入键 = 抽取 × 任务 4 映射版本 × 任务 5 人物映射版本 × 解析版本 × 规则版本 × IMDb 快照。
阶段键细分见 sql/501_task6_schema.sql 的 entity_stage 注释：只改规则版本（择优与上界的阈值）时
候选、共现、商品日期与评论时间的阶段键不变，不重新抽取（AC13）。同一抽取同时只允许一个任务 6 运行
（会话级咨询锁，键与任务 4、5 不同），拿不到锁立即失败。写法与任务 5 的 run.py 对应，表单独建在 task6。
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

from . import COOCCUR_PARAM_VERSION, DEFAULT_PMAP, DEFAULT_T4_MAPPING_VERSION, MENTION_PATTERN_VERSION, T4_NORM_VERSION

SCHEMA_VERSION = "501"
DEFAULT_BATCH = 500
NO_T5_SET = "none"

# 测试注入故障与批次观察用：before_commit(阶段, 批号) 在该批事务提交前调用，抛异常即整批回滚
BeforeCommit = Callable[[str, int], None]


class Task6InputError(RuntimeError):
    """输入不合格：抽取、映射版本、人物映射版本或 IMDb 快照缺失或不完整（确定性错误）。"""


class RunBusy(RuntimeError):
    """同一抽取已有写入运行持有锁。确定性失败，不自动重试。"""


def apply_task6_schema(conn: psycopg.Connection) -> bool:
    """应用 task6 表结构（须先有任务 4 的 302 与任务 5 的 401）；已应用时不做任何事。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task6_schema'))")
        have = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
        if "302" not in have or "401" not in have:
            raise RuntimeError("前置表结构 302 与 401 尚未应用，不能创建 task6 表")
        if SCHEMA_VERSION in have:
            return False
        conn.execute((sql_dir() / "501_task6_schema.sql").read_text(encoding="utf-8"))
    return True


@dataclass(frozen=True)
class RunInputs:
    extraction_id: str
    t4_mapping_version_id: str = DEFAULT_T4_MAPPING_VERSION
    t5_person_mapping_version_id: str = DEFAULT_PMAP
    imdb_snapshot_id: str = ""
    parser_version: str = PARSER_VERSION
    rule_version: str = "t6r1"

    def key(self) -> str:
        return "|".join((self.extraction_id, self.t4_mapping_version_id, self.t5_person_mapping_version_id,
                         self.parser_version, self.rule_version, self.imdb_snapshot_id))

    def stage_key(self, stage: str) -> str:
        """某阶段的输入版本串（design“版本、幂等、分批与编排”）。

        candidates     读任务 4 映射、任务 5 线索与 IMDb 标题，不含规则版本；
        cooccur        读共现评分的分数与标题、编号条件（COOCCUR_PARAM_VERSION）；
        review_years   读评论正文与提及模式版本（MENTION_PATTERN_VERSION）；
        product_dates  读页面字段（解析版本）；
        product_reviews 读参与匹配商品的有效评论（任务 4 映射版本的选取范围）。
        抽取 ID 是 entity_stage 的单独列，不进阶段键。"""
        t4, pv, sid = self.t4_mapping_version_id, self.parser_version, self.imdb_snapshot_id
        if stage == "candidates":
            return f"{t4}|{pv}|{self.t5_person_mapping_version_id}|{sid}"
        if stage == "cooccur":
            from .rules import ruleset
            r = ruleset(self.rule_version)
            return f"{t4}|{pv}|{sid}|{COOCCUR_PARAM_VERSION}:{r.cooccur_director_points}:{r.cooccur_actor_points}:{r.cooccur_min_score}"
        if stage == "review_years":
            return f"{t4}|{pv}|{MENTION_PATTERN_VERSION}"
        if stage == "product_dates":
            return pv
        if stage == "product_reviews":
            return f"{t4}|{pv}"
        raise ValueError(f"未知的阶段 {stage!r}")


@dataclass(frozen=True)
class RunContext:
    run_id: str
    inputs: RunInputs


def person_mapping_row(conn: psycopg.Connection, pmap: str):
    return conn.execute(
        "SELECT extraction_id, t4_mapping_version_id, parser_version, norm_version, rule_version, imdb_snapshot_id "
        "FROM task5.person_mapping_version WHERE person_mapping_version_id = %s", (pmap,)).fetchone()


def hint_stage_key(conn: psycopg.Connection, pmap: str) -> str:
    """任务 5 升级线索的 stage_key：由人物映射版本的各列拼出（与任务 5 写入时使用的键一致），
    随该版本的规则版本变化——换人物映射版本后线索的键不同，读不到就不再产生线索候选。"""
    row = person_mapping_row(conn, pmap)
    if row is None:
        raise Task6InputError(f"任务 5 人物映射版本 {pmap} 不存在")
    return "|".join(row[1:])


def check_inputs(conn: psycopg.Connection, inputs: RunInputs) -> dict[str, int]:
    """R01：任务 4 映射版本存在且属于该抽取；人物映射版本基于同一映射版本；IMDb 快照已装载且有标题键、
    演职员与人名；返回小计数。不符即确定性失败。"""
    row = conn.execute("SELECT extraction_id, parser_version FROM task4.mapping_version "
                       "WHERE mapping_version_id = %s", (inputs.t4_mapping_version_id,)).fetchone()
    if row is None:
        raise Task6InputError(f"任务 4 映射版本 {inputs.t4_mapping_version_id} 不存在")
    if row[0] != inputs.extraction_id:
        raise Task6InputError(f"映射版本 {inputs.t4_mapping_version_id} 属于抽取 {row[0]}，不是 {inputs.extraction_id}")
    if row[1] != inputs.parser_version:
        raise Task6InputError(f"映射版本 {inputs.t4_mapping_version_id} 的解析版本是 {row[1]}，"
                              f"不是 {inputs.parser_version}")
    pm = person_mapping_row(conn, inputs.t5_person_mapping_version_id)
    if pm is None:
        raise Task6InputError(f"任务 5 人物映射版本 {inputs.t5_person_mapping_version_id} 不存在")
    if pm[0] != inputs.extraction_id or pm[1] != inputs.t4_mapping_version_id:
        raise Task6InputError(f"人物映射版本 {inputs.t5_person_mapping_version_id} 基于（{pm[0]}, {pm[1]}），"
                              f"与本次输入（{inputs.extraction_id}, {inputs.t4_mapping_version_id}）不一致")
    status = conn.execute("SELECT status FROM imdb.snapshot WHERE snapshot_id = %s",
                          (inputs.imdb_snapshot_id,)).fetchone()
    if not status or status[0] != "loaded":
        raise Task6InputError(f"IMDb 快照 {inputs.imdb_snapshot_id} 未装载"
                              f"（状态 {status[0] if status else '不存在'}）")
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    counts = {name: one(f"SELECT count(*) FROM imdb.{t} WHERE snapshot_id = %s", inputs.imdb_snapshot_id)
              for name, t in (("imdb_titles", "title"), ("imdb_names", "name"), ("imdb_directors", "title_director"),
                              ("imdb_principals", "title_principal"))}
    if not counts["imdb_names"]:
        raise Task6InputError(f"IMDb 快照 {inputs.imdb_snapshot_id} 没有装载人名（imdb.name 为空）")
    title_keys = one("SELECT count(*) FROM task4.imdb_title_key WHERE imdb_snapshot_id = %s AND norm_version = %s",
                     inputs.imdb_snapshot_id, T4_NORM_VERSION)
    if not title_keys:
        raise Task6InputError(f"IMDb 快照 {inputs.imdb_snapshot_id} 没有标准化版本 {T4_NORM_VERSION} 的标题键")
    movies = one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", inputs.t4_mapping_version_id)
    return {"movies": movies, "imdb_title_keys": title_keys, **counts}


def _lock_key(extraction_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"etl_task6:{extraction_id}".encode()).digest()[:8], "big", signed=True)


@contextmanager
def extraction_lock(conn: psycopg.Connection, extraction_id: str) -> Iterator[None]:
    """R29：同一抽取同时只允许一个写入运行（键与任务 4、5 不同，互不阻塞）。"""
    key = _lock_key(extraction_id)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise RunBusy(f"抽取 {extraction_id} 正由另一个任务 6 运行处理")
    try:
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except psycopg.Error:
            pass


def close_abandoned(conn: psycopg.Connection, extraction_id: str, keep_airflow_run_id: str | None = None) -> None:
    """持锁后仍是 running 的运行，说明上次进程中止。keep_airflow_run_id：分步骤的 DAG 运行在两个步骤之间
    也是 running，属于本次 DAG run 的那条不能关掉。"""
    conn.execute("UPDATE task6.run SET status = 'interrupted', finished_at = clock_timestamp(), "
                 "failure_reason = '上一次运行未正常结束（进程中止）' "
                 "WHERE extraction_id = %s AND status = 'running' AND airflow_run_id IS DISTINCT FROM %s",
                 (extraction_id, keep_airflow_run_id))


def start_run(conn: psycopg.Connection, inputs: RunInputs, kind: str = "release", *,
              params: dict[str, Any] | None = None, dag_id: str | None = None,
              airflow_run_id: str | None = None) -> RunContext:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO task6.run (run_id, kind, extraction_id, t4_mapping_version_id, "
        "t5_person_mapping_version_id, parser_version, rule_version, imdb_snapshot_id, input_key, params, dag_id, "
        "airflow_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, kind, inputs.extraction_id, inputs.t4_mapping_version_id, inputs.t5_person_mapping_version_id,
         inputs.parser_version, inputs.rule_version, inputs.imdb_snapshot_id, inputs.key(), Jsonb(params or {}),
         dag_id, airflow_run_id))
    return RunContext(run_id, inputs)


def finish_run(conn: psycopg.Connection, run_id: str, status: str, counts: dict[str, Any] | None = None, *,
               failure_reason: str | None = None, release_version_id: str | None = None) -> None:
    conn.execute("UPDATE task6.run SET status = %s, counts = %s, failure_reason = %s, "
                 "release_version_id = coalesce(%s, release_version_id), finished_at = clock_timestamp() "
                 "WHERE run_id = %s", (status, Jsonb(counts or {}), failure_reason, release_version_id, run_id))


def set_release_version(conn: psycopg.Connection, run_id: str, release_version_id: str | None) -> None:
    conn.execute("UPDATE task6.run SET release_version_id = %s WHERE run_id = %s", (release_version_id, run_id))


def add_counts(conn: psycopg.Connection, run_id: str, key: str, value: Any) -> None:
    conn.execute("UPDATE task6.run SET counts = counts || jsonb_build_object(%s::text, %s::jsonb) WHERE run_id = %s",
                 (key, Jsonb(value), run_id))


def reusable_run(conn: psycopg.Connection, inputs: RunInputs) -> tuple[str, str] | None:
    """R28：同一输入键已有成功的上映运行时返回 (run_id, release_version_id)。"""
    row = conn.execute(
        "SELECT run_id, release_version_id FROM task6.run WHERE extraction_id = %s AND input_key = %s "
        "AND kind = 'release' AND status = 'succeeded' AND release_version_id IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1", (inputs.extraction_id, inputs.key())).fetchone()
    return (row[0], row[1]) if row else None


def next_batch_no(conn: psycopg.Connection, ctx: RunContext, stage: str) -> int:
    """同一运行内重新进入某阶段（重试）时批号接着编。"""
    return conn.execute("SELECT coalesce(max(batch_no), 0) + 1 FROM task6.run_batch WHERE run_id = %s AND stage = %s",
                        (ctx.run_id, stage)).fetchone()[0]


def next_release_version(conn: psycopg.Connection, extraction_id: str) -> str:
    """下一个上映信息版本号（rv0001…，按抽取内递增）。"""
    row = conn.execute("SELECT release_version_id FROM task6.release_version WHERE extraction_id = %s "
                       "ORDER BY created_at DESC, release_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    n = int(row[0][2:]) + 1 if row else 1
    return f"rv{n:04d}"


def mark_batch(conn: psycopg.Connection, ctx: RunContext, stage: str, batch_no: int, first_entity: str | None,
               last_entity: str | None, rows: int) -> None:
    conn.execute("INSERT INTO task6.run_batch (run_id, stage, batch_no, first_entity, last_entity, rows_written) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (ctx.run_id, stage, batch_no, first_entity, last_entity, rows))


def mark_entities(conn: psycopg.Connection, ctx: RunContext, stage: str, batch_no: int,
                  entities: list[tuple[str, int]]) -> None:
    """同一事务里标记本批已处理的实体及各自写入的行数。"""
    if not entities:
        return
    key = ctx.inputs.stage_key(stage)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO task6.entity_stage (extraction_id, stage, stage_key, entity_id, run_id, batch_no, "
            "rows_written) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(ctx.inputs.extraction_id, stage, key, e, ctx.run_id, batch_no, n) for e, n in entities])


def add_quality_issue(conn: psycopg.Connection, ctx: RunContext, entity_id: str, code: str,
                      detail: dict[str, Any]) -> None:
    conn.execute("INSERT INTO task6.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                 "VALUES (%s, %s, %s, %s, %s)", (ctx.run_id, ctx.inputs.extraction_id, entity_id, code,
                                                  Jsonb(detail)))


def copy_rows(conn: psycopg.Connection, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
