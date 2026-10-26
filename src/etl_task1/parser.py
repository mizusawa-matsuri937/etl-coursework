"""流式解析：把解压字节流切成记录块，再逐块转换。

读取按有界块进行，极长记录溢出到旁路文件，内存不随记录或行长度增长。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .records import ParsedRecord, RawBlock
from .transform import parse_record

CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_RECORD_BYTES = 32 * 1024 * 1024
_BLANK_LINES = (b"\n", b"\r\n")


def iter_pieces(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> Iterator[tuple[bytes, bool]]:
    """把流切成 (片段, 是否以 LF 结束行)。过长的行会被切成多个片段。

    非结束片段不会以孤立的 CR 结尾（CR 并入下一片段），保证 CRLF 不被块边界拆开。
    """
    carry = b""
    while chunk := stream.read(chunk_size):
        data = carry + chunk if carry else chunk
        carry = b""
        pos = 0
        while True:
            i = data.find(b"\n", pos)
            if i < 0:
                rest = data[pos:]
                if rest.endswith(b"\r"):
                    carry, rest = b"\r", rest[:-1]
                if rest:
                    yield rest, False
                break
            yield data[pos : i + 1], True
            pos = i + 1
    if carry:
        yield carry, False


class _BlockBuilder:
    def __init__(self, record_no: int, start_line: int, start_offset: int,
                 max_bytes: int, spool_dir: Path | None):
        self.record_no = record_no
        self.start_line = start_line
        self.start_offset = start_offset
        self.end_line = start_line
        self.end_offset = start_offset
        self._max = max_bytes
        self._spool_dir = spool_dir
        self._buf = bytearray()
        self._hash = hashlib.sha256()
        self._size = 0
        self._spill_fh: BinaryIO | None = None
        self._spill_path: Path | None = None

    def add(self, piece: bytes) -> None:
        self._hash.update(piece)
        self._size += len(piece)
        if self._spill_fh is None and self._size > self._max:
            fd, name = tempfile.mkstemp(
                prefix=f"oversize-{self.record_no}-", suffix=".bin", dir=self._spool_dir
            )
            self._spill_fh = os.fdopen(fd, "wb")
            self._spill_path = Path(name)
            self._spill_fh.write(self._buf)
            self._buf = bytearray()
        if self._spill_fh is not None:
            self._spill_fh.write(piece)
        else:
            self._buf += piece

    def finish(self) -> RawBlock:
        if self._spill_fh is not None:
            self._spill_fh.close()
        return RawBlock(
            record_no=self.record_no,
            start_line=self.start_line,
            end_line=self.end_line,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            size=self._size,
            sha256=self._hash.hexdigest(),
            data=None if self._spill_path else bytes(self._buf),
            spill_path=self._spill_path,
        )


def iter_blocks(
    stream: BinaryIO,
    *,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    chunk_size: int = CHUNK_SIZE,
    spool_dir: Path | None = None,
) -> Iterator[RawBlock]:
    """按 R03 划分记录块：仅行结束符之外为空的行才是分隔符。"""
    record_no = 0
    line_no = 0  # 已完成的物理行数
    offset = 0
    builder: _BlockBuilder | None = None
    line_open = False  # 当前行已有片段但尚未结束
    blank = False
    line_start = 0

    for piece, final in iter_pieces(stream, chunk_size):
        if not line_open:
            line_open = True
            line_start = offset
            blank = final and piece in _BLANK_LINES
        offset += len(piece)
        if blank:
            if builder is not None:
                yield builder.finish()
                builder = None
        else:
            if builder is None:
                record_no += 1
                builder = _BlockBuilder(record_no, line_no + 1, line_start,
                                        max_record_bytes, spool_dir)
            builder.add(piece)
        if final:
            line_no += 1
            if not blank and builder is not None:
                builder.end_line, builder.end_offset = line_no, offset
            line_open = False

    if line_open and builder is not None:  # EOF 处的无换行末行
        builder.end_line, builder.end_offset = line_no + 1, offset
    if builder is not None:
        yield builder.finish()


@dataclass
class ScanStatus:
    """扫描完成状态。样本完成与整份文件完成分别记录，未到 EOF 不声称验证了 gzip。"""

    records_read: int = 0
    scope_complete: bool = False
    source_eof_reached: bool = False
    gzip_verified: bool = False


def scan_records(
    stream: BinaryIO,
    source_file_id: str,
    status: ScanStatus,
    *,
    max_records: int | None = None,
    start_after: int = 0,
    on_skip: Callable[[RawBlock], None] | None = None,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    chunk_size: int = CHUNK_SIZE,
    spool_dir: Path | None = None,
) -> Iterator[ParsedRecord]:
    """逐条产出解析结果。

    max_records 为样本上限（None 表示读到 EOF）。start_after 用于恢复：重新顺序扫描，
    但不解析编号不大于该值的记录（普通 gzip 无法随机跳转，仍需解压扫描）。
    读流过程中的 gzip 错误（CRC、截断）直接向上抛出，不会设置 gzip_verified。
    """
    blocks = iter_blocks(
        stream, max_record_bytes=max_record_bytes, chunk_size=chunk_size, spool_dir=spool_dir
    )
    for block in blocks:
        if block.record_no <= start_after:
            status.records_read = block.record_no
            if on_skip is not None and block.record_no == start_after:
                on_skip(block)  # 供调用方核对检查点对应的字节是否与已提交记录一致
            if block.spill_path is not None:
                block.spill_path.unlink(missing_ok=True)
            continue
        status.records_read = block.record_no
        yield parse_record(block, source_file_id)
        if max_records is not None and block.record_no >= max_records:
            status.scope_complete = True
            return
    status.source_eof_reached = True
    status.gzip_verified = True
    status.scope_complete = True
