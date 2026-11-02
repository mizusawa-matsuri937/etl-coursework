"""任务 7 运行的公共部分：迁移、输入键与阶段键、运行审计、批次标记与锁（spec R01、R50–R53、R56）。

输入键 = 抽取 × 任务 4 映射版本 × 任务 5 人物映射版本 × 任务 6 上映版本 × 解析版本 × 规范化版本 × IMDb 快照 × 规则版本。
四个阶段键按依赖链嵌套（R51）：edition_resolve → link_assess → movie_scalar 与 link_assess → movie_multivalue，
下游阶段键包含上游阶段键；每组阶段的规则签名见 rules.RuleSet.signature，所以只改片长容差时只有 movie_scalar 的键变化。
同一抽取同时只允许一个任务 7 运行（会话级咨询锁，键与任务 4–6 不同），拿不到锁立即失败。
写法与任务 6 的 run.py 对应，表单独建在 task7。
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import psycopg
from psycopg.types.json import Jsonb

from etl_task1.db import sql_dir
from etl_task3 import PARSER_VERSION

from . import (DEFAULT_IMDB_SNAPSHOT, DEFAULT_PMAP, DEFAULT_T4_MAPPING_VERSION, DEFAULT_T6_RELEASE_VERSION,
               RULE_VERSION, T4_NORM_VERSION)

SCHEMA_VERSION = "601"
DEFAULT_BATCH = 1000
STAGES = ("edition_resolve", "link_assess", "movie_scalar", "movie_multivalue")
SHARED_MARK = "__shared_tconst__"        # link_assess 跨作品步骤的完成标记（entity_stage 的实体 ID）

# 测试注入故障与批次观察用：before_commit(阶段, 批号) 在该批事务提交前调用，抛异常即整批回滚
BeforeCommit = Callable[[str, int], None]


class Task7InputError(RuntimeError):
    """输入不合格：抽取、映射版本、人物映射版本、上映版本或 IMDb 快照缺失或不完整（确定性错误）。"""


class RunBusy(RuntimeError):
    """同一抽取已有写入运行持有锁。确定性失败，不自动重试（R53）。"""


def apply_task7_schema(conn: psycopg.Connection) -> bool:
    """应用 task7 表结构（须先有任务 4 的 302、任务 5 的 401 与任务 6 的 501）；已应用时不做任何事。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task7_schema'))")
        have = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
        if not {"302", "401", "501"} <= have:
            raise RuntimeError("前置表结构 302、401 与 501 尚未应用，不能创建 task7 表")
        if SCHEMA_VERSION in have:
            return False
        conn.execute((sql_dir() / "601_task7_schema.sql").read_text(encoding="utf-8"))
    return True


@dataclass(frozen=True)
class RunInputs:
    extraction_id: str
    t4_mapping_version_id: str = DEFAULT_T4_MAPPING_VERSION
    t5_person_mapping_version_id: str = DEFAULT_PMAP
    t6_release_version_id: str = DEFAULT_T6_RELEASE_VERSION
    imdb_snapshot_id: str = DEFAULT_IMDB_SNAPSHOT
    parser_version: str = PARSER_VERSION
    norm_version: str = T4_NORM_VERSION
    rule_version: str = RULE_VERSION

    def key(self) -> str:
        return "|".join((self.extraction_id, self.t4_mapping_version_id, self.t5_person_mapping_version_id,
                         self.t6_release_version_id, self.parser_version, self.norm_version,
                         self.imdb_snapshot_id, self.rule_version))

    def stage_key(self, stage: str) -> str:
        """某阶段的输入版本串（design“输入键与阶段键”）：下游包含上游。
        edition_resolve 读任务 4 断言、任务 3 页面字段与任务 6 商品日期，不含 IMDb 与任务 5；
        link_assess 再加任务 5 人物与 IMDb 快照；movie_scalar、movie_multivalue 在 link_assess 之上各加自己的规则签名。"""
        from .rules import ruleset
        r = ruleset(self.rule_version)
        edition = "|".join((self.extraction_id, self.t4_mapping_version_id, self.t6_release_version_id,
                            self.parser_version, self.norm_version, r.signature("edition")))
        if stage == "edition_resolve":
            return edition
        link = "|".join((edition, self.t5_person_mapping_version_id, self.imdb_snapshot_id, r.signature("link")))
        if stage == "link_assess":
            return link
        if stage == "movie_scalar":
            return f"{link}|{r.signature('scalar')}"
        if stage == "movie_multivalue":
            return f"{link}|{r.signature('multivalue')}"
        raise ValueError(f"未知的阶段 {stage!r}")


@dataclass(frozen=True)
class RunContext:
    run_id: str
    inputs: RunInputs


