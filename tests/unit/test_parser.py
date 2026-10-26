import io
import tracemalloc

import pytest

from etl_task1.parser import ScanStatus, iter_blocks, scan_records
from etl_task1.records import QUARANTINED, VALID

from _helpers import FILE_ID, GOOD, scan_bytes

REC = b"\n".join(GOOD)


def rec_with(**over):
    lines = list(GOOD)
    for i, line in enumerate(lines):
        for key, value in over.items():
            if line.startswith(key.encode() + b":"):
                lines[i] = key.encode() + b": " + value
    return b"\n".join(lines)


def test_lf_and_crlf_give_same_logical_records():
    lf = REC + b"\n\n" + REC
    crlf = lf.replace(b"\n", b"\r\n")
    a, _ = scan_bytes(lf)
    b, _ = scan_bytes(crlf)
    assert [r.raw for r in a] == [r.raw for r in b]
    assert all(r.status == VALID for r in a + b)
    assert b[0].block.data.endswith(b"\r\n")  # 原始字节保留 CRLF


def test_consecutive_blank_lines_make_no_empty_records():
    data = b"\n\n" + REC + b"\n\n\n\n" + REC + b"\n\n\n"
    records, _ = scan_bytes(data)
    assert [r.block.record_no for r in records] == [1, 2]
    assert records[0].block.start_line == 3


def test_eof_without_newline_keeps_last_record_and_locates_it():
    data = REC + b"\n\n" + REC
    records, status = scan_bytes(data)
    last = records[-1].block
    assert len(records) == 2 and status.source_eof_reached
    assert last.end_offset == len(data) and data[last.start_offset:last.end_offset] == last.data


def test_lines_offsets_and_hash_locate_the_bytes():
    data = b"\n" + REC + b"\r\n\r\n" + REC + b"\n"
    for r in scan_bytes(data)[0]:
        b = r.block
        assert data[b.start_offset:b.end_offset] == b.data
        assert b.end_line - b.start_line + 1 == len(GOOD)
    first = scan_bytes(data)[0][0].block
    assert (first.start_line, first.end_line) == (2, 9)


def test_fields_may_be_reordered_and_body_keeps_separators():
    text = b'a: b, c;\td \\ e review/score: 1 product/productId: X'
    lines = list(reversed(GOOD[:-1])) + [b"review/text: " + text]
    (r,), _ = scan_bytes(b"\n".join(lines))
    assert r.status == VALID and r.raw["review_text"] == text.decode()
    assert r.raw["product_id"] == "AAAAAAAAAA"


def test_only_one_space_removed_after_prefix():
    (r,), _ = scan_bytes(rec_with(**{"review/summary": b" two spaces "}))
    assert r.raw["summary"] == " two spaces "
    no_space = b"\n".join(GOOD[:6] + [b"review/summary:x", GOOD[7]])
    (r2,), _ = scan_bytes(no_space)
    assert r2.raw["summary"] == "x"


def test_duplicate_prefix_quarantines_whole_record_and_keeps_numbering():
    bad = REC + b"\nreview/score: 4"
    records, _ = scan_bytes(REC + b"\n\n" + bad + b"\n\n" + REC)
    assert [r.status for r in records] == [VALID, QUARANTINED, VALID]
    assert [r.block.record_no for r in records] == [1, 2, 3]
    dup = records[1].issues[0]
    assert dup.code == "duplicate_prefix" and dup.details["values"] == ["5.0", "4"]


def test_unknown_and_continuation_lines_quarantine():
    for extra in (b"review/other: x", b"continuation of text", b"  "):
        (r,), _ = scan_bytes(REC + b"\n" + extra)
        assert r.status == QUARANTINED and "unknown_line" in r.quarantine_reasons


def test_whitespace_only_line_is_not_a_separator():
    records, _ = scan_bytes(REC + b"\n \n" + REC)
    assert len(records) == 1 and records[0].status == QUARANTINED


