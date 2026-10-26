import gzip
import hashlib

import pytest

from etl_task1.evidence import read_record_bytes, verify_block, verify_range, verify_spill
from etl_task1.parser import ScanStatus, scan_records
from etl_task1.source import open_gzip

import build_fixtures as bf
from _helpers import FILE_ID, GOOD, scan_bytes


def test_every_fixture_record_replays_byte_for_byte(tmp_path):
    gz, _ = bf.build(tmp_path)
    plain = gzip.decompress(gz.read_bytes())
    with open_gzip(gz) as fh:
        records = list(scan_records(fh, FILE_ID, ScanStatus()))
    assert len(records) == 10
    for r in records:
        b = r.block
        got = read_record_bytes(gz, b.start_offset, b.end_offset)
        assert got == b.data == plain[b.start_offset:b.end_offset]
        assert hashlib.sha256(got).hexdigest() == b.sha256
        assert verify_block(gz, b)


def test_invalid_encoding_and_nul_bytes_survive(tmp_path):
    gz, _ = bf.build(tmp_path)
    with open_gzip(gz) as fh:
        records = list(scan_records(fh, FILE_ID, ScanStatus()))
    assert b"\xff\xfe" in read_record_bytes(gz, records[4].block.start_offset, records[4].block.end_offset)
    assert b"\x00" in read_record_bytes(gz, records[7].block.start_offset, records[7].block.end_offset)


def test_each_issue_points_at_a_field(tmp_path):
    gz, _ = bf.build(tmp_path)
    with open_gzip(gz) as fh:
        records = list(scan_records(fh, FILE_ID, ScanStatus()))
    by_no = {r.block.record_no: r for r in records}
    assert {i.field for i in by_no[4].issues} == {"helpfulness", "score", "review_time"}
    bad = by_no[5].issues[0]
    assert bad.field == "review_text" and bad.details["line"] == by_no[5].start_lines["review_text"]
    assert bytes.fromhex(bad.evidence).find(b"\xff") >= 0  # 字节证据而非替换字符
    assert by_no[6].issues[0].field == "score"
    assert by_no[7].issues[0].field == "product_id"


def test_range_validation(tmp_path):
    gz = tmp_path / "x.gz"
    gz.write_bytes(gzip.compress(b"0123456789", mtime=0))
    assert read_record_bytes(gz, 2, 5) == b"234"
    assert not verify_range(gz, 2, 5, "0" * 64)
    with pytest.raises(EOFError):
        read_record_bytes(gz, 5, 50)
    with pytest.raises(ValueError):
        read_record_bytes(gz, 5, 2)


def test_spill_file_verification(tmp_path):
    big = b"\n".join(GOOD[:-1]) + b"\nreview/text: " + b"z" * 3000
    records, _ = scan_bytes(big, max_record_bytes=500, spool_dir=tmp_path)
    block = records[0].block
    assert block.oversize and verify_spill(block)
    block.spill_path.write_bytes(block.spill_path.read_bytes()[:-1])  # 模拟旁路文件不完整
    assert not verify_spill(block)
