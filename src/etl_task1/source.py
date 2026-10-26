"""来源校验：manifest、文件大小与 SHA-256。校验失败发生在任何装载之前。"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .identity import sha256_file

_REQUIRED_MANIFEST_KEYS = ("source_url", "size_bytes", "sha256")


class SourceError(Exception):
    """输入校验失败；code 是确定性错误类别，不应盲目重试。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Manifest:
    source_url: str
    size_bytes: int
    sha256: str
    download_completed_at: str | None  # 未知时为 None，不用验证时间代替
    gzip_integrity: str | None


@dataclass(frozen=True)
class SourceVerification:
    path: Path
    manifest_path: Path
    source_url: str
    download_completed_at: str | None
    verified_at: str  # 本次验证时间，与下载时间分开
    recorded_size_bytes: int
    measured_size_bytes: int
    recorded_sha256: str
    measured_sha256: str
    # 本地计算/记录的哈希，不是 SNAP 发布的官方校验值。
    sha256_is_official: bool = False

    @property
    def source_file_id(self) -> str:
        return self.measured_sha256


def load_manifest(path: Path) -> Manifest:
    if not path.is_file():
        raise SourceError("input_missing", f"缺少 manifest：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError("manifest_invalid", f"manifest 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise SourceError("manifest_invalid", "manifest 必须是 JSON 对象")
    missing = [k for k in _REQUIRED_MANIFEST_KEYS if data.get(k) in (None, "")]
    if missing:
        raise SourceError("manifest_invalid", f"manifest 缺少必需字段：{', '.join(missing)}")
    size, digest = data["size_bytes"], str(data["sha256"]).lower()
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise SourceError("manifest_invalid", "size_bytes 必须是正整数")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise SourceError("manifest_invalid", "sha256 必须是 64 位十六进制")
    completed = data.get("download_completed_at")
    return Manifest(
        source_url=str(data["source_url"]),
        size_bytes=size,
        sha256=digest,
        download_completed_at=str(completed) if completed else None,
        gzip_integrity=str(data["gzip_integrity"]) if data.get("gzip_integrity") else None,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_source(
    gzip_path: Path,
    manifest_path: Path,
    *,
    clock: Callable[[], str] = _utc_now,
) -> SourceVerification:
    """校验输入文件与 manifest；大小或哈希不符时抛出 SourceError。"""
    if not gzip_path.is_file():
        raise SourceError("input_missing", f"缺少源文件：{gzip_path}")
    manifest = load_manifest(manifest_path)
    measured_size = gzip_path.stat().st_size
    if measured_size != manifest.size_bytes:
        raise SourceError(
            "size_mismatch",
            f"文件大小不符：记录 {manifest.size_bytes}，实测 {measured_size}",
        )
    measured_sha = sha256_file(gzip_path)
    if measured_sha != manifest.sha256:
        raise SourceError(
            "hash_mismatch",
            f"SHA-256 不符：记录 {manifest.sha256}，实测 {measured_sha}",
        )
    return SourceVerification(
        path=gzip_path,
        manifest_path=manifest_path,
        source_url=manifest.source_url,
        download_completed_at=manifest.download_completed_at,
        verified_at=clock(),
        recorded_size_bytes=manifest.size_bytes,
        measured_size_bytes=measured_size,
        recorded_sha256=manifest.sha256,
        measured_sha256=measured_sha,
    )


def open_gzip(path: Path) -> BinaryIO:
    """以二进制流打开 gzip；读到流末尾时校验 CRC 与长度。"""
    return gzip.open(path, "rb")
