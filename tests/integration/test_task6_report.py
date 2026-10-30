"""任务 6 统计、导出、实例与查询（spec AC16）。

命令的工作目录为项目根目录：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_report.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import psycopg
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


# ---------------------------------------------------------------- AC22 报告口径、只读查询与核对修复

def test_ac22_summary_title_types_shared_counts_stage_key_and_run_id(conn, ran, tmp_path):
    """AC22（ISSUE-06）：
    1. candidates.title_types 读取 source_ref->>'title_type'（兼容 titleType），正确统计 movie/tvMovie/video 等分布且非全 'null'；
    2. candidate_shared_count 使用独立 COUNT(*) 查询，超过 20 条时不被 LIMIT 20 样本列表截断；
    3. 同一 extraction_id 下存在多套阶段键数据时，build_summary 按当前版本的 stage_key 过滤；
    4. 重跑 reused 后默认仍读取最新 succeeded 运行的真实 run_id 与 stage_counts，显式传入不存在的 run_id 报错拒绝；
       build_and_write_report 默认使用真实 run_id 而非硬编码 't6_full_1'。
    """
    w, rv = ran
    s = report.build_summary(conn, rv)

    # 1. title_types 不再全落入 'null'
    tt = s["candidates"]["title_types"]
    assert "movie" in tt and tt["movie"] > 0
    assert set(tt.keys()) != {"null"}

    # 2. 注入 >20 条 candidate_shared 记录（当前 candidates stage_key），验证 candidate_shared_count 不被 LIMIT 20 截断
    inp = inputs_for(w)
    cand_key = inp.stage_key("candidates")
    owner_mid = one(conn, "SELECT movie_id FROM task4.movie WHERE mapping_version_id = %s LIMIT 1", w.mapping)
    for i in range(25):
        conn.execute(
            "INSERT INTO task6.hint_use (extraction_id, stage_key, asin, member_ordinal, tconst, movie_id, "
            "action, owner_movie_id, run_id) VALUES (%s, %s, %s, 0, 'tt0006001', NULL, "
            "'candidate_shared', %s, %s)",
            (w.extraction_id, cand_key, f"B0T6SH{i:04d}", owner_mid, s["run_id"]),
        )
    # 3. 同时注入另一套 stage_key 的噪声行，确认不会串入当前版本统计
    conn.execute(
        "INSERT INTO task6.hint_use (extraction_id, stage_key, asin, member_ordinal, tconst, movie_id, "
        "action, owner_movie_id, run_id) VALUES (%s, 'other_stage_key', 'B0T6OTHER1', 0, 'tt0006001', NULL, "
        "'candidate_shared', %s, %s)",
        (w.extraction_id, owner_mid, s["run_id"]),
    )
    s2 = report.build_summary(conn, rv)
    assert s2["hint_use"]["candidate_shared_count"] >= 25
    assert len(s2["hint_use"]["candidate_shared_top"]) == 20
    assert s2["shared_tconst_summary"]["candidate_shared_count"] == s2["hint_use"]["candidate_shared_count"]

    # 4. 连续第二次运行（status='reused'）后，build_summary 默认仍取首次 succeeded 运行的 run_id 与非空 stage_counts
    reused_res = execute_release(conn, inp, batch_size=7)
    assert reused_res.status == "reused"
    s_after_reuse = report.build_summary(conn, rv)
    assert s_after_reuse["run_id"] == s["run_id"]
    assert s_after_reuse["stage_counts"]

    # build_and_write_report 默认写出目录名使用真实 succeeded run_id，而非硬编码 t6_full_1
    rep_data, rep_dir, _ = report.build_and_write_report(conn, rv, tmp_path / "out_ac22")
    assert rep_dir.name == s["run_id"] and rep_dir.name != "t6_full_1"
    assert rep_data["run_id"] == s["run_id"]

    # 显式传入不存在的 run_id 报错拒绝
    with pytest.raises(ValueError, match="不存在"):
        report.build_summary(conn, rv, run_id="t6_nonexistent_run_999")


def test_ac22_query_movie_returns_product_review_time_and_cooccur_details(conn, ran):
    """AC22（ISSUE-09）：query_movie 补齐商品级最早评论摘要 product_review_time，
    并在 cooccur_score 中返回 match_key_source、matched_title_key、product_title_key、shared_tconst。"""
    w, rv = ran
    cooc_mid = one(
        conn,
        "SELECT movie_id FROM task6.release_selection WHERE release_version_id = %s "
        "AND status = 'cooccur_matched' LIMIT 1",
        rv,
    )
    res = query.query_movie(conn, rv, cooc_mid)
    assert "product_review_time" in res and isinstance(res["product_review_time"], list)
    assert "cooccur_score" in res and len(res["cooccur_score"]) > 0
    top_score = res["cooccur_score"][0]
    for field in ("match_key_source", "matched_title_key", "product_title_key", "shared_tconst", "number_guard_detail"):
        assert field in top_score
    assert top_score["match_key_source"] in ("primary", "original", "aka")
    assert top_score["matched_title_key"] is not None
    assert top_score["product_title_key"] is not None
    assert isinstance(top_score["shared_tconst"], bool)

    # 找一部有评论的商品所属作品，验证 product_review_time 非空且包含最早评论时间与条数
    rev_mid = one(
        conn,
        "SELECT pm.movie_id FROM task4.product_movie pm JOIN task6.product_review_time prt "
        "ON prt.extraction_id = %s AND prt.asin = pm.asin WHERE pm.mapping_version_id = %s "
        "AND pm.relation_type = 'edition' LIMIT 1",
        w.extraction_id, w.mapping,
    )
    rev_res = query.query_movie(conn, rv, rev_mid)
    assert len(rev_res["product_review_time"]) > 0
    first_prt = rev_res["product_review_time"][0]
    assert first_prt["asin"] in rev_res["edition_asins"]
    assert first_prt["review_count"] >= 1
    assert first_prt["earliest_time"] is not None


def test_ac22_check_detects_bound_in_selection_and_invalid_cooccur_matched(conn, ran):
    """AC22（ISSUE-10）：check.verify 能真正检出：
    1. 将推断上界年份（EXTRACT(YEAR FROM bound_time)::int）伪装写入 release_selection 的违规记录；
    2. cooccur_matched 作品在当前共现阶段键下缺少 passed = TRUE 的 cooccur_score 记录。
    """
    w, rv = ran
    info_ok = check.verify(conn, rv)
    assert check.problems(info_ok) == []

    # 1. 构造推断上界年份直接混入选择记录（无合法候选支撑且等于 bound_time 年份）
    missing_with_bound = one(
        conn,
        "SELECT s.movie_id, EXTRACT(YEAR FROM b.bound_time)::int FROM task6.release_selection s "
        "JOIN task6.inferred_release_bound b ON b.release_version_id = s.release_version_id "
        "AND b.movie_id = s.movie_id WHERE s.release_version_id = %s AND s.status = 'missing' LIMIT 1",
        rv,
    )
    mid_b = conn.execute(
        "SELECT s.movie_id, EXTRACT(YEAR FROM b.bound_time)::int FROM task6.release_selection s "
        "JOIN task6.inferred_release_bound b ON b.release_version_id = s.release_version_id "
        "AND b.movie_id = s.movie_id WHERE s.release_version_id = %s AND s.status = 'missing' LIMIT 1",
        (rv,),
    ).fetchone()
    assert mid_b is not None
    with conn.transaction():
        conn.execute(
            "UPDATE task6.release_selection SET status = 'verified', release_year = %s, missing_reason = NULL "
            "WHERE release_version_id = %s AND movie_id = %s",
            (mid_b[1], rv, mid_b[0]),
        )
        info_bad_bound = check._verify(conn, rv)
        assert info_bad_bound["bounds_in_selection"] >= 1
        assert any("推断上界写入了选择年份" in p for p in check.problems(info_bad_bound))
        raise psycopg.Rollback()

    # 2. 将一部 cooccur_matched 作品在 cooccur_score 中的 passed 篡改为 FALSE，验证 check 检出违规
    cooc_mid = one(
        conn,
        "SELECT movie_id FROM task6.release_selection WHERE release_version_id = %s "
        "AND status = 'cooccur_matched' LIMIT 1",
        rv,
    )
    with conn.transaction():
        conn.execute(
            "UPDATE task6.cooccur_score SET passed = FALSE WHERE extraction_id = %s AND movie_id = %s",
            (w.extraction_id, cooc_mid),
        )
        info_bad_cooc = check._verify(conn, rv)
        assert info_bad_cooc["cooccur_matched_invalid"] >= 1
        assert any("cooccur_matched" in p for p in check.problems(info_bad_cooc))
        raise psycopg.Rollback()
