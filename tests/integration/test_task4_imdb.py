"""任务 4 IMDb 快照装载（spec AC02）：类型过滤、行号、`\\N`、非 ASCII 别名、哈希不符拒绝且无部分行、重复装载 reused。"""

from __future__ import annotations

import gzip
import json

import psycopg
import pytest
from task4 import imdb_files as fx

from etl_task1.db import connect
from etl_task4 import imdb
from etl_task4.imdb import (FILE_NAMES, ImdbBusy, ImdbInputError, apply_imdb_schema, check_files, load_snapshot,
                            problems, snapshot_dir, verify_snapshot)

pytestmark = pytest.mark.integration

TABLES = ("title", "title_other", "title_aka", "title_director", "title_principal", "name", "source_file", "snapshot")


@pytest.fixture
def db(conn):
    """每个测试从空的 imdb schema 开始（imdb 表与 public 的清单无关，不会被 conn 夹具清空）。"""
    apply_imdb_schema(conn)
    conn.execute("TRUNCATE imdb.snapshot CASCADE")
    return conn


@pytest.fixture
def snap(tmp_path):
    return fx.build_snapshot(tmp_path / "2026-10-30", **fx.BASIC)


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM imdb.{table}").fetchone()[0]


def _all_empty(conn) -> bool:
    return all(_count(conn, t) == 0 for t in TABLES)


def test_load_keeps_only_three_types_with_aliases_crew_and_people(db, snap):
    r = load_snapshot(db, snap)
    assert r.status == "loaded" and r.snapshot_id == fx.SNAPSHOT_ID
    types = dict(db.execute("SELECT title_type, count(*) FROM imdb.title GROUP BY 1").fetchall())
    assert types == fx.TITLE_TYPES
    assert {t for (t,) in db.execute("SELECT tconst FROM imdb.title").fetchall()} == {
        "tt0000001", "tt0000002", "tt0000003", "tt0000007", "tt0000008"}
    # 别名只属于保留作品；导演保留 title.crew 串内序号；演员表只含 actor、actress、director
    assert _count(db, "title_aka") == 4 and _count(db, "title_director") == 4
    assert sorted(db.execute("SELECT tconst, ordinal, nconst FROM imdb.title_director WHERE tconst = 'tt0000008' "
                             "ORDER BY ordinal").fetchall()) == [("tt0000008", 1, "nm0000006"),
                                                                ("tt0000008", 2, "nm0000001")]
    assert {c for (c,) in db.execute("SELECT category FROM imdb.title_principal").fetchall()} == {
        "actor", "actress", "director"}
    # 人名只含被引用的人：writer（nm5）与无人引用的 nm9 不装载
    assert {n for (n,) in db.execute("SELECT nconst FROM imdb.name").fetchall()} == {
        "nm0000001", "nm0000002", "nm0000003", "nm0000004", "nm0000006"}
    # R15：其他类型只在 title_other，且不带别名、人员
    assert dict(db.execute("SELECT tconst, title_type FROM imdb.title_other").fetchall()) == fx.OTHER_TYPES_KEPT
    assert db.execute("SELECT count(*) FROM imdb.title_aka WHERE tconst IN ('tt0000004', 'tt0000005')").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM imdb.title WHERE title_type IN ('tvSeries', 'short', 'tvEpisode')"
                      ).fetchone()[0] == 0


def test_source_file_counts_hashes_and_filter_reasons(db, snap):
    load_snapshot(db, snap)
    record = json.loads((snap / "download.json").read_text())
    rows = {r[0]: r[1:] for r in db.execute(
        "SELECT file_name, rows_read, rows_kept, sha256, size_bytes, url, filter_counts FROM imdb.source_file").fetchall()}
    assert set(rows) == set(FILE_NAMES)
    for f in record["files"]:
        read, kept, sha, size, url, _ = rows[f["name"]]
        assert (read, kept) == fx.BASIC_EXPECTED[f["name"]]
        assert (sha, size, url) == (f["sha256"], f["size_bytes"], f["url"])
    basics = rows["title.basics.tsv.gz"][-1]
    assert basics == {"to_title_other": 2, "title_type:tvEpisode": 1}
    assert rows["title.principals.tsv.gz"][-1] == {"category:writer": 1, "not_in_titles": 1}
    assert rows["name.basics.tsv.gz"][-1] == {"not_referenced": 2}
    assert problems(verify_snapshot(db, fx.SNAPSHOT_ID)) == []