def check_inputs(conn: psycopg.Connection, inputs: RunInputs) -> dict[str, int]:
    """R01：任务 4 映射版本属于该抽取且解析、规范化版本一致；人物映射版本、上映版本基于同一映射版本且上映运行成功；
    IMDb 快照已装载且有标题键、演职员与人名；返回小计数。不符即确定性失败。"""
    row = conn.execute("SELECT extraction_id, parser_version, norm_version FROM task4.mapping_version "
                       "WHERE mapping_version_id = %s", (inputs.t4_mapping_version_id,)).fetchone()
    if row is None:
        raise Task7InputError(f"任务 4 映射版本 {inputs.t4_mapping_version_id} 不存在")
    if row[0] != inputs.extraction_id:
        raise Task7InputError(f"映射版本 {inputs.t4_mapping_version_id} 属于抽取 {row[0]}，不是 {inputs.extraction_id}")
    if row[1] != inputs.parser_version or row[2] != inputs.norm_version:
        raise Task7InputError(f"映射版本 {inputs.t4_mapping_version_id} 的解析、规范化版本是 {row[1]}、{row[2]}，"
                              f"不是 {inputs.parser_version}、{inputs.norm_version}")
    pm = conn.execute("SELECT extraction_id, t4_mapping_version_id FROM task5.person_mapping_version "
                      "WHERE person_mapping_version_id = %s", (inputs.t5_person_mapping_version_id,)).fetchone()
    if pm is None:
        raise Task7InputError(f"任务 5 人物映射版本 {inputs.t5_person_mapping_version_id} 不存在")
    if pm[0] != inputs.extraction_id or pm[1] != inputs.t4_mapping_version_id:
        raise Task7InputError(f"人物映射版本 {inputs.t5_person_mapping_version_id} 基于（{pm[0]}, {pm[1]}），"
                              f"与本次输入（{inputs.extraction_id}, {inputs.t4_mapping_version_id}）不一致")
    rv = conn.execute(
        "SELECT v.extraction_id, v.t4_mapping_version_id, v.t5_person_mapping_version_id, v.imdb_snapshot_id, "
        "v.parser_version, r.status FROM task6.release_version v JOIN task6.run r ON r.run_id = v.run_id "
        "WHERE v.release_version_id = %s", (inputs.t6_release_version_id,)).fetchone()
    if rv is None:
        raise Task7InputError(f"任务 6 上映版本 {inputs.t6_release_version_id} 不存在")
    if rv[:5] != (inputs.extraction_id, inputs.t4_mapping_version_id, inputs.t5_person_mapping_version_id,
                  inputs.imdb_snapshot_id, inputs.parser_version):
        raise Task7InputError(f"上映版本 {inputs.t6_release_version_id} 的输入是 {rv[:5]}，与本次输入不一致")
    if rv[5] != "succeeded":
        raise Task7InputError(f"上映版本 {inputs.t6_release_version_id} 所属运行状态为 {rv[5]}，不是 succeeded")
    status = conn.execute("SELECT status FROM imdb.snapshot WHERE snapshot_id = %s",
                          (inputs.imdb_snapshot_id,)).fetchone()
    if not status or status[0] != "loaded":
        raise Task7InputError(f"IMDb 快照 {inputs.imdb_snapshot_id} 未装载"
                              f"（状态 {status[0] if status else '不存在'}）")
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    counts = {name: one(f"SELECT count(*) FROM imdb.{t} WHERE snapshot_id = %s", inputs.imdb_snapshot_id)
              for name, t in (("imdb_titles", "title"), ("imdb_names", "name"), ("imdb_directors", "title_director"),
                              ("imdb_principals", "title_principal"))}
    if not counts["imdb_names"]:
        raise Task7InputError(f"IMDb 快照 {inputs.imdb_snapshot_id} 没有装载人名（imdb.name 为空）")
    title_keys = one("SELECT count(*) FROM task4.imdb_title_key WHERE imdb_snapshot_id = %s AND norm_version = %s",
                     inputs.imdb_snapshot_id, inputs.norm_version)
    if not title_keys:
        raise Task7InputError(f"IMDb 快照 {inputs.imdb_snapshot_id} 没有标准化版本 {inputs.norm_version} 的标题键")
    movies = one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", inputs.t4_mapping_version_id)
    return {"movies": movies, "imdb_title_keys": title_keys, **counts}


def _lock_key(extraction_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"etl_task7:{extraction_id}".encode()).digest()[:8], "big", signed=True)


@contextmanager
def extraction_lock(conn: psycopg.Connection, extraction_id: str) -> Iterator[None]:
    """R53：同一抽取同时只允许一个写入运行（键与任务 4–6 不同，互不阻塞）。"""
    key = _lock_key(extraction_id)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise RunBusy(f"抽取 {extraction_id} 正由另一个任务 7 运行处理")
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
    conn.execute("UPDATE task7.run SET status = 'interrupted', finished_at = clock_timestamp(), "
                 "failure_reason = '上一次运行未正常结束（进程中止）' "
                 "WHERE extraction_id = %s AND status = 'running' AND airflow_run_id IS DISTINCT FROM %s",
                 (extraction_id, keep_airflow_run_id))


