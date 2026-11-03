"""任务 8 运行的公共部分：迁移、输入键与阶段键、运行审计、批次单元标记与锁（spec R01、R33–R36、R39）。

输入键 = 抽取 × 任务 3 分类规则版本 × 任务 7 冲突消解版本 × 规则版本（design“输入键与阶段键”）。
四个阶段键按依赖链嵌套（R34）：review_clean → review_family → review_dedup → review_attribution，下游阶段键包含上游；
每段在上游之上加自己的规则签名（rules.RuleSet.signature），所以只改家族阈值时 review_clean 的结果原样复用。
大表里存的是阶段键的短键（阶段首字母 + 完整串 SHA-256 前 15 位），完整串与所属抽取登记在 task8.stage_key。
同一抽取同时只允许一个任务 8 运行（会话级咨询锁，键与任务 4–7 不同），拿不到锁立即失败（R36）。
写法与任务 7 的 run.py 对应，表单独建在 task8。
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import psycopg
from psycopg.types.json import Jsonb

from etl_task1.db import sql_dir

from . import (CURRENT_RULE_VERSION, DEFAULT_CONFLICT_VERSION, DEFAULT_EXTRACTION, DEFAULT_T1_RULES,
               DEFAULT_T3_RULE)
from .rules import RuleSet, ruleset

SCHEMA_VERSION = "701"
DEFAULT_BATCH = 500_000          # review_clean 每批记录号区间的长度（R36）
DEFAULT_PARTITIONS = 32          # review_family 与 review_dedup 的分区数（R36）
STAGES = ("review_clean", "review_family", "review_dedup", "review_attribution")
_PREFIX = {"review_clean": "c", "review_family": "f", "review_dedup": "d", "review_attribution": "a"}
_GROUP = {"review_clean": "clean", "review_family": "family", "review_dedup": "dedup",
          "review_attribution": "attribution"}
COMPLETE = "__complete__"        # 阶段完成标记（entity_stage 的实体 ID），与最后一个批次单元同事务写入

# 测试注入故障与批次观察用：before_commit(阶段, 批号) 在该批事务提交前调用，抛异常即整批回滚
BeforeCommit = Callable[[str, int], None]


class Task8InputError(RuntimeError):
    """输入不合格：抽取、任务 3 分类、任务 7 版本缺失或不完整，或上游阶段未完成（确定性错误）。"""


class RunBusy(RuntimeError):
    """同一抽取已有写入运行持有锁。确定性失败，不自动重试（R36）。"""


def apply_task8_schema(conn: psycopg.Connection) -> bool:
    """应用 task8 表结构（须先有任务 1 的 001 与任务 7 的 601）；已应用时不做任何事。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task8_schema'))")
        have = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
        if not {"001", "601"} <= have:
            raise RuntimeError("前置表结构 001 与 601 尚未应用，不能创建 task8 表")
        if SCHEMA_VERSION in have:
            return False
        conn.execute((sql_dir() / "701_task8_schema.sql").read_text(encoding="utf-8"))
    return True


@dataclass(frozen=True)
class RunInputs:
    extraction_id: str = DEFAULT_EXTRACTION
    t1_rules_version: str = DEFAULT_T1_RULES
    t3_rule_version: str = DEFAULT_T3_RULE
    conflict_version_id: str = DEFAULT_CONFLICT_VERSION
    rule_version: str = CURRENT_RULE_VERSION
    cv_link_stage_key: str | None = None      # 任务 7 版本的 link_stage_key，由 check_attribution_inputs 填入

    def key(self) -> str:
        return "|".join((self.extraction_id, self.t3_rule_version, self.conflict_version_id, self.rule_version))

    def full_stage_key(self, stage: str, rules: RuleSet | None = None) -> str:
        """某阶段的完整阶段串（design“输入键与阶段键”）：下游包含上游。
        review_clean 只读任务 1；review_family、review_dedup 只加规则签名；review_attribution 再加 t3、cv 与 cv 的
        link_stage_key（shared_tconst_result 按它限定）。"""
        r = rules or ruleset(self.rule_version)
        clean = "|".join((self.extraction_id, self.t1_rules_version, r.signature("clean")))
        if stage == "review_clean":
            return clean
        family = f"{clean}|{r.signature('family')}"
        if stage == "review_family":
            return family
        dedup = f"{family}|{r.signature('dedup')}"
        if stage == "review_dedup":
            return dedup
        if stage == "review_attribution":
            if self.cv_link_stage_key is None:
                raise Task8InputError("review_attribution 的阶段键需要任务 7 版本的 link_stage_key（先运行 check_attribution_inputs）")
            return "|".join((dedup, self.t3_rule_version, self.conflict_version_id, self.cv_link_stage_key,
                             r.signature("attribution")))
        raise ValueError(f"未知的阶段 {stage!r}")


def short_key(stage: str, full_key: str) -> str:
    return _PREFIX[stage] + hashlib.sha256(full_key.encode("utf-8")).hexdigest()[:15]


