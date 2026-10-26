import hashlib
import json

import pytest

from etl_task1 import identity
from etl_task1.config import load_settings
from etl_task1.parser import ScanStatus, scan_records

from _helpers import FILE_ID, GOOD, scan_bytes

CFG = '{"batch_bytes":1,"batch_rows":2,"encoding":"utf-8","sample_max_records":null,"scope":"full"}'


def test_source_review_id_formula_and_stability():
    expected = hashlib.sha256(f"{FILE_ID}:7".encode()).hexdigest()
    assert identity.source_review_id(FILE_ID, 7) == expected
    assert identity.source_review_id(FILE_ID, 7) == identity.source_review_id(FILE_ID, 7)
    assert identity.source_review_id(FILE_ID, 7) != identity.source_review_id(FILE_ID, 8)
    assert identity.source_review_id("b" * 64, 7) != expected
    with pytest.raises(ValueError):
        identity.source_review_id(FILE_ID, 0)


def test_identical_content_gets_distinct_ids_and_both_kept():
    block = b"\n".join(GOOD)
    records, _ = scan_bytes(block + b"\n\n" + block + b"\n")
    assert len(records) == 2
    assert records[0].raw == records[1].raw
    assert records[0].source_review_id != records[1].source_review_id
    assert records[0].block.sha256 == records[1].block.sha256  # 内容相同，定位与 ID 不同


def test_rerun_same_ids_and_run_time_irrelevant():
    block = b"\n".join(GOOD)
    a, _ = scan_bytes(block)
    b, _ = scan_bytes(block)
    assert a[0].source_review_id == b[0].source_review_id


def test_extraction_id_depends_on_version_inputs():
    base = identity.extraction_id(FILE_ID, CFG)
    assert base == identity.extraction_id(FILE_ID, CFG)
    assert base != identity.extraction_id("b" * 64, CFG)
    assert base != identity.extraction_id(FILE_ID, CFG.replace('"scope":"full"', '"scope":"sample"'))
    cfg2 = json.loads(CFG) | {"batch_rows": 3}
    assert base != identity.extraction_id(FILE_ID, json.dumps(cfg2, sort_keys=True))


def test_extraction_id_sample_vs_full_via_settings(base_env):
    full = load_settings(base_env)
    sample = load_settings({**base_env, "ETL_SCOPE": "sample", "ETL_SAMPLE_MAX_RECORDS": "5"})
    assert identity.extraction_id(FILE_ID, full.effective_config_json()) != \
        identity.extraction_id(FILE_ID, sample.effective_config_json())


def test_program_version_has_code_evidence():
    v = identity.program_version()
    assert len(v["source_tree_sha256"]) == 64 and v["rules_version"] == identity.RULES_VERSION
