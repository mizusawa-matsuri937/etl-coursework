"""任务 9 规则层的纯函数（spec AC01、AC02、AC03、AC05 的纯函数部分；AC07 的操作类型闭集）。预期值手写，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib

import pytest

from etl_task9 import rules
from etl_task9.rules import (ACQUIRED_BASIS, HASH_SCOPE, OPERATIONS, assertion_id, imdb_locator_key, manual_locator_key,
                             ol_of, page_locator_key, resolve_snapshot, review_locator_key, selection_id,
                             snapshot_status, source_record_id, stage_signature)
from etl_task9.run import RunInputs, Task9InputError, short_key

FILLED = RunInputs(t5_norm_version="t5n1", cv_stage_keys=("e", "l", "s", "m"), rd_stage_keys=("c", "f", "d", "a"))


# ---------------------------------------------------------------- AC02：源记录 ID、定位串与获取时间来源

def test_ac02_source_record_id_formula():
    key = "page|eid|B000063UR2|abc"
    expected = "sr_" + hashlib.sha256(key.encode()).hexdigest()[:16]
    assert source_record_id(key) == expected
    assert source_record_id(key) == source_record_id(key)            # 同一定位稳定（R08）
    assert source_record_id(key + "x") != expected
    assert len(expected) == 19 and expected.startswith("sr_")


def test_ac02_locator_keys():
    assert page_locator_key("e1", "B0X", "h1") == "page|e1|B0X|h1"
    assert imdb_locator_key("imdb_2026-10-29", "title.basics.tsv.gz", 87476) == \
        "imdb|imdb_2026-10-29|title.basics.tsv.gz|87476"
    assert review_locator_key("e1", "8f17") == "review|e1|8f17"
    assert manual_locator_key("task4.human_decision", 7) == "manual|task4.human_decision|7"


def test_ac02_acquired_basis_and_hash_scope_per_kind():
    """R07：获取时间的来源列按种类固定；R06：哈希口径按种类固定。"""
    assert ACQUIRED_BASIS == {"page_snapshot": "task2.product_manifest.fetched_at",
                              "imdb_row": "imdb.snapshot.downloaded_at",
                              "review_record": "public.source_file.download_completed_at",
                              "manual_revision": "human_decision.decided_at"}
    assert HASH_SCOPE == {"page_snapshot": "page_body_sha256", "imdb_row": "imdb_file_sha256",
                          "review_record": "review_record_sha256", "manual_revision": "import_file_sha256"}


# ---------------------------------------------------------------- AC03：快照哈希核验

def test_ac03_snapshot_status_ok_mismatch_missing(tmp_path):
    body = b"<html>legend</html>"
    good = tmp_path / "good.html.gz"
    good.write_bytes(gzip.compress(body))
    digest = hashlib.sha256(body).hexdigest()
    assert snapshot_status(good, digest) == ("ok", {})
    changed = tmp_path / "changed.html.gz"
    changed.write_bytes(gzip.compress(body + b"!"))
    status, detail = snapshot_status(changed, digest)
    assert status == "snapshot_hash_mismatch"
    assert detail["expected"] == digest and detail["actual"] == hashlib.sha256(body + b"!").hexdigest()
    assert snapshot_status(tmp_path / "gone.html.gz", digest)[0] == "snapshot_missing"


def test_ac03_snapshot_status_hashes_decompressed_body_and_corrupt_gzip(tmp_path):
    """登记的是解压后正文的哈希（design：gzip 文件本身的哈希不同）；gzip 损坏按不一致处理。"""
    body = b"x" * 5000
    p = tmp_path / "a.html.gz"
    p.write_bytes(gzip.compress(body))
    assert snapshot_status(p, hashlib.sha256(p.read_bytes()).hexdigest())[0] == "snapshot_hash_mismatch"
    assert snapshot_status(p, hashlib.sha256(body).hexdigest(), chunk=7)[0] == "ok"      # 流式分块不影响结果
    bad = tmp_path / "bad.html.gz"
    bad.write_bytes(gzip.compress(body)[:-6])
    status, detail = snapshot_status(bad, hashlib.sha256(body).hexdigest())
    assert status == "snapshot_hash_mismatch" and "error" in detail


def test_ac03_resolve_snapshot_relative_to_root(tmp_path):
    assert resolve_snapshot("outputs/task2/snapshots/R2/B000063UR2.html.gz", "/opt/etl").as_posix() == \
        "/opt/etl/outputs/task2/snapshots/R2/B000063UR2.html.gz"
    assert resolve_snapshot(str(tmp_path / "x.gz"), "/opt/etl") == tmp_path / "x.gz"


# ---------------------------------------------------------------- AC05：断言 ID

def test_ac05_assertion_id_formula():
    expected = "as_" + hashlib.sha256(b"task4.field_assertion|44253").hexdigest()[:16]
    assert assertion_id("task4.field_assertion", "44253") == expected
    assert assertion_id("task4.field_assertion", "44253") == expected                  # 同一上游行稳定（R12）
    assert assertion_id("task7.field_candidate", "44253") != expected                   # 表名参与
    assert selection_id("movie", "mv_1", "release_year") == \
        "fs_" + hashlib.sha256(b"movie|mv_1|release_year|").hexdigest()[:16]


def test_ac05_sql_fragments_use_same_formula():
    """SQL 片段与 Python 同一公式（集成测试再逐行核对库里的值）。"""
    assert "sha256(convert_to(x, 'UTF8'))" in rules.sql_sr_id("x")
    assert "'sr_'" in rules.sql_sr_id("x") and "1, 16" in rules.sql_sr_id("x")
    assert "t || '|' || k" in rules.sql_as_id("t", "k") and "'as_'" in rules.sql_as_id("t", "k")


# ---------------------------------------------------------------- AC01：输入键与阶段键

def test_ac01_stage_keys_nest_and_rd_only_enters_selections():
    a = FILLED
    keys = {s: a.full_stage_key(s) for s in rules.STAGES}
    assert keys["assertions"].startswith(keys["sources"] + "|")
    assert keys["derivations"].startswith(keys["assertions"] + "|")
    assert keys["selections"].startswith(keys["derivations"] + "|")
    assert keys["impact"].startswith(keys["selections"] + "|")
    b = dataclasses.replace(a, review_dedup_version_id="rd0002", rd_stage_keys=("c2", "f2", "d2", "a2"))
    for s in ("sources", "assertions", "derivations"):
        assert b.full_stage_key(s) == keys[s]                  # 只换 rd：前三个阶段原样复用
    assert b.full_stage_key("selections") != keys["selections"]
    c = dataclasses.replace(a, conflict_version_id="cv0004", cv_stage_keys=("e", "l", "s2", "m"))
    assert c.full_stage_key("sources") != keys["sources"]
    assert a.key() != b.key() != c.key()


def test_ac01_stage_key_needs_upstream_keys():
    with pytest.raises(Task9InputError):
        RunInputs().full_stage_key("sources")


def test_ac01_short_key_format():
    for stage, prefix in rules.STAGE_PREFIX.items():
        k = short_key(stage, FILLED.full_stage_key(stage))
        assert k[0] == prefix and len(k) == 16 and all(ch in "0123456789abcdef" for ch in k[1:])
    assert stage_signature("sources") != stage_signature("sources", "t9r2")
    with pytest.raises(ValueError):
        stage_signature("nope")


def test_ac01_default_inputs_match_spec_r01():
    i = RunInputs()
    assert (i.extraction_id, i.t3_rule_version, i.parser_version, i.t4_mapping_version_id, i.norm_version,
            i.t5_person_mapping_version_id, i.t6_release_version_id, i.conflict_version_id,
            i.review_dedup_version_id, i.imdb_snapshot_id, i.rule_version) == (
        "f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8", "t3r2", "t3p1", "map0002", "t4n1",
        "pmap0001", "rv0001", "cv0003", "rd0001", "imdb_2026-10-29", "t9r1")


# ---------------------------------------------------------------- AC07（T9-02 继续）：操作类型闭集与 OpenLineage 标注

def test_ac07_operation_closed_set_and_openlineage():
    assert set(OPERATIONS) == {"extract", "normalize", "vocab_map", "split", "align", "link", "merge", "select",
                               "union", "infer", "aggregate", "relocate"}
    assert ol_of("extract") == ("DIRECT", "IDENTITY")
    assert ol_of("aggregate") == ("DIRECT", "AGGREGATION")
    assert ol_of("select") == ("INDIRECT", "CONDITIONAL")
    assert ol_of("align") == ("INDIRECT", "JOIN")
    for t, sub in OPERATIONS.values():
        assert (t == "DIRECT") == (sub in ("IDENTITY", "TRANSFORMATION", "AGGREGATION"))   # 与 801 的 CHECK 一致
    with pytest.raises(ValueError):
        ol_of("guess")