def start_run(conn: psycopg.Connection, inputs: RunInputs, kind: str = "resolve", *,
              params: dict[str, Any] | None = None, dag_id: str | None = None,
              airflow_run_id: str | None = None) -> RunContext:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO task7.run (run_id, kind, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
        "t6_release_version_id, parser_version, norm_version, imdb_snapshot_id, rule_version, input_key, params, "
        "dag_id, airflow_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, kind, inputs.extraction_id, inputs.t4_mapping_version_id, inputs.t5_person_mapping_version_id,
         inputs.t6_release_version_id, inputs.parser_version, inputs.norm_version, inputs.imdb_snapshot_id,
         inputs.rule_version, inputs.key(), Jsonb(params or {}), dag_id, airflow_run_id))
    return RunContext(run_id, inputs)


def finish_run(conn: psycopg.Connection, run_id: str, status: str, counts: dict[str, Any] | None = None, *,
               failure_reason: str | None = None, conflict_version_id: str | None = None) -> None:
    conn.execute("UPDATE task7.run SET status = %s, counts = %s, failure_reason = %s, "
                 "conflict_version_id = coalesce(%s, conflict_version_id), finished_at = clock_timestamp() "
                 "WHERE run_id = %s", (status, Jsonb(counts or {}), failure_reason, conflict_version_id, run_id))


def add_counts(conn: psycopg.Connection, run_id: str, key: str, value: Any) -> None:
    conn.execute("UPDATE task7.run SET counts = counts || jsonb_build_object(%s::text, %s::jsonb) WHERE run_id = %s",
                 (key, Jsonb(value), run_id))


def reusable_run(conn: psycopg.Connection, inputs: RunInputs) -> tuple[str, str] | None:
    """R52：同一输入键已有成功且版本通过核对（verified）的运行时返回 (run_id, conflict_version_id)。"""
    row = conn.execute(
        "SELECT r.run_id, r.conflict_version_id FROM task7.run r JOIN task7.conflict_version v "
        "ON v.conflict_version_id = r.conflict_version_id WHERE r.extraction_id = %s AND r.input_key = %s "
        "AND r.kind = 'resolve' AND r.status = 'succeeded' AND v.status = 'verified' "
        "ORDER BY r.started_at DESC LIMIT 1", (inputs.extraction_id, inputs.key())).fetchone()
    return (row[0], row[1]) if row else None


def next_batch_no(conn: psycopg.Connection, ctx: RunContext, stage: str) -> int:
    """同一运行内重新进入某阶段（重试）时批号接着编。"""
    return conn.execute("SELECT coalesce(max(batch_no), 0) + 1 FROM task7.run_batch WHERE run_id = %s AND stage = %s",
                        (ctx.run_id, stage)).fetchone()[0]


def mark_batch(conn: psycopg.Connection, ctx: RunContext, stage: str, batch_no: int, first_entity: str | None,
               last_entity: str | None, rows: int) -> None:
    conn.execute("INSERT INTO task7.run_batch (run_id, stage, batch_no, first_entity, last_entity, rows_written) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (ctx.run_id, stage, batch_no, first_entity, last_entity, rows))


def mark_entities(conn: psycopg.Connection, ctx: RunContext, stage: str, batch_no: int,
                  entities: list[tuple[str, int]]) -> None:
    """同一事务里标记本批已处理的实体及各自写入的行数。"""
    if not entities:
        return
    key = ctx.inputs.stage_key(stage)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO task7.entity_stage (extraction_id, stage, stage_key, entity_id, run_id, batch_no, "
            "rows_written) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(ctx.inputs.extraction_id, stage, key, e, ctx.run_id, batch_no, n) for e, n in entities])


def done_entities(conn: psycopg.Connection, ctx: RunContext, stage: str) -> set[str]:
    """本阶段键下已提交的实体。"""
    return {r[0] for r in conn.execute(
        "SELECT entity_id FROM task7.entity_stage WHERE extraction_id = %s AND stage = %s AND stage_key = %s",
        (ctx.inputs.extraction_id, stage, ctx.inputs.stage_key(stage)))}


def pending_entities(conn: psycopg.Connection, ctx: RunContext, stage: str, entities: list[str]) -> list[str]:
    """R53：按顺序返回还没有标记的实体，中断后重跑只处理这些。"""
    done = done_entities(conn, ctx, stage)
    return [e for e in entities if e not in done]


def require_stage_complete(conn: psycopg.Connection, ctx: RunContext, stage: str, entities: list[str]) -> None:
    """下游阶段只读上游阶段结果：上游还有未处理的实体就确定性失败，不静默读到残缺结果。"""
    missing = [e for e in entities if e not in done_entities(conn, ctx, stage)]
    if missing:
        raise Task7InputError(f"上游阶段 {stage} 还有 {len(missing)} 个实体未处理（如 {missing[0]}），不能继续")


def add_quality_issue(conn: psycopg.Connection, ctx: RunContext, stage: str, entity_id: str, code: str,
                      detail: dict[str, Any]) -> None:
    conn.execute("INSERT INTO task7.quality_issue (run_id, extraction_id, stage_key, entity_id, issue_code, detail) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (ctx.run_id, ctx.inputs.extraction_id, ctx.inputs.stage_key(stage),
                                                     entity_id, code, Jsonb(detail)))


def copy_rows(conn: psycopg.Connection, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