def test_line_numbers_point_back_into_the_file_and_null_marker_is_null(db, snap):
    load_snapshot(db, snap)
    # 第 1 行是表头，数据从第 2 行起；tt0000007 是文件里第 7 行数据，即第 8 行
    assert dict(db.execute("SELECT tconst, line_no FROM imdb.title").fetchall()) == {
        "tt0000001": 2, "tt0000002": 3, "tt0000003": 4, "tt0000007": 8, "tt0000008": 9}
    with gzip.open(snap / "title.basics.tsv.gz", "rt", encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    assert lines[8 - 1].startswith("tt0000007\t")
    row = db.execute("SELECT original_title, is_adult, start_year, end_year, runtime_minutes, genres "
                     "FROM imdb.title WHERE tconst = 'tt0000007'").fetchone()
    assert row == (None, True, None, None, None, None)
    assert "\\N" not in "".join(str(v) for r in db.execute("SELECT * FROM imdb.title").fetchall() for v in r)
    assert db.execute("SELECT genres FROM imdb.title WHERE tconst = 'tt0000001'").fetchone()[0] == ["Drama", "Romance"]
    # 别名与导演、演员、人名同样带文件行号
    assert db.execute("SELECT line_no FROM imdb.title_aka WHERE tconst = 'tt0000008' AND ordering = 1"
                      ).fetchone()[0] == 4
    assert db.execute("SELECT line_no FROM imdb.name WHERE nconst = 'nm0000006'").fetchone()[0] == 7


def test_non_ascii_alias_is_kept_intact(db, snap):
    load_snapshot(db, snap)
    titles = {t for (t,) in db.execute("SELECT title FROM imdb.title_aka").fetchall()}
    assert {"卡萨布兰卡", "À bout de souffle"} <= titles
    row = db.execute("SELECT region, language, is_original FROM imdb.title_aka WHERE title = 'À bout de souffle'"
                     ).fetchone()
    assert row == ("FR", "fr", True)


@pytest.mark.parametrize("name", FILE_NAMES)
def test_altered_byte_is_rejected_and_nothing_is_written(db, snap, name):
    fx.flip_one_byte(snap, name)
    with pytest.raises(ImdbInputError, match="SHA-256"):
        load_snapshot(db, snap)
    assert _all_empty(db)


def test_size_mismatch_and_missing_file_are_rejected(db, snap, tmp_path):
    with open(snap / "title.crew.tsv.gz", "ab") as fh:
        fh.write(b"x")
    with pytest.raises(ImdbInputError, match="大小"):
        check_files(snap)
    (snap / "title.akas.tsv.gz").unlink()
    with pytest.raises(ImdbInputError, match="缺少文件"):
        check_files(snap)
    with pytest.raises(ImdbInputError, match="缺少下载记录"):
        check_files(tmp_path / "empty")
    assert _all_empty(db)


def test_reloading_the_same_snapshot_is_reused_and_changes_nothing(db, snap):
    first = load_snapshot(db, snap)
    before = {t: _count(db, t) for t in TABLES}
    loaded_at = db.execute("SELECT loaded_at FROM imdb.snapshot").fetchone()[0]
    second = load_snapshot(db, snap)
    assert (first.status, second.status) == ("loaded", "reused")
    assert {t: _count(db, t) for t in TABLES} == before
    assert db.execute("SELECT loaded_at FROM imdb.snapshot").fetchone()[0] == loaded_at
    assert second.files == first.files


def test_same_snapshot_id_with_different_content_is_rejected(db, snap, tmp_path):
    load_snapshot(db, snap)
    changed = {**fx.BASIC, "names": fx.BASIC["names"] + [["nm0000010", "Extra Person", None, None, None, None]]}
    other = fx.build_snapshot(tmp_path / "other" / "2026-10-30", **changed)
    with pytest.raises(ImdbInputError, match="哈希"):
        load_snapshot(db, other)
    assert _count(db, "name") == 5


def test_failure_before_commit_rolls_everything_back_and_rerun_succeeds(db, snap):
    def boom():
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        load_snapshot(db, snap, before_commit=boom)
    assert all(_count(db, t) == 0 for t in TABLES if t != "snapshot")
    status, reason = db.execute("SELECT status, failure_reason FROM imdb.snapshot").fetchone()
    assert status == "failed" and "injected" in reason
    assert load_snapshot(db, snap).status == "loaded"
    assert _count(db, "title") == 5 and _count(db, "snapshot") == 1


def test_bad_header_and_bad_row_width_fail_without_partial_rows(db, tmp_path):
    bad_header = dict(fx.BASIC, crew=fx.BASIC["crew"])
    d1 = fx.build_snapshot(tmp_path / "h" / "2026-10-30", **bad_header)
    # 重写 crew 文件的表头并更新下载记录
    fx.write_gz(d1 / "title.crew.tsv.gz", ("tconst", "directors"), [["tt0000001", "nm0000001"]])
    _refresh_record(d1, "title.crew.tsv.gz")
    with pytest.raises(ImdbInputError, match="表头"):
        load_snapshot(db, d1)
    assert all(_count(db, t) == 0 for t in TABLES if t != "snapshot")

    d2 = fx.build_snapshot(tmp_path / "w" / "2026-10-30", **fx.BASIC)
    fx.write_gz(d2 / "name.basics.tsv.gz", imdb.HEADERS["name.basics.tsv.gz"], [["nm0000001", "Only", "Three"]])
    _refresh_record(d2, "name.basics.tsv.gz")
    with pytest.raises(ImdbInputError, match="第 2 行有 3 列"):
        load_snapshot(db, d2)
    assert all(_count(db, t) == 0 for t in TABLES if t != "snapshot")


def _refresh_record(directory, name):
    import hashlib

    path = directory / "download.json"
    record = json.loads(path.read_text())
    raw = (directory / name).read_bytes()
    for f in record["files"]:
        if f["name"] == name:
            f.update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    path.write_text(json.dumps(record))


def test_concurrent_load_of_the_same_snapshot_is_busy(db, snap, base_settings):
    key = imdb._lock_key(fx.SNAPSHOT_ID)
    with connect(base_settings) as other:
        assert other.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
        with pytest.raises(ImdbBusy):
            load_snapshot(db, snap)
        other.execute("SELECT pg_advisory_unlock(%s)", (key,))
    assert load_snapshot(db, snap).status == "loaded"


def test_verify_reports_problems_when_rows_go_missing(db, snap):
    load_snapshot(db, snap)
    db.execute("DELETE FROM imdb.name WHERE nconst = 'nm0000002'")
    found = problems(verify_snapshot(db, fx.SNAPSHOT_ID))
    assert any("name.basics" in p for p in found) and any("dangling_principal_names" in p for p in found)


def test_snapshot_directory_mapping_and_id_check(tmp_path):
    assert snapshot_dir("imdb_2026-10-30", tmp_path) == tmp_path / "2026-10-30"
    for bad in ("2026-10-30", "imdb_", "imdb_../x", "imdb_a b"):
        with pytest.raises(ImdbInputError):
            snapshot_dir(bad, tmp_path)


def test_apply_imdb_schema_is_idempotent(conn):
    apply_imdb_schema(conn)
    assert apply_imdb_schema(conn) is False
    versions = {v for (v,) in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
    assert "301" in versions


def test_a_few_dangling_names_in_a_large_file_are_warnings_not_problems():
    """真实文件里有极少量引用了 name.basics 没有的人：占比低于容忍值只记警告，超过才是问题。"""
    from etl_task4 import imdb

    def out(dangling):
        files = {n: {"rows_read": 1, "rows_kept": 5, "table_rows": 5} for n in imdb.FILE_NAMES}
        files["title.principals.tsv.gz"].update(rows_kept=1_000_000, table_rows=1_000_000)
        return {"status": "loaded", "files": files, "title_other_rows": 0, "title_other_expected": 0,
                "title_overlap_other": 0, "dangling_director_names": 0, "dangling_principal_names": dangling}

    assert imdb.problems(out(618)) == [] and "618" in imdb.warnings(out(618))[0]
    assert any("dangling_principal_names" in p for p in imdb.problems(out(5000)))
