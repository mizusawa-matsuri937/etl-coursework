"""任务 6 统计、导出、实例与查询（spec AC16）。

命令的工作目录为项目根目录：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_report.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import _task6_helpers as h
from _task6_helpers import World, inputs_for
from etl_task1 import reporting
from etl_task6 import check, query, report
from etl_task6.runner import execute_release

pytestmark = pytest.mark.integration


@pytest.fixture
def ran(conn, make_settings, tmp_path):
    w = h.make_world(conn, make_settings, tmp_path)
    res = execute_release(conn, inputs_for(w), batch_size=7)
    assert res.status == "succeeded"
    return w, res.release_version_id


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


# ---------------------------------------------------------------- AC16 统计与报告核对

def test_summary_matches_database(conn, ran):
    """summary.json 的各项计数与库中一致（R33）。"""
    w, rv = ran
    s = report.build_summary(conn, rv)
    m = s["movies"]
    t4 = w.mapping

    # 作品数与状态
    assert m["total"] == one(conn, "SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", t4)
    assert m["by_status"] == dict(conn.execute(
        "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    assert m["missing_reasons"] == dict(conn.execute(
        "SELECT missing_reason, count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "AND status = 'missing' GROUP BY 1 ORDER BY 1", (rv,)).fetchall())

    # 覆盖率
    cov = s["coverage"]
    assert cov["works_with_year"] == sum(v for k, v in m["by_status"].items() if k != "missing")
    assert cov["works_total"] == m["total"]

    # 候选
    assert s["candidates"]["by_source"] == dict(conn.execute(
        "SELECT source, count(*) FROM task6.release_candidate WHERE extraction_id = %s "
        "GROUP BY 1 ORDER BY 1", (w.extraction_id,)).fetchall())

    # 上界
    b = s["bounds"]
    assert b["assessments_total"] == one(conn, "SELECT count(*) FROM task6.bound_assessment WHERE release_version_id = %s", rv)
    assert sum(b["by_evidence"].values()) == one(conn, "SELECT count(*) FROM task6.inferred_release_bound WHERE release_version_id = %s", rv)


def test_examples_categories_and_locators(conn, ran):
    """实例部分按 R35 的各类列出夹具中的作品并附来源定位。"""
    w, rv = ran
    ex = report.examples(conn, rv)
    for k in ("verified", "hint_matched", "cooccur_matched", "page_reported", "missing",
              "inferred_review", "inferred_media", "failed_review", "failed_media", "coexisting"):
        assert k in ex and len(ex[k]) > 0
        for item in ex[k]:
            assert "movie_id" in item and "title" in item


def test_export_files_and_digests(conn, ran, tmp_path):
    """导出所有文件，哈希记录一致，导出内容不含评论正文（R33）。"""
    w, rv = ran
    out_dir = tmp_path / "export_test"
    hashes = report.export_files(conn, rv, out_dir)

    expected_files = {
        "summary.json", "report.md", "examples.md", "release_selection.csv",
        "release_candidates.csv", "cooccur.csv", "product_release_dates.csv",
        "bounds.csv", "hint_use.csv",
    }
    assert set(hashes.keys()) == expected_files

    for fname, digest in hashes.items():
        fpath = out_dir / fname
        assert fpath.exists()
        assert reporting.file_digest(fpath) == digest

    # 检查不含评论正文
    for fname in expected_files:
        content = (out_dir / fname).read_bytes()
        assert b"Saw this long ago" not in content
        assert b"Plain review text" not in content


# ---------------------------------------------------------------- AC16 两个只读查询

def test_query_movie_returns_full_lineage(conn, ran):
    """query movie 返回候选、选择、共现评分、上界与商品发售日（R34）。"""
    w, rv = ran
    # 取一个有共现反查的作品
    movie_id = one(conn, "SELECT movie_id FROM task6.release_selection WHERE release_version_id = %s "
                        "AND status = 'cooccur_matched' LIMIT 1", rv)
    res = query.query_movie(conn, rv, movie_id)
    assert res["found"] is True
    assert res["selection"]["status"] == "cooccur_matched"
    assert len(res["candidates"]) > 0
    assert "cooccur" in res and res["cooccur"]["result"] is not None
    assert "edition_asins" in res


def test_query_gaps_filters(conn, ran):
    """query gaps 按状态筛选缺失与推断上界作品（R34）。"""
    w, rv = ran
    missing = query.query_gaps(conn, rv, "missing")
    assert all(r["status"] == "missing" for r in missing)
    assert len(missing) == one(conn, "SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s "
                                    "AND status = 'missing'", rv)

    inferred = query.query_gaps(conn, rv, "inferred")
    assert all(r["inferred_bound"] is not None for r in inferred)
    assert len(inferred) == one(conn, "SELECT count(*) FROM task6.inferred_release_bound WHERE release_version_id = %s", rv)

    all_gaps = query.query_gaps(conn, rv)
    assert len(all_gaps) >= len(missing)
