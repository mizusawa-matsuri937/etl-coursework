"""数据库连接、表结构应用与完整性核对。"""

from __future__ import annotations

from pathlib import Path

import psycopg

from .config import Settings

SCHEMA_VERSION = "001"


def sql_dir() -> Path:
    """sql/ 目录：容器内为 /opt/etl/sql，本地为仓库根目录下的 sql。"""
    return Path(__file__).resolve().parents[2] / "sql"


def connect(settings: Settings, **kwargs) -> psycopg.Connection:
    """默认自动提交：显式事务一律用 `with conn.transaction()`，避免隐式事务把它降级为保存点。"""
    kwargs.setdefault("autocommit", True)
    return psycopg.connect(settings.dsn(with_password=True), **kwargs)


def apply_schema(conn: psycopg.Connection) -> bool:
    """应用表结构；已应用时不做任何事。返回是否本次新建。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task1_schema'))")
        exists = conn.execute("SELECT to_regclass('schema_migration')").fetchone()[0] is not None
        if exists and conn.execute(
            "SELECT 1 FROM schema_migration WHERE version = %s", (SCHEMA_VERSION,)
        ).fetchone():
            return False
        conn.execute((sql_dir() / "001_task1_schema.sql").read_text(encoding="utf-8"))
    return True


def run_integrity(conn: psycopg.Connection, extraction_id: str) -> dict[str, int]:
    """执行 task1_integrity.sql，返回 {检查名: 违规数}。"""
    sql = (sql_dir() / "task1_integrity.sql").read_text(encoding="utf-8")
    with conn.transaction():
        rows = conn.execute(sql, {"eid": extraction_id}).fetchall()
    return {name: int(n) for name, n in rows}


def advisory_key(extraction_id: str) -> int:
    """由抽取 ID 前 8 字节得到的有符号 64 位咨询锁键。"""
    return int.from_bytes(bytes.fromhex(extraction_id)[:8], "big", signed=True)
