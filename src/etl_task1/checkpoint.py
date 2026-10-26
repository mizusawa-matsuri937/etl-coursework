"""来源/抽取/执行登记与检查点读取。检查点就是已提交的 etl_batch 行。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from .config import Settings
from .db import advisory_key
from .identity import PARSER_VERSION, RULES_VERSION, extraction_id as compute_extraction_id, program_version
from .source import SourceVerification


class CheckpointMismatch(Exception):
    """输入、配置或位置与检查点不一致，不得沿用该检查点。"""


class LoadConflict(Exception):
    """同键不同内容：已存在的记录与待写入的记录不一致。"""


class ExtractionBusy(Exception):
    """同一抽取已有另一个装载器持有锁。"""


@dataclass(frozen=True)
class Checkpoint:
    last_batch_no: int = 0
    last_record_no: int = 0
    resume_offset: int = 0
    resume_line: int = 1
    valid_count: int = 0
    quarantined_count: int = 0


@dataclass(frozen=True)
class ExtractionState:
    extraction_id: str
    status: str
    scope_complete: bool
    source_eof_reached: bool
    gzip_verified: bool


def plan_extraction_id(settings: Settings, verification: SourceVerification) -> str:
    return compute_extraction_id(verification.source_file_id, settings.effective_config_json())


def register_source(conn: psycopg.Connection, v: SourceVerification) -> None:
    with conn.transaction():
        row = conn.execute(
            "SELECT measured_size_bytes FROM source_file WHERE source_file_id = %s",
            (v.source_file_id,),
        ).fetchone()
        if row is not None:
            if row[0] != v.measured_size_bytes:
                raise LoadConflict("source_file 已存在但大小不同")
            return
        conn.execute(
            """INSERT INTO source_file (source_file_id, path, source_url, download_completed_at,
                   verified_at, recorded_size_bytes, measured_size_bytes, recorded_sha256,
                   measured_sha256, sha256_is_official)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (v.source_file_id, str(v.path), v.source_url, v.download_completed_at, v.verified_at,
             v.recorded_size_bytes, v.measured_size_bytes, v.recorded_sha256, v.measured_sha256,
             v.sha256_is_official),
        )


def register_extraction(conn: psycopg.Connection, settings: Settings,
                        v: SourceVerification) -> ExtractionState:
    eid = plan_extraction_id(settings, v)
    cfg = json.loads(settings.effective_config_json())
    with conn.transaction():
        row = conn.execute(
            """SELECT status, scope_complete, source_eof_reached, gzip_verified, source_file_id,
                      parser_version, rules_version, config FROM extraction WHERE extraction_id = %s""",
            (eid,),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO extraction (extraction_id, source_file_id, parser_version, rules_version,
                       config, scope, sample_max_records) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (eid, v.source_file_id, PARSER_VERSION, RULES_VERSION, Jsonb(cfg), settings.scope,
                 settings.sample_max_records),
            )
            return ExtractionState(eid, "loading", False, False, False)
        if (row[4], row[5], row[6], row[7]) != (v.source_file_id, PARSER_VERSION, RULES_VERSION, cfg):
            raise CheckpointMismatch("同一 extraction_id 的登记参数与当前输入不一致")
        return ExtractionState(eid, row[0], row[1], row[2], row[3])


def load_checkpoint(conn: psycopg.Connection, extraction_id: str) -> Checkpoint:
    row = conn.execute(
        """SELECT batch_no, last_record_no, resume_offset, resume_line
           FROM etl_batch WHERE extraction_id = %s ORDER BY batch_no DESC LIMIT 1""",
        (extraction_id,),
    ).fetchone()
    if row is None:
        return Checkpoint()
    totals = conn.execute(
        "SELECT sum(valid_count), sum(quarantined_count) FROM etl_batch WHERE extraction_id = %s",
        (extraction_id,),
    ).fetchone()
    return Checkpoint(row[0], row[1], row[2], row[3], int(totals[0]), int(totals[1]))


@dataclass(frozen=True)
class RunContext:
    run_id: str | None = None
    dag_id: str | None = None
    airflow_run_id: str | None = None
    image_digest: str | None = None


def start_run(conn: psycopg.Connection, extraction_id: str, ctx: RunContext) -> str:
    run_id = ctx.run_id or uuid.uuid4().hex
    with conn.transaction():
        conn.execute(
            """INSERT INTO etl_run (run_id, extraction_id, dag_id, airflow_run_id, program_version,
                   image_digest) VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, extraction_id, ctx.dag_id, ctx.airflow_run_id, Jsonb(program_version()),
             ctx.image_digest),
        )
    return run_id


def finish_run(conn: psycopg.Connection, run_id: str, status: str, reason: str | None = None) -> None:
    with conn.transaction():
        conn.execute(
            """UPDATE etl_run SET status = %s, failure_reason = %s, finished_at = now(),
                   batches_committed = (SELECT count(*) FROM etl_batch WHERE run_id = %s),
                   records_loaded = coalesce((SELECT sum(valid_count + quarantined_count)
                                              FROM etl_batch WHERE run_id = %s), 0)
               WHERE run_id = %s""",
            (status, reason, run_id, run_id, run_id),
        )


def try_lock(conn: psycopg.Connection, extraction_id: str) -> bool:
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (advisory_key(extraction_id),)).fetchone()[0]
    return bool(got)


def unlock(conn: psycopg.Connection, extraction_id: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (advisory_key(extraction_id),))