@dataclass(frozen=True)
class RunContext:
    run_id: str
    inputs: RunInputs
    rules: RuleSet

    def key(self, stage: str) -> str:
        """阶段短键（大表里的 *_stage_key 列）。"""
        return short_key(stage, self.inputs.full_stage_key(stage, self.rules))


def register_stage_key(conn: psycopg.Connection, ctx: RunContext, stage: str) -> str:
    """登记阶段键（已登记则不变），返回短键。上游阶段先登记。"""
    i = STAGES.index(stage)
    parent = register_stage_key(conn, ctx, STAGES[i - 1]) if i else None
    key = ctx.key(stage)
    conn.execute("INSERT INTO task8.stage_key (stage_key, extraction_id, stage, full_key, parent_stage_key, rule_version) "
                 "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (stage_key) DO NOTHING",
                 (key, ctx.inputs.extraction_id, stage, ctx.inputs.full_stage_key(stage, ctx.rules), parent,
                  ctx.rules.version))
    return key


# ---------------------------------------------------------------- 输入检查（R01）

def check_extraction(conn: psycopg.Connection, inputs: RunInputs) -> dict[str, int]:
    """review_clean 到 review_dedup 只读任务 1：抽取存在、已完成、任务 1 规则版本一致；返回批次表里的记录数。"""
    row = conn.execute("SELECT status, rules_version FROM public.extraction WHERE extraction_id = %s",
                       (inputs.extraction_id,)).fetchone()
    if row is None:
        raise Task8InputError(f"抽取 {inputs.extraction_id} 不存在")
    if row[0] != "complete":
        raise Task8InputError(f"抽取 {inputs.extraction_id} 状态为 {row[0]}，不是 complete")
    if row[1] != inputs.t1_rules_version:
        raise Task8InputError(f"抽取 {inputs.extraction_id} 的任务 1 规则版本是 {row[1]}，不是 {inputs.t1_rules_version}")
    valid, quarantined, last = conn.execute(
        "SELECT coalesce(sum(valid_count), 0), coalesce(sum(quarantined_count), 0), coalesce(max(last_record_no), 0) "
        "FROM public.etl_batch WHERE extraction_id = %s", (inputs.extraction_id,)).fetchone()
    return {"records": int(valid + quarantined), "valid": int(valid), "quarantined": int(quarantined),
            "max_record_no": int(last)}


def check_attribution_inputs(conn: psycopg.Connection, inputs: RunInputs) -> RunInputs:
    """review_attribution 另读任务 3 与任务 7：该抽取有 t3r2 分类，cv 版本属于该抽取且已 verified。
    返回填入 cv 的 link_stage_key 的输入（T8-04 的归属阶段与 check_inputs 步骤使用）。"""
    n = conn.execute("SELECT count(*) FROM task3.product_classification WHERE extraction_id = %s AND rule_version = %s",
                     (inputs.extraction_id, inputs.t3_rule_version)).fetchone()[0]
    if not n:
        raise Task8InputError(f"抽取 {inputs.extraction_id} 没有任务 3 规则 {inputs.t3_rule_version} 的分类")
    cv = conn.execute("SELECT extraction_id, status, link_stage_key FROM task7.conflict_version "
                      "WHERE conflict_version_id = %s", (inputs.conflict_version_id,)).fetchone()
    if cv is None:
        raise Task8InputError(f"任务 7 冲突消解版本 {inputs.conflict_version_id} 不存在")
    if cv[0] != inputs.extraction_id:
        raise Task8InputError(f"冲突消解版本 {inputs.conflict_version_id} 属于抽取 {cv[0]}，不是 {inputs.extraction_id}")
    if cv[1] != "verified":
        raise Task8InputError(f"冲突消解版本 {inputs.conflict_version_id} 状态为 {cv[1]}，不是 verified")
    return dataclasses.replace(inputs, cv_link_stage_key=cv[2])


def check_inputs(conn: psycopg.Connection, inputs: RunInputs) -> tuple[RunInputs, dict[str, int]]:
    """R01 全部输入：任务 1 抽取 + 任务 3 分类 + 任务 7 版本。"""
    counts = check_extraction(conn, inputs)
    return check_attribution_inputs(conn, inputs), counts


# ---------------------------------------------------------------- 锁与运行审计

def _lock_key(extraction_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"etl_task8:{extraction_id}".encode()).digest()[:8], "big", signed=True)


@contextmanager
def extraction_lock(conn: psycopg.Connection, extraction_id: str) -> Iterator[None]:
    """R36：同一抽取同时只允许一个写入运行（键与任务 4–7 不同，互不阻塞）。"""
    key = _lock_key(extraction_id)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise RunBusy(f"抽取 {extraction_id} 正由另一个任务 8 运行处理")
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
    conn.execute("UPDATE task8.run SET status = 'interrupted', finished_at = clock_timestamp(), "
                 "failure_reason = '上一次运行未正常结束（进程中止）' "
                 "WHERE extraction_id = %s AND status = 'running' AND airflow_run_id IS DISTINCT FROM %s",
                 (extraction_id, keep_airflow_run_id))