def test_lone_cr_is_content_not_a_line_ending():
    (r,), _ = scan_bytes(rec_with(**{"review/text": b"a\rb"}))
    assert r.raw["review_text"] == "a\rb"


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 64])
def test_chunk_boundaries_do_not_change_results(chunk):
    data = (b"\r\n" + REC + b"\r\n\r\n\r\n" + REC + b"\r\n").replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n")
    ref, _ = scan_bytes(data)
    got, _ = scan_bytes(data, chunk_size=chunk)
    key = lambda rs: [(r.block.record_no, r.block.start_offset, r.block.end_offset, r.block.sha256, r.raw) for r in rs]
    assert key(got) == key(ref)


def test_sample_limit_marks_scope_but_not_eof_or_gzip():
    data = b"\n\n".join([REC] * 5)
    records, status = scan_bytes(data, max_records=2)
    assert len(records) == 2
    assert (status.scope_complete, status.source_eof_reached, status.gzip_verified) == (True, False, False)
    _, full = scan_bytes(data)
    assert (full.scope_complete, full.source_eof_reached, full.gzip_verified) == (True, True, True)


def test_resume_skips_committed_records_but_keeps_numbers():
    data = b"\n\n".join([REC] * 4)
    records, status = scan_bytes(data, start_after=2)
    assert [r.block.record_no for r in records] == [3, 4] and status.records_read == 4


def test_oversize_record_is_spilled_whole_and_isolated(tmp_path):
    big = rec_with(**{"review/text": b"x" * 5000})
    data = REC + b"\n\n" + big + b"\n\n" + REC
    records, _ = scan_bytes(data, max_record_bytes=1000, spool_dir=tmp_path)
    assert [r.status for r in records] == [VALID, QUARANTINED, VALID]
    b = records[1].block
    assert b.oversize and b.data is None and records[1].quarantine_reasons == ("oversize_record",)
    assert b.spill_path.read_bytes() == data[b.start_offset:b.end_offset]
    assert (records[1].observed_product_id, records[1].observed_reviewer_id) == ("AAAAAAAAAA", "U1")
    assert records[2].block.record_no == 3


class _Generated(io.RawIOBase):
    """按需生成数据的流，不在内存中保留整条记录。"""

    def __init__(self, head: bytes, body_len: int, tail: bytes):
        self._parts = [head, b"y" * body_len, tail]
        self._pos = [0, 0, 0]
        self._i = 0

    def readable(self):
        return True

    def read(self, n=-1):
        while self._i < 3:
            part, pos = self._parts[self._i], self._pos[self._i]
            if pos < len(part):
                out = part[pos:pos + n]
                self._pos[self._i] += len(out)
                return out
            self._i += 1
        return b""


def test_huge_single_line_has_bounded_memory(tmp_path):
    body = 20 * 1024 * 1024
    head = b"\n".join(GOOD[:-1]) + b"\nreview/text: "
    stream = _Generated(head, body, b"\n\n" + REC)
    status = ScanStatus()
    tracemalloc.start()
    records = list(scan_records(stream, FILE_ID, status, max_record_bytes=1024 * 1024, spool_dir=tmp_path))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert [r.status for r in records] == [QUARANTINED, VALID]
    assert records[0].block.size == len(head) + body + 1  # 含结尾换行
    assert peak < 6 * 1024 * 1024


def test_single_line_over_32_mib_default_limit(tmp_path):
    body = 33 * 1024 * 1024
    head = b"\n".join(GOOD[:-1]) + b"\nreview/text: "
    records, _ = (list(scan_records(_Generated(head, body, b""), FILE_ID, ScanStatus(), spool_dir=tmp_path)), None)
    (r,) = records
    assert r.block.oversize and r.quarantine_reasons == ("oversize_record",)
    assert r.block.spill_path.stat().st_size == len(head) + body
