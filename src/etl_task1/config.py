"""运行配置：从环境变量读取，校验后冻结。

`effective_config()` 给出参与 extraction_id 的规范化配置，不含口令、路径和日志位置。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024 * 1024
SCOPES = ("full", "sample")


class ConfigError(ValueError):
    """配置缺失或取值非法。"""


@dataclass(frozen=True)
class Settings:
    source_gzip_path: Path
    source_manifest_path: Path
    output_dir: Path
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    scope: str = "full"
    sample_max_records: int | None = None
    batch_rows: int = 10_000
    batch_bytes: int = 32 * MIB
    encoding: str = "utf-8"
    env: str = "prod"
    fault_injection: str | None = None

    def dsn(self, *, with_password: bool = False) -> str:
        """libpq 连接串；默认不含口令，避免进入日志。"""
        parts = f"host={self.db_host} port={self.db_port} dbname={self.db_name} user={self.db_user}"
        if with_password:
            parts += f" password={self.db_password}"
        return parts

    def effective_config(self) -> dict[str, object]:
        """参与抽取 ID 的有效配置：编码、样本范围与批次参数。"""
        return {
            "encoding": self.encoding,
            "scope": self.scope,
            "sample_max_records": self.sample_max_records,
            "batch_rows": self.batch_rows,
            "batch_bytes": self.batch_bytes,
        }

    def effective_config_json(self) -> str:
        return json.dumps(self.effective_config(), sort_keys=True, separators=(",", ":"))


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"缺少必需配置 {key}")
    return value


def _positive_int(env: Mapping[str, str], key: str, default: int | None) -> int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} 必须是整数，实际为 {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{key} 必须为正整数，实际为 {value}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    scope = env.get("ETL_SCOPE", "full").strip() or "full"
    if scope not in SCOPES:
        raise ConfigError(f"ETL_SCOPE 只能是 {SCOPES}，实际为 {scope!r}")
    sample_max = _positive_int(env, "ETL_SAMPLE_MAX_RECORDS", None)
    if scope == "full" and sample_max is not None:
        raise ConfigError("scope=full 不允许设置 ETL_SAMPLE_MAX_RECORDS")

    encoding = env.get("ETL_ENCODING", "utf-8").strip() or "utf-8"
    if encoding.lower() != "utf-8":
        raise ConfigError("初版只支持严格 UTF-8；更换编码须有证据并更新规则版本")

    etl_env = env.get("ETL_ENV", "prod").strip() or "prod"
    fault = env.get("ETL_FAULT_INJECTION", "").strip() or None
    if fault is not None and etl_env != "test":
        raise ConfigError("故障注入仅允许在 ETL_ENV=test 下启用")

    return Settings(
        source_gzip_path=Path(_required(env, "SOURCE_GZIP_PATH")),
        source_manifest_path=Path(_required(env, "SOURCE_MANIFEST_PATH")),
        output_dir=Path(env.get("ETL_OUTPUT_DIR", "outputs").strip() or "outputs"),
        db_host=_required(env, "ETL_DB_HOST"),
        db_port=_positive_int(env, "ETL_DB_PORT", 5432) or 5432,
        db_name=_required(env, "ETL_DB_NAME"),
        db_user=_required(env, "ETL_DB_USER"),
        db_password=_required(env, "ETL_DB_PASSWORD"),
        scope=scope,
        sample_max_records=sample_max,
        batch_rows=_positive_int(env, "ETL_BATCH_ROWS", 10_000) or 10_000,
        batch_bytes=_positive_int(env, "ETL_BATCH_BYTES", 32 * MIB) or 32 * MIB,
        encoding="utf-8",
        env=etl_env,
        fault_injection=fault,
    )
