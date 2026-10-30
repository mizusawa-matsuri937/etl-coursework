"""任务 6 的最早评论时间（spec AC10）。

预期值按夹具手算，见 tests/fixtures/task6/README.md。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_reviews.py
"""

from __future__ import annotations

import pytest
import _task6_helpers as h

pytestmark = pytest.mark.integration


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return h.make_world(conn, make_settings, tmp_path)


@pytest.fixture
def full(conn, world):
    return h.run_stages(conn, world, batch_size=7)


def _movie_of(conn, world, asin, ordinal=0):
    return conn.execute("SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s "
                        "AND member_ordinal = %s", (world.mapping, asin, ordinal)).fetchone()[0]


def _movie_reviews(conn, world, movie):
    return conn.execute("SELECT earliest_review_time, earliest_precision, earliest_source_review_id, "
                        "earliest_asin, second_review_time, review_count, reviewed_asin_count, "
                        "edition_asin_count FROM task6.movie_review_time WHERE extraction_id = %s AND "
                        "t4_mapping_version_id = %s AND movie_id = %s",
                        (world.extraction_id, world.mapping, movie)).fetchone()


def _product_reviews(conn, world, asin):
    return conn.execute("SELECT product_kind, earliest_time, earliest_source_review_id, second_time, "
                        "review_count FROM task6.product_review_time WHERE extraction_id = %s AND asin = %s",
                        (world.extraction_id, asin)).fetchone()


def test_ac10_movie_earliest_and_second(conn, world, full):
    """作品 J：最早 2000-12-01（B0T6FLJ002）、第二早 2001-03-02、评论数 3、有评论商品数 2。"""
    ctx, rv = full
    j = _movie_of(conn, world, "B0T6FLJ001")
    row = _movie_reviews(conn, world, j)
    assert row[0].date().isoformat() == "2000-12-01" and row[1] == "day"
    assert row[3] == "B0T6FLJ002"
    assert row[2] == conn.execute("SELECT source_review_id FROM review_staging WHERE extraction_id = %s AND "
                                  "product_id = 'B0T6FLJ002' ORDER BY review_time_utc LIMIT 1",
                                  (world.extraction_id,)).fetchone()[0]
    assert row[4].date().isoformat() == "2001-03-02"
    assert (row[5], row[6], row[7]) == (3, 2, 2)


def test_ac10_collection_reviews_stay_at_product_level(conn, world, full):
    """合集评论（1999-01-01）只在合集商品的商品层，不进 J 的作品层。"""
    ctx, rv = full
    prod = _product_reviews(conn, world, "B0T6FLJ003")
    assert prod[0] == "collection" and prod[1].date().isoformat() == "1999-01-01" and prod[4] == 1
    j = _movie_of(conn, world, "B0T6FLJ001")
    row = _movie_reviews(conn, world, j)
    assert row[5] == 3                                  # 两条 + 一条，不含合集评论
    # 合集的成员作品（K2，只经 contains）没有评论行
    k2 = _movie_of(conn, world, "B0T6FLJ003", 2)
    assert _movie_reviews(conn, world, k2) is None


def test_ac10_movie_without_reviews(conn, world, full):
    """作品 K（评论全部隔离）：没有最早评论行，计入覆盖率缺失。"""
    ctx, rv = full
    k = _movie_of(conn, world, "B0T6FLK001")
    assert _movie_reviews(conn, world, k) is None
    assert _product_reviews(conn, world, "B0T6FLK001") is None
    # 隔离记录在任务 1 的表里存在，但不是有效评论
    n = conn.execute("SELECT count(*) FROM quarantine_record qr JOIN review_record rr "
                     "USING (extraction_id, source_review_id) WHERE rr.extraction_id = %s AND rr.product_id_observed = 'B0T6FLK001'",
                     (world.extraction_id,)).fetchone()[0]
    assert n == 1


def test_ac10_product_level_two_versions(conn, world, full):
    """商品层：FLJ001 两条（2001-03-02、2001-05-10），FLJ002 一条（2000-12-01）。"""
    ctx, rv = full
    a = _product_reviews(conn, world, "B0T6FLJ001")
    assert a[0] == "movie" and a[1].date().isoformat() == "2001-03-02" and a[4] == 2
    b = _product_reviews(conn, world, "B0T6FLJ002")
    assert b[1].date().isoformat() == "2000-12-01" and b[4] == 1 and b[3] is None
