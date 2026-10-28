"""任务 2 候选链接构造（AC03）。"""

import pytest

from etl_task2.urls import URL_TEMPLATE_VERSION, build_candidate


@pytest.mark.parametrize("asin", ["B000000743", "0001489305", "000148930X"])
def test_valid_asin_gets_dp_url(asin):
    cand = build_candidate(asin)
    assert cand.original_url == f"https://www.amazon.com/dp/{asin}"
    assert cand.url_issue is None


@pytest.mark.parametrize("asin", ["b000000743", "B00000 743", " B000000743", "B000000743\n",
                                  "B00000074", "B0000007433", "B00000/743", "", "Ｂ000000743"])
def test_nonconforming_asin_gets_no_url_and_is_not_rewritten(asin):
    original = str(asin)
    cand = build_candidate(asin)
    assert cand.original_url is None
    assert cand.url_issue and cand.url_issue.startswith("asin_format")
    assert asin == original


def test_candidate_is_deterministic():
    for asin in ("B000000743", "0001489305", "b000000743"):
        assert build_candidate(asin) == build_candidate(asin)
    assert URL_TEMPLATE_VERSION == "dp-v1"
