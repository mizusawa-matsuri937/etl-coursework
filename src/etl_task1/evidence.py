"""原始证据回放：按解压字节区间重取记录，并核对哈希。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from .records import RawBlock

_CHUNK = 1024 * 1024


def iter_range(gzip_path: Path, start: int, end: int, chunk_size: int = _CHUNK) -> Iterator[bytes]:
    """从 gzip 解压流中取 [start, end) 字节。普通 gzip 需顺序解压到 start。"""
    import gzip

    if not 0 <= start <= end:
        raise ValueError("字节区间非法")
    remaining = end - start
    with gzip.open(gzip_path, "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(chunk_size, remaining))
            if not chunk:
                raise EOFError(f"区间 [{start}, {end}) 超出解压流末尾")
            remaining -= len(chunk)
            yield chunk


def read_record_bytes(gzip_path: Path, start: int, end: int, *, max_bytes: int = 64 * _CHUNK) -> bytes:
    if end - start > max_bytes:
        raise ValueError("记录过大，请改用 iter_range 流式读取")
    return b"".join(iter_range(gzip_path, start, end))


def verify_range(gzip_path: Path, start: int, end: int, expected_sha256: str) -> bool:
    digest = hashlib.sha256()
    for chunk in iter_range(gzip_path, start, end):
        digest.update(chunk)
    return digest.hexdigest() == expected_sha256


def verify_block(gzip_path: Path, block: RawBlock) -> bool:
    """用定位信息从源文件重取，核对与解析时的哈希一致。"""
    return verify_range(gzip_path, block.start_offset, block.end_offset, block.sha256)


def verify_spill(block: RawBlock) -> bool:
    """旁路文件的大小与哈希是否与记录一致。"""
    if block.spill_path is None or not block.spill_path.is_file():
        return False
    if block.spill_path.stat().st_size != block.size:
        return False
    digest = hashlib.sha256()
    with open(block.spill_path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest() == block.sha256
