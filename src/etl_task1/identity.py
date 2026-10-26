"""版本、哈希与三类标识：source_file_id、source_review_id、extraction_id。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

from . import __version__

PARSER_VERSION = "parser-1"
RULES_VERSION = "task1-rules-1"

_CHUNK = 1024 * 1024


def sha256_file(path: Path, chunk_size: int = _CHUNK) -> str:
    """流式计算文件 SHA-256（小写十六进制）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def source_review_id(source_file_id: str, record_no: int) -> str:
    """SHA256(ASCII(source_file_id + ':' + record_no))；不含运行时间或自增值。"""
    if record_no < 1:
        raise ValueError("record_no 从 1 开始")
    return hashlib.sha256(f"{source_file_id}:{record_no}".encode("ascii")).hexdigest()


def extraction_id(source_file_id: str, effective_config_json: str) -> str:
    """源文件版本、解析版本、规则版本与规范化有效配置的哈希。"""
    payload = json.dumps(
        {
            "source_file_id": source_file_id,
            "parser_version": PARSER_VERSION,
            "rules_version": RULES_VERSION,
            "config": json.loads(effective_config_json),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iter_source_files(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*.py"))


def source_tree_checksum(root: Path | None = None) -> str:
    """包内全部 .py 文件的相对路径与内容的哈希，作为代码依据。"""
    root = root or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in _iter_source_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def program_version() -> dict[str, str | None]:
    """程序版本：包版本、源码树校验值；Git 提交由 ETL_GIT_COMMIT 提供，缺失时为 None。"""
    return {
        "package_version": __version__,
        "parser_version": PARSER_VERSION,
        "rules_version": RULES_VERSION,
        "source_tree_sha256": source_tree_checksum(),
        "git_commit": os.environ.get("ETL_GIT_COMMIT") or None,
    }
