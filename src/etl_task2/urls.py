"""候选链接构造（spec R04–R06）：纯函数，不访问网络。"""

from __future__ import annotations

import re
from typing import NamedTuple

URL_TEMPLATE = "https://www.amazon.com/dp/{asin}"
URL_TEMPLATE_VERSION = "dp-v1"

ASIN_PATTERN = re.compile(r"[0-9A-Z]{10}")


class Candidate(NamedTuple):
    original_url: str | None
    url_issue: str | None


def build_candidate(asin: str) -> Candidate:
    """格式合规的 ASIN 按模板生成链接；否则不生成链接并说明原因，不改写 ASIN。"""
    if ASIN_PATTERN.fullmatch(asin):
        return Candidate(URL_TEMPLATE.format(asin=asin), None)
    return Candidate(None, f"asin_format: 不符合 ^[0-9A-Z]{{10}}$（长度 {len(asin)}）")
