"""任务 9 原始层 build_sources（spec AC01 的原始层部分、AC02、AC03）。夹具见 tests/fixtures/task9/README.md；
预期值按 spec 手算，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_sources.py
"""

from __future__ import annotations

import gzip
from datetime import datetime, timezone

import pytest

import _task9_helpers as h
from etl_task9.rules import imdb_locator_key, page_locator_key, review_locator_key, source_record_id
from etl_task9.run import RunBusy, Task9InputError, apply_task9_schema, check_inputs, extraction_lock
from etl_task9.runner import run_stages


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    return h.seed(conn, make_settings, tmp_path)


def _sources(conn, sk):
    cols = ("source_record_id", "source_kind", "locator", "locator_key", "content_hash", "hash_scope", "acquired_at",
            "acquired_at_basis", "upstream_table", "upstream_key", "evidence_status")
    return {r[3]: dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM task9.source_record WHERE stage_key = %s", (sk,))}


def _issues(conn, sk):
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT entity_id, issue_code, detail FROM task9.quality_issue WHERE stage_key = %s", (sk,))}


# ---------------------------------------------------------------- AC01：只读指定输入、上游不变、新对象只在 task9

def test_ac01_sources_read_only_scoped_inputs(conn, t9):
    before = h.upstream_digest(conn)
    h.run(conn, t9, through="sources")
    assert h.upstream_digest(conn) == before                       # R03：上游各表行数与摘要不变
    sk = h.stage_key(conn, "sources")
    rows = list(_sources(conn, sk).values())
    pages = [r for r in rows if r["source_kind"] == "page_snapshot"]
    reviews = [r for r in rows if r["source_kind"] == "review_record"]
    imdb = [r for r in rows if r["source_kind"] == "imdb_row"]
    assert pages and reviews and imdb
    assert {r["locator"]["extraction_id"] for r in pages + reviews} == {t9.eid}   # 不读装载器的另一个抽取
    assert {r["locator"]["snapshot"] for r in imdb} == {h.SNAPSHOT}               # 不读旧快照 imdb_2026-01-01
    # 评论只登记 Legend 三个 edition 商品的 6 条，合集 B0LgC 与其他作品的评论不登记（R10、R22）
    assert sorted(r["locator"]["record_no"] for r in reviews) == [1, 2, 3, 4, 5, 7]
    assert {r["upstream_table"] for r in rows} <= {"task2.product_manifest", "imdb.title", "imdb.title_aka",
                                                   "imdb.title_director", "imdb.title_principal", "imdb.name",
                                                   "public.review_record", "task4.human_decision"}


def test_ac01_new_objects_only_in_task9(conn, t9):
    """R41：迁移 801 只在 task9 建对象，只在 public.schema_migration 写一行。"""
    rel = ("SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
           "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') ORDER BY 1")
    h.reset(conn)
    before = {r[0] for r in conn.execute(rel)}
    migrations = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration")}
    assert apply_task9_schema(conn) is True
    assert apply_task9_schema(conn) is False                      # 幂等
    added = {r[0] for r in conn.execute(rel)} - before
    assert added and all(name.startswith("task9.") for name in added)
    assert {r[0] for r in conn.execute("SELECT version FROM public.schema_migration")} - migrations == {"801"}


def test_ac01_check_inputs_rejects_inconsistent_versions(conn, t9):
    filled = check_inputs(conn, t9.inputs())
    assert filled.t5_norm_version == "t5n1" and len(filled.cv_stage_keys) == 4 and len(filled.rd_stage_keys) == 4
    with pytest.raises(Task9InputError, match="cv0009"):
        check_inputs(conn, t9.inputs(conflict_version_id="cv0009"))
    with pytest.raises(Task9InputError, match="不一致"):          # rd0002 基于另一个 cv
        check_inputs(conn, t9.inputs(review_dedup_version_id="rd0002"))
    with pytest.raises(Task9InputError, match="map0001"):         # 干扰映射版本：pmap 不基于它
        check_inputs(conn, t9.inputs(t4_mapping_version_id="map0001"))
    conn.execute("UPDATE task7.conflict_version SET status = 'unverified', verified_at = NULL "
                 "WHERE conflict_version_id = %s", (t9.cv,))
    with pytest.raises(Task9InputError, match="verified"):
        check_inputs(conn, t9.inputs())


def test_ac01_concurrent_run_fails_deterministically(conn, t9, base_settings):
    """R38：同一抽取已有写入运行持有锁时立即失败（不等待）。"""
    from etl_task1.db import connect
    with connect(base_settings) as other, extraction_lock(other, t9.eid):
        with pytest.raises(RunBusy):
            h.run(conn, t9, through="sources")


# ---------------------------------------------------------------- AC02：四类源记录、获取时间与来源列、ID 稳定

def test_ac02_four_kinds_with_locators(conn, t9):
    # B0R6 的页面记录没有 fetched_at（清单状态改为 parse_failed，CHECK 允许）：获取时间保持 NULL 并写质量问题
    conn.execute("UPDATE task2.product_manifest SET status = 'parse_failed', fetched_at = NULL "
                 "WHERE extraction_id = %s AND asin = 'B0R6'", (t9.eid,))
    h.run(conn, t9, through="sources")
    sk = h.stage_key(conn, "sources")
    rows = _sources(conn, sk)
    kinds = {r["source_kind"] for r in rows.values()}
    assert kinds == {"page_snapshot", "imdb_row", "review_record", "manual_revision"}

    page = rows[page_locator_key(t9.eid, "B0Y1", h.hashlib.sha256(b"B0Y1").hexdigest())]
    assert page["locator"] == {"extraction_id": t9.eid, "asin": "B0Y1", "final_url": "https://example.invalid/dp/B0Y1",
                               "snapshot_path": "snapshots/B0Y1.html.gz", "attempt_no": 2,     # 第 1 次超时
                               "content_hash": h.hashlib.sha256(b"B0Y1").hexdigest(),
                               "task4_source_record_id": page["locator"]["task4_source_record_id"]}
    assert page["locator"]["task4_source_record_id"] is not None
    assert (page["hash_scope"], page["acquired_at"], page["acquired_at_basis"]) == \
        ("page_body_sha256", h.h7.ACQUIRED, "task2.product_manifest.fetched_at")
    assert (page["upstream_table"], page["upstream_key"], page["evidence_status"]) == \
        ("task2.product_manifest", f"{t9.eid}|B0Y1", "ok")

    lg = conn.execute("SELECT tconst, line_no FROM imdb.title WHERE snapshot_id = %s AND primary_title = 'Legend Picture'",
                      (h.SNAPSHOT,)).fetchone()
    title = rows[imdb_locator_key(h.SNAPSHOT, "title.basics.tsv.gz", lg[1])]
    assert title["locator"] == {"snapshot": h.SNAPSHOT, "file": "title.basics.tsv.gz", "line_no": lg[1],
                                "table": "imdb.title", "tconst": lg[0],
                                "file_sha256": h.file_sha256("title.basics.tsv.gz")}
    assert (title["content_hash"], title["hash_scope"], title["acquired_at"], title["acquired_at_basis"]) == \
        (h.file_sha256("title.basics.tsv.gz"), "imdb_file_sha256", h.DOWNLOADED, "imdb.snapshot.downloaded_at")

    rid, rec_no, start, end = conn.execute(
        "SELECT r.source_review_id, r.record_no, r.start_offset, r.end_offset FROM public.review_record r "
        "JOIN public.review_staging s USING (extraction_id, source_review_id) WHERE r.extraction_id = %s "
        "AND s.reviewer_id = 'LU1'", (t9.eid,)).fetchone()
    review = rows[review_locator_key(t9.eid, rid)]
    assert review["locator"]["record_no"] == rec_no and review["locator"]["start_offset"] == start
    assert review["locator"]["end_offset"] == end and review["content_hash"] == review["locator"]["raw_sha256"]
    assert review["acquired_at"] == datetime(2026, 10, 21, 2, 25, 40, tzinfo=timezone.utc)
    assert review["acquired_at_basis"] == "public.source_file.download_completed_at"
    assert review["locator"]["source_file_sha256"] == conn.execute(
        "SELECT source_file_id FROM public.extraction WHERE extraction_id = %s", (t9.eid,)).fetchone()[0]

    manual = [r for r in rows.values() if r["source_kind"] == "manual_revision"]
    assert len(manual) == 1
    m = manual[0]
    assert (m["locator"]["table"], m["locator"]["kind"], m["locator"]["left_entity"], m["locator"]["right_entity"],
            m["locator"]["annotator"]) == ("task4.human_decision", "must_link", "B0R1", "B0R1B", "fixture-annotator")
    assert m["content_hash"] == h.hashlib.sha256(b"fixture human csv").hexdigest()
    assert m["hash_scope"] == "import_file_sha256" and m["acquired_at_basis"] == "human_decision.decided_at"

    # fetched_at 为空：获取时间为 NULL，不用其他时间代替，并写 missing_acquired_at
    gone = rows[page_locator_key(t9.eid, "B0R6", h.hashlib.sha256(b"B0R6").hexdigest())]
    assert gone["acquired_at"] is None and gone["acquired_at_basis"] is None
    issues = _issues(conn, sk)
    assert issues[(gone["source_record_id"], "missing_acquired_at")] == {"source_kind": "page_snapshot"}
    assert [k for k in issues if k[1] == "missing_acquired_at"] == [(gone["source_record_id"], "missing_acquired_at")]


def test_ac02_source_record_ids_stable_and_match_python(conn, t9):
    """同一定位两次构建（另一组上游版本 → 另一个 sources 阶段键）ID 相同，且等于 Python 公式（R08）。"""
    h.run(conn, t9, through="sources")
    first = _sources(conn, h.stage_key(conn, "sources"))
    for key, row in first.items():
        assert row["source_record_id"] == source_record_id(key)
    h.run(conn, t9, through="sources", over={"conflict_version_id": t9.other_cv, "review_dedup_version_id": "rd0002"})
    keys = [r[0] for r in conn.execute("SELECT stage_key FROM task9.stage_key WHERE stage = 'sources'")]
    assert len(keys) == 2
    second = _sources(conn, h.stage_key(conn, "sources"))
    shared = set(first) & set(second)
    # 页面、评论与人工修订不随 cv 变化，必须全部再次出现；IMDb 引用集合可能随 cv 的有效 tconst 略有不同
    assert {k for k, r in first.items() if r["source_kind"] != "imdb_row"} <= shared
    assert all(first[k]["source_record_id"] == second[k]["source_record_id"] for k in shared)


def test_ac02_rerun_same_stage_key_is_idempotent(conn, t9):
    h.run(conn, t9, through="sources")
    sk = h.stage_key(conn, "sources")
    snap = sorted(conn.execute("SELECT source_record_id, locator::text, evidence_status FROM task9.source_record "
                               "WHERE stage_key = %s", (sk,)).fetchall())
    out = h.run(conn, t9, through="sources")
    assert out["sources"]["skipped"] is True                      # 完成标记存在：整个阶段跳过
    assert sorted(conn.execute("SELECT source_record_id, locator::text, evidence_status FROM task9.source_record "
                               "WHERE stage_key = %s", (sk,)).fetchall()) == snap


def test_ac02_resume_after_injected_failure(conn, t9):
    """R38：第 2 批提交前注入故障，只有第 1 批落库；重跑从未提交的实体继续，结果与一次跑完一致。"""
    def boom(stage, batch_no):
        if batch_no == 2:
            raise RuntimeError("注入的故障")
    with pytest.raises(RuntimeError, match="注入的故障"):
        h.run(conn, t9, through="sources", batch_size=4, before_commit=boom)
    sk = h.stage_key(conn, "sources")
    assert conn.execute("SELECT count(*) FROM task9.source_record WHERE stage_key = %s", (sk,)).fetchone()[0] == 4
    assert conn.execute("SELECT status FROM task9.run ORDER BY started_at DESC LIMIT 1").fetchone()[0] == "failed"
    h.run(conn, t9, through="sources", batch_size=4)
    resumed = sorted(conn.execute("SELECT source_record_id, locator::text FROM task9.source_record WHERE stage_key = %s",
                                  (sk,)).fetchall())
    conn.execute("DELETE FROM task9.quality_issue")
    conn.execute("DELETE FROM task9.source_record")
    conn.execute("DELETE FROM task9.entity_stage")
    h.run(conn, t9, through="sources", batch_size=1000)
    assert sorted(conn.execute("SELECT source_record_id, locator::text FROM task9.source_record WHERE stage_key = %s",
                               (sk,)).fetchall()) == resumed


# ---------------------------------------------------------------- AC03：快照哈希核验

def test_ac03_snapshot_ok_changed_missing(conn, t9):
    (t9.snapshot_root / "snapshots" / "B0LgB.html.gz").write_bytes(gzip.compress(b"tampered page body"))
    (t9.snapshot_root / "snapshots" / "B0LgD.html.gz").unlink()
    h.run(conn, t9, through="sources")
    sk = h.stage_key(conn, "sources")
    status = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT locator->>'asin', evidence_status, source_record_id FROM task9.source_record WHERE stage_key = %s "
        "AND source_kind = 'page_snapshot'", (sk,))}
    assert status["B0Lg"][0] == "ok"
    assert status["B0LgB"][0] == "snapshot_hash_mismatch"
    assert status["B0LgD"][0] == "snapshot_missing"
    assert sorted(a for a, (s, _) in status.items() if s != "ok") == ["B0LgB", "B0LgD"]
    issues = _issues(conn, sk)
    changed = issues[(status["B0LgB"][1], "snapshot_hash_mismatch")]
    assert changed["asin"] == "B0LgB" and changed["expected"] == h.hashlib.sha256(b"B0LgB").hexdigest()
    assert changed["actual"] == h.hashlib.sha256(b"tampered page body").hexdigest()
    missing = issues[(status["B0LgD"][1], "snapshot_missing")]
    assert missing["asin"] == "B0LgD" and missing["path"].endswith("snapshots/B0LgD.html.gz")
    # 证据有问题的源记录照常登记（不中断构建），其余种类不做快照核验
    assert conn.execute("SELECT count(*) FROM task9.source_record WHERE stage_key = %s AND source_kind <> 'page_snapshot' "
                        "AND evidence_status <> 'not_checked'", (sk,)).fetchone()[0] == 0


def test_ac03_all_snapshots_ok_when_untouched(conn, t9):
    """快照未改动时全部 ok：核验以任务 2 清单登记的正文哈希为准，多线程读文件不影响结果。"""
    run = run_stages(conn, t9.inputs(), through="sources", snapshot_root=t9.snapshot_root, snapshot_workers=4)
    assert run["sources"]["page_evidence"] == {"ok": len(t9.world.editions)}