def start_run(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet, kind: str = "dedup", *,
              params: dict[str, Any] | None = None, dag_id: str | None = None,
              airflow_run_id: str | None = None) -> RunContext:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO task8.run (run_id, kind, extraction_id, t3_rule_version, conflict_version_id, rule_version, "
        "input_key, params, dag_id, airflow_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, kind, inputs.extraction_id, inputs.t3_rule_version, inputs.conflict_version_id, inputs.rule_version,
         inputs.key(), Jsonb(params or {}), dag_id, airflow_run_id))
    return RunContext(run_id, inputs, rules)


def finish_run(conn: psycopg.Connection, run_id: str, status: str, counts: dict[str, Any] | None = None, *,
               failure_reason: str | None = None, review_dedup_version_id: str | None = None) -> None:
    conn.execute("UPDATE task8.run SET status = %s, counts = %s, failure_reason = %s, "
                 "review_dedup_version_id = coalesce(%s, review_dedup_version_id), finished_at = clock_timestamp() "
                 "WHERE run_id = %s", (status, Jsonb(counts or {}), failure_reason, review_dedup_version_id, run_id))


def fail_run(conn: psycopg.Connection, run_id: str, reason: str) -> None:
    """把运行记为 failed，保留已累计的阶段计数。"""
    conn.execute("UPDATE task8.run SET status = 'failed', failure_reason = %s, finished_at = clock_timestamp() "
                 "WHERE run_id = %s", (reason[:2000], run_id))


def add_counts(conn: psycopg.Connection, run_id: str, key: str, value: Any) -> None:
    conn.execute("UPDATE task8.run SET counts = counts || jsonb_build_object(%s::text, %s::jsonb) WHERE run_id = %s",
                 (key, Jsonb(value), run_id))


# ---------------------------------------------------------------- 批次单元（R36）

def next_batch_no(conn: psycopg.Connection, ctx: RunContext, stage: str) -> int:
    """同一运行内重新进入某阶段（重试）时批号接着编。"""
    return conn.execute("SELECT coalesce(max(batch_no), 0) + 1 FROM task8.run_batch WHERE run_id = %s AND stage = %s",
                        (ctx.run_id, stage)).fetchone()[0]


def done_units(conn: psycopg.Connection, stage_key: str) -> set[str]:
    """本阶段键下已提交的批次单元（含完成标记）。"""
    return {r[0] for r in conn.execute("SELECT entity_id FROM task8.entity_stage WHERE stage_key = %s", (stage_key,))}


def stage_complete(conn: psycopg.Connection, stage_key: str) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM task8.entity_stage WHERE stage_key = %s AND entity_id = %s)",
                        (stage_key, COMPLETE)).fetchone()[0]


def require_stage_complete(conn: psycopg.Connection, ctx: RunContext, stage: str) -> str:
    """下游阶段只读上游阶段结果：上游没有完成标记就确定性失败，不静默读到残缺结果。返回上游短键。"""
    key = ctx.key(stage)
    if not stage_complete(conn, key):
        raise Task8InputError(f"上游阶段 {stage}（{key}）尚未完成，不能继续")
    return key


def require_partitions(done: set[str], prefix: str, partitions: int) -> None:
    """已提交的分区单元必须用同一分区数：中途改分区数会使同一用户落在两个分区，重跑时重复写入。"""
    other = sorted({u.split("/", 1)[1] for u in done if u.startswith(prefix) and "/" in u} - {str(partitions)})
    if other:
        raise Task8InputError(f"该阶段键已按分区数 {other[0]} 提交过部分分区，本次分区数 {partitions} 不同；"
                              "请用原分区数重跑")


def mark_unit(conn: psycopg.Connection, ctx: RunContext, stage: str, stage_key: str, batch_no: int, unit: str,
              rows: int, *, first: str | None = None, last: str | None = None, complete: bool = False) -> None:
    """与该批数据同一事务：登记批次单元与批次审计；complete 时同时写阶段完成标记。"""
    units = [(unit, rows)] + ([(COMPLETE, 0)] if complete else [])
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO task8.entity_stage (stage_key, entity_id, stage, run_id, batch_no, rows_written) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        [(stage_key, u, stage, ctx.run_id, batch_no, n) for u, n in units])
    conn.execute("INSERT INTO task8.run_batch (run_id, stage, batch_no, first_entity, last_entity, rows_written) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (ctx.run_id, stage, batch_no, first or unit, last or unit, rows))


def mark_complete(conn: psycopg.Connection, ctx: RunContext, stage: str, stage_key: str, batch_no: int) -> None:
    """所有单元都已在之前提交（例如中断恰在最后一批之后）时单独补写完成标记。"""
    conn.execute("INSERT INTO task8.entity_stage (stage_key, entity_id, stage, run_id, batch_no, rows_written) "
                 "VALUES (%s, %s, %s, %s, %s, 0) ON CONFLICT DO NOTHING", (stage_key, COMPLETE, stage, ctx.run_id, batch_no))


def copy_rows(conn: psycopg.Connection, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
