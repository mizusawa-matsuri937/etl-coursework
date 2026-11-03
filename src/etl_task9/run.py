"""任务 9 运行的公共部分：迁移、输入与阶段键、运行审计、实体批次标记、质量问题与咨询锁（spec R01、R36–R41）。

输入键 = 抽取 × 任务 3 规则与解析版本 × map × t4 规范化 × pmap × rv × cv（含四个阶段键）× rd（含四个阶段键）
× IMDb 快照 × 规则版本（design“版本、幂等、分批与编排”）。五个阶段键按依赖链嵌套：
sources → assertions → derivations → selections → impact，下游包含上游；rd 只进入 selections（评论层视图与评论
归属在目标层使用，原始层与暂存层不读任务 8），所以只换 rd 版本时前三个阶段原样复用。
大表里存阶段短键（阶段首字母 + 完整串 SHA-256 前 15 位），完整串登记在 task9.stage_key。
同一抽取同时只允许一个任务 9 写入运行（会话级咨询锁，键与任务 4–8 不同），拿不到锁立即失败（R38）。
写法与任务 8 的 run.py 对应。
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Sequence

import psycopg
from psycopg.types.json import Jsonb

from etl_task1.db import sql_dir

from . import (CURRENT_RULE_VERSION, DEFAULT_CONFLICT_VERSION, DEFAULT_DEDUP_VERSION, DEFAULT_EXTRACTION,
               DEFAULT_IMDB_SNAPSHOT, DEFAULT_PARSER, DEFAULT_PMAP, DEFAULT_T3_RULE, DEFAULT_T4_MAPPING_VERSION,
               DEFAULT_T4_NORM, DEFAULT_T6_RELEASE_VERSION)
from .rules import STAGE_PREFIX, STAGES, stage_signature

SCHEMA_VERSION = "801"
PREREQUISITES = {"001", "101", "102", "103", "201", "301", "302", "401", "501", "601", "701"}
DEFAULT_BATCH = 1_000            # 每批实体数（R38）
COMPLETE = "__complete__"        # 阶段完成标记（entity_stage 的实体 ID），与最后一批同事务写入

# 测试注入故障与批次观察用：before_commit(阶段, 批号) 在该批事务提交前调用，抛异常即整批回滚
BeforeCommit = Callable[[str, int], None]


class Task9InputError(RuntimeError):
    """输入不合格：抽取或上游版本缺失、未核实或互不一致，或上游阶段未完成（确定性错误）。"""


class RunBusy(RuntimeError):
    """同一抽取已有任务 9 写入运行持有锁。确定性失败，不自动重试（R38）。"""


def apply_task9_schema(conn: psycopg.Connection) -> bool:
    """应用 task9 表结构（须先有 001–701）；已应用时不做任何事。801_task9_views.sql 若存在则每次重建视图
    （视图在 T9-03 加入；文件须可重复执行）。返回本次是否新建了表结构。"""
    created = False
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task9_schema'))")
        have = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
        missing = sorted(PREREQUISITES - have)
        if missing:
            raise RuntimeError(f"前置表结构 {', '.join(missing)} 尚未应用，不能创建 task9 表")
        if SCHEMA_VERSION not in have:
            conn.execute((sql_dir() / "801_task9_schema.sql").read_text(encoding="utf-8"))
            created = True
        views = sql_dir() / "801_task9_views.sql"
        if views.exists():
            conn.execute(views.read_text(encoding="utf-8"))
    return created


# ---------------------------------------------------------------- 输入与阶段键（R01、R36）

@dataclass(frozen=True)
class RunInputs:
    extraction_id: str = DEFAULT_EXTRACTION
    t3_rule_version: str = DEFAULT_T3_RULE
    parser_version: str = DEFAULT_PARSER
    t4_mapping_version_id: str = DEFAULT_T4_MAPPING_VERSION
    norm_version: str = DEFAULT_T4_NORM
    t5_person_mapping_version_id: str = DEFAULT_PMAP
    t6_release_version_id: str = DEFAULT_T6_RELEASE_VERSION
    conflict_version_id: str = DEFAULT_CONFLICT_VERSION
    review_dedup_version_id: str = DEFAULT_DEDUP_VERSION
    imdb_snapshot_id: str = DEFAULT_IMDB_SNAPSHOT
    rule_version: str = CURRENT_RULE_VERSION
    # 以下由 check_inputs 从上游版本表填入（不由调用方给出）
    t5_norm_version: str | None = None
    cv_stage_keys: tuple[str, str, str, str] | None = None   # edition、link、scalar、multivalue
    rd_stage_keys: tuple[str, str, str, str] | None = None   # clean、family、dedup、attribution

    def key(self) -> str:
        return "|".join((self.extraction_id, self.t3_rule_version, self.parser_version, self.t4_mapping_version_id,
                         self.norm_version, self.t5_person_mapping_version_id, self.t6_release_version_id,
                         self.conflict_version_id, self.review_dedup_version_id, self.imdb_snapshot_id,
                         self.rule_version))

    def _filled(self) -> None:
        if self.cv_stage_keys is None or self.rd_stage_keys is None or self.t5_norm_version is None:
            raise Task9InputError("阶段键需要上游版本的阶段键（先运行 check_inputs）")

    def full_stage_key(self, stage: str) -> str:
        """某阶段的完整阶段串：下游包含上游。sources 含除 rd 外的全部上游版本与 cv 的四个阶段键；
        assertions、derivations 只加规则签名；selections 再加 rd 与它的归属阶段键；impact 加规则签名。"""
        self._filled()
        sources = "|".join((self.extraction_id, self.t3_rule_version, self.parser_version, self.t4_mapping_version_id,
                            self.norm_version, self.t5_person_mapping_version_id, self.t5_norm_version,
                            self.t6_release_version_id, self.conflict_version_id, *self.cv_stage_keys,
                            self.imdb_snapshot_id, stage_signature("sources", self.rule_version)))
        if stage == "sources":
            return sources
        assertions = f"{sources}|{stage_signature('assertions', self.rule_version)}"
        if stage == "assertions":
            return assertions
        derivations = f"{assertions}|{stage_signature('derivations', self.rule_version)}"
        if stage == "derivations":
            return derivations
        selections = "|".join((derivations, self.review_dedup_version_id, self.rd_stage_keys[3],
                               stage_signature("selections", self.rule_version)))
        if stage == "selections":
            return selections
        if stage == "impact":
            return f"{selections}|{stage_signature('impact', self.rule_version)}"
        raise ValueError(f"未知的阶段 {stage!r}")

    def as_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["cv_stage_keys"] = list(self.cv_stage_keys) if self.cv_stage_keys else None
        d["rd_stage_keys"] = list(self.rd_stage_keys) if self.rd_stage_keys else None
        return d


def short_key(stage: str, full_key: str) -> str:
    return STAGE_PREFIX[stage] + hashlib.sha256(full_key.encode("utf-8")).hexdigest()[:15]


@dataclass(frozen=True)
class RunContext:
    run_id: str
    inputs: RunInputs

    def key(self, stage: str) -> str:
        """阶段短键（大表里的 stage_key 列）。"""
        return short_key(stage, self.inputs.full_stage_key(stage))


def register_stage_key(conn: psycopg.Connection, ctx: RunContext, stage: str) -> str:
    """登记阶段键（已登记则不变），返回短键。上游阶段先登记。"""
    i = STAGES.index(stage)
    parent = register_stage_key(conn, ctx, STAGES[i - 1]) if i else None
    key = ctx.key(stage)
    conn.execute("INSERT INTO task9.stage_key (stage_key, extraction_id, stage, full_key, parent_stage_key, rule_version) "
                 "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (stage_key) DO NOTHING",
                 (key, ctx.inputs.extraction_id, stage, ctx.inputs.full_stage_key(stage), parent,
                  ctx.inputs.rule_version))
    return key


def check_inputs(conn: psycopg.Connection, inputs: RunInputs) -> RunInputs:
    """R01：抽取已完成；map、pmap、rv、cv、rd 都属于该抽取且互相一致（cv 与 rd 须 verified）；IMDb 快照已装载。
    返回填入 t5 规范化版本与 cv、rd 阶段键的输入。不符即确定性失败（Task9InputError）。"""
    i = inputs
    row = conn.execute("SELECT status FROM public.extraction WHERE extraction_id = %s", (i.extraction_id,)).fetchone()
    if row is None:
        raise Task9InputError(f"抽取 {i.extraction_id} 不存在")
    if row[0] != "complete":
        raise Task9InputError(f"抽取 {i.extraction_id} 状态为 {row[0]}，不是 complete")
    m = conn.execute("SELECT extraction_id, t3_rule_version, parser_version, norm_version, imdb_snapshot_id "
                     "FROM task4.mapping_version WHERE mapping_version_id = %s", (i.t4_mapping_version_id,)).fetchone()
    if m is None:
        raise Task9InputError(f"任务 4 映射版本 {i.t4_mapping_version_id} 不存在")
    if tuple(m) != (i.extraction_id, i.t3_rule_version, i.parser_version, i.norm_version, i.imdb_snapshot_id):
        raise Task9InputError(f"映射版本 {i.t4_mapping_version_id} 的（抽取、t3、解析、规范化、快照）为 {tuple(m)}，"
                              "与本次输入不一致")
    p = conn.execute("SELECT extraction_id, t4_mapping_version_id, parser_version, norm_version, imdb_snapshot_id "
                     "FROM task5.person_mapping_version WHERE person_mapping_version_id = %s",
                     (i.t5_person_mapping_version_id,)).fetchone()
    if p is None:
        raise Task9InputError(f"任务 5 人物映射版本 {i.t5_person_mapping_version_id} 不存在")
    if (p[0], p[1], p[2], p[4]) != (i.extraction_id, i.t4_mapping_version_id, i.parser_version, i.imdb_snapshot_id):
        raise Task9InputError(f"人物映射版本 {i.t5_person_mapping_version_id} 基于 {tuple(p)}，与本次输入不一致")
    r = conn.execute("SELECT extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, imdb_snapshot_id "
                     "FROM task6.release_version WHERE release_version_id = %s", (i.t6_release_version_id,)).fetchone()
    if r is None:
        raise Task9InputError(f"任务 6 上映版本 {i.t6_release_version_id} 不存在")
    if tuple(r) != (i.extraction_id, i.t4_mapping_version_id, i.t5_person_mapping_version_id, i.imdb_snapshot_id):
        raise Task9InputError(f"上映版本 {i.t6_release_version_id} 基于 {tuple(r)}，与本次输入不一致")
    c = conn.execute("SELECT extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, t6_release_version_id, "
                     "parser_version, norm_version, imdb_snapshot_id, status, edition_stage_key, link_stage_key, "
                     "scalar_stage_key, multivalue_stage_key FROM task7.conflict_version "
                     "WHERE conflict_version_id = %s", (i.conflict_version_id,)).fetchone()
    if c is None:
        raise Task9InputError(f"任务 7 冲突消解版本 {i.conflict_version_id} 不存在")
    if tuple(c[:7]) != (i.extraction_id, i.t4_mapping_version_id, i.t5_person_mapping_version_id,
                        i.t6_release_version_id, i.parser_version, i.norm_version, i.imdb_snapshot_id):
        raise Task9InputError(f"冲突消解版本 {i.conflict_version_id} 基于 {tuple(c[:7])}，与本次输入不一致")
    if c[7] != "verified":
        raise Task9InputError(f"冲突消解版本 {i.conflict_version_id} 状态为 {c[7]}，不是 verified")
    d = conn.execute("SELECT extraction_id, t3_rule_version, conflict_version_id, status, clean_stage_key, "
                     "family_stage_key, dedup_stage_key, attribution_stage_key FROM task8.dedup_version "
                     "WHERE review_dedup_version_id = %s", (i.review_dedup_version_id,)).fetchone()
    if d is None:
        raise Task9InputError(f"任务 8 去重版本 {i.review_dedup_version_id} 不存在")
    if tuple(d[:3]) != (i.extraction_id, i.t3_rule_version, i.conflict_version_id):
        raise Task9InputError(f"去重版本 {i.review_dedup_version_id} 基于 {tuple(d[:3])}，与本次输入不一致")
    if d[3] != "verified":
        raise Task9InputError(f"去重版本 {i.review_dedup_version_id} 状态为 {d[3]}，不是 verified")
    s = conn.execute("SELECT status FROM imdb.snapshot WHERE snapshot_id = %s", (i.imdb_snapshot_id,)).fetchone()
    if s is None or s[0] != "loaded":
        raise Task9InputError(f"IMDb 快照 {i.imdb_snapshot_id} 不存在或未装载完成")
    return dataclasses.replace(i, t5_norm_version=p[3], cv_stage_keys=tuple(c[8:12]), rd_stage_keys=tuple(d[4:8]))


# ---------------------------------------------------------------- 锁与运行审计

def _lock_key(extraction_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"etl_task9:{extraction_id}".encode()).digest()[:8], "big", signed=True)


@contextmanager
def extraction_lock(conn: psycopg.Connection, extraction_id: str) -> Iterator[None]:
    """R38：同一抽取同时只允许一个写入运行（键与任务 4–8 不同，互不阻塞）。"""
    key = _lock_key(extraction_id)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise RunBusy(f"抽取 {extraction_id} 正由另一个任务 9 运行处理")
    try:
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except psycopg.Error:
            pass


def close_abandoned(conn: psycopg.Connection, extraction_id: str, keep_airflow_run_id: str | None = None) -> None:
    """持锁后仍是 running 的运行，说明上次进程中止（DAG 同一 run 的那条除外）。"""
    conn.execute("UPDATE task9.run SET status = 'interrupted', finished_at = clock_timestamp(), "
                 "failure_reason = '上一次运行未正常结束（进程中止）' "
                 "WHERE extraction_id = %s AND status = 'running' AND airflow_run_id IS DISTINCT FROM %s",
                 (extraction_id, keep_airflow_run_id))


def start_run(conn: psycopg.Connection, inputs: RunInputs, kind: str = "lineage", *,
              params: dict[str, Any] | None = None, dag_id: str | None = None,
              airflow_run_id: str | None = None) -> RunContext:
    run_id = uuid.uuid4().hex
    conn.execute("INSERT INTO task9.run (run_id, kind, extraction_id, rule_version, input_key, inputs, params, dag_id, "
                 "airflow_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (run_id, kind, inputs.extraction_id, inputs.rule_version, inputs.key(), Jsonb(inputs.as_json()),
                  Jsonb(params or {}), dag_id, airflow_run_id))
    return RunContext(run_id, inputs)


def finish_run(conn: psycopg.Connection, run_id: str, status: str, counts: dict[str, Any] | None = None, *,
               failure_reason: str | None = None, lineage_version_id: str | None = None) -> None:
    conn.execute("UPDATE task9.run SET status = %s, counts = %s, failure_reason = %s, "
                 "lineage_version_id = coalesce(%s, lineage_version_id), finished_at = clock_timestamp() "
                 "WHERE run_id = %s", (status, Jsonb(counts or {}), failure_reason, lineage_version_id, run_id))


def fail_run(conn: psycopg.Connection, run_id: str, reason: str) -> None:
    conn.execute("UPDATE task9.run SET status = 'failed', failure_reason = %s, finished_at = clock_timestamp() "
                 "WHERE run_id = %s", (reason[:2000], run_id))


def add_counts(conn: psycopg.Connection, run_id: str, key: str, value: Any) -> None:
    conn.execute("UPDATE task9.run SET counts = counts || jsonb_build_object(%s::text, %s::jsonb) WHERE run_id = %s",
                 (key, Jsonb(value), run_id))


# ---------------------------------------------------------------- 实体批次（R38）

def next_batch_no(conn: psycopg.Connection, ctx: RunContext, stage: str) -> int:
    return conn.execute("SELECT coalesce(max(batch_no), 0) + 1 FROM task9.run_batch WHERE run_id = %s AND stage = %s",
                        (ctx.run_id, stage)).fetchone()[0]


def done_entities(conn: psycopg.Connection, stage_key: str, unit: str) -> set[str]:
    """本阶段键下某单元种类已提交的实体（去掉“种类:”前缀）。"""
    prefix = f"{unit}:"
    return {r[0][len(prefix):] for r in conn.execute(
        "SELECT entity_id FROM task9.entity_stage WHERE stage_key = %s AND starts_with(entity_id, %s)",
        (stage_key, prefix))}


def stage_complete(conn: psycopg.Connection, stage_key: str) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM task9.entity_stage WHERE stage_key = %s AND entity_id = %s)",
                        (stage_key, COMPLETE)).fetchone()[0]


def require_stage_complete(conn: psycopg.Connection, ctx: RunContext, stage: str) -> str:
    """下游阶段只读上游阶段结果：上游没有完成标记就确定性失败，不静默读到残缺结果。返回上游短键。"""
    key = ctx.key(stage)
    if not stage_complete(conn, key):
        raise Task9InputError(f"上游阶段 {stage}（{key}）尚未完成，不能继续")
    return key


def mark_entities(conn: psycopg.Connection, ctx: RunContext, stage: str, stage_key: str, batch_no: int, unit: str,
                  entities: Sequence[str], rows: int) -> None:
    """与该批数据同一事务：登记实体标记与批次审计。行数记在批次审计上（实体标记的 rows_written 记 0）。"""
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO task9.entity_stage (stage_key, entity_id, stage, run_id, batch_no) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [(stage_key, f"{unit}:{e}", stage, ctx.run_id, batch_no) for e in entities])
    conn.execute("INSERT INTO task9.run_batch (run_id, stage, batch_no, first_entity, last_entity, rows_written) "
                 "VALUES (%s, %s, %s, %s, %s, %s)",
                 (ctx.run_id, stage, batch_no, f"{unit}:{entities[0]}", f"{unit}:{entities[-1]}", rows))


def mark_complete(conn: psycopg.Connection, ctx: RunContext, stage: str, stage_key: str, batch_no: int) -> None:
    conn.execute("INSERT INTO task9.entity_stage (stage_key, entity_id, stage, run_id, batch_no) "
                 "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (stage_key, COMPLETE, stage, ctx.run_id, batch_no))


def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for k in range(0, len(items), size):
        yield items[k:k + size]


@dataclass
class Unit:
    """一种批次单元：name 为 entity_stage 的前缀，entities 为全部实体（已排序），write(批) 在事务内写入并返回行数，
    prepare(批) 在事务外做只读准备（如读快照文件），结果作为 write 的第二个参数。"""
    name: str
    entities: list[str]
    write: Callable[[Sequence[str], Any], int]
    prepare: Callable[[Sequence[str]], Any] | None = None


def process_units(conn: psycopg.Connection, ctx: RunContext, stage: str, stage_key: str, units: list[Unit], *,
                  batch_size: int = DEFAULT_BATCH, before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """逐单元、逐批写入：已提交的实体跳过；每批的数据、实体标记与批次审计同一事务；全部单元完成后写阶段完成标记。
    返回 {单元: {entities, todo, rows}}。"""
    if batch_size < 1:
        raise ValueError("batch_size 必须 ≥ 1")
    counts: dict[str, Any] = {}
    batch_no = next_batch_no(conn, ctx, stage)
    for u in units:
        done = done_entities(conn, stage_key, u.name)
        todo = [e for e in u.entities if e not in done]
        rows = 0
        for batch in chunks(todo, batch_size):
            prepared = u.prepare(batch) if u.prepare else None
            with conn.transaction():
                n = u.write(batch, prepared)
                mark_entities(conn, ctx, stage, stage_key, batch_no, u.name, batch, n)
                if before_commit is not None:
                    before_commit(stage, batch_no)
            rows += n
            batch_no += 1
        counts[u.name] = {"entities": len(u.entities), "todo": len(todo), "rows": rows}
    with conn.transaction():
        mark_complete(conn, ctx, stage, stage_key, batch_no)
    return counts


def add_issues(conn: psycopg.Connection, ctx: RunContext, stage_key: str,
               issues: Iterable[tuple[str, str, str, dict[str, Any]]]) -> int:
    """写质量问题 (entity_type, entity_id, issue_code, detail)；同一阶段键下重复的问题不再写（重跑幂等）。"""
    rows = [(stage_key, ctx.run_id, t, e, code, Jsonb(detail)) for t, e, code, detail in issues]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO task9.quality_issue (stage_key, run_id, entity_type, entity_id, issue_code, detail) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", rows)
    return len(rows)
