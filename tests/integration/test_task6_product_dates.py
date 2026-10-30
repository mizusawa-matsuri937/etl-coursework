"""任务 6 的商品发售日期与标记（spec AC09）。

预期值按夹具手算，见 tests/fixtures/task6/README.md。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_product_dates.py
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


def _dates(conn, world, asin):
    return conn.execute("SELECT assertion_id, kind, label_raw, raw_value, value_date, precision, flags, section, "
                        "page_ordinal, source_record_id FROM task6.product_release_date WHERE extraction_id = %s "
                        "AND asin = %s ORDER BY assertion_id", (world.extraction_id, asin)).fetchall()


# AC09 的五个案例分布在四个商品上：A10001（Release date 2003-05-20）、A10002（Release date 2001-01-01）、
# FLC001（Publication date 2002-03-04）、FLC002（Date First Available 2004-07-09）、FLE001（Release date 2014-02-11）。

def test_ac09_release_date_plain(conn, world, full):
    rows = _dates(conn, world, "B0T6A10001")
    assert len(rows) == 1
    r = rows[0]
    assert (r[1], r[2], r[3], str(r[4]), r[5], r[6]) == ("media_release", "Release date", "May 20, 2003",
                                                         "2003-05-20", "day", [])
    assert (r[7], r[8], r[9]) == ("detail_bullets", 1, r[9]) and r[9] is not None   # 页面定位与源记录


def test_ac09_release_date_jan_first_suspect(conn, world, full):
    rows = _dates(conn, world, "B0T6A10002")
    assert (rows[0][1], str(rows[0][4]), rows[0][5], rows[0][6]) == ("media_release", "2001-01-01", "day",
                                                                     ["jan_first_suspect"])


def test_ac09_publication_date_is_media_release(conn, world, full):
    rows = _dates(conn, world, "B0T6FLC001")
    assert (rows[0][1], rows[0][2], str(rows[0][4])) == ("media_release", "Publication date", "2002-03-04")


def test_ac09_date_first_available_is_listing(conn, world, full):
    rows = _dates(conn, world, "B0T6FLC002")
    assert (rows[0][1], rows[0][2], str(rows[0][4]), rows[0][6]) == ("listing_first_available",
                                                                     "Date First Available", "2004-07-09", [])


def test_ac09_after_review_period(conn, world, full):
    rows = _dates(conn, world, "B0T6FLE001")
    assert (rows[0][1], str(rows[0][4]), rows[0][6]) == ("media_release", "2014-02-11", ["after_review_period"])


def test_ac09_dates_never_feed_year_selection(conn, world, full):
    """商品发售日期的值不出现在任何作品的年份候选或选择记录里（R17）。"""
    ctx, rv = full
    values = {str(r[0]) for r in conn.execute("SELECT value_date FROM task6.product_release_date")}
    years = {str(r[0]) for r in conn.execute("SELECT raw_value FROM task6.release_candidate")}
    # 年份候选的 raw_value 全是四位年份，与日期值无交集；选择记录的年份只来自候选
    assert not (values & years)
    bad = conn.execute("SELECT count(*) FROM task6.release_selection s JOIN task6.release_candidate c "
                       "ON c.candidate_id = s.selected_candidate_id WHERE c.extraction_id = %s AND "
                       "c.source = 'edition.product_release_date'", (world.extraction_id,)).fetchone()[0]
    assert bad == 0
    # 非参与匹配的剧集（有页面日期）没有商品发售日期行
    assert _dates(conn, world, "B0T6TV0001") == []
