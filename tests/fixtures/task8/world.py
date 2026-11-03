"""任务 8 合成夹具的规格类（全部为手写的合成评论，非真实用户与商品）。

测试用 `_task8_helpers.load` 把一组 `Rec` 写成 movies.txt 格式的 gzip，再用任务 1 的装载器（etl_task1.loader）
真正装进 etl_test，所以 review_record、review_staging、quarantine_record、quality_issue 的列、规范值、隔离分类与
质量问题都由任务 1 的代码生成，与正式库同形（README.md）。预期值按 spec 的 AC 表手算，写在各测试里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import build_fixtures as bf

DAY = 1182729600            # 2007-06-25 00:00:00 UTC：真实数据的评论时间都是 UTC 零点
ONE_DAY = 86400


def unix(days: int = 0) -> str:
    """DAY 之后第 days 天零点的 Unix 秒（文本，与源文件写法相同）。"""
    return str(DAY + days * ONE_DAY)


@dataclass
class Rec:
    """一条源记录。字段为 None 表示省略该行（任务 1 记为缺失）；raw_text 覆盖正文字节（如非法 UTF-8）；
    extra 为附加的原始行（如无法识别的行，任务 1 隔离为 unknown_line）。"""
    asin: str | None
    user: str
    profile: str | None = "Profile"
    helpfulness: str | None = "1/2"
    score: str | None = "5.0"
    time: str | None = field(default_factory=unix)
    summary: str | None = "Summary"
    text: str | None = "Review text"
    raw_text: bytes | None = None
    extra: tuple[bytes, ...] = ()

    def lines(self) -> list[bytes]:
        enc = lambda v: None if v is None else v.encode("utf-8")      # noqa: E731
        text = self.raw_text if self.raw_text is not None else enc(self.text)
        return bf.record(product=enc(self.asin), user=enc(self.user), profile=enc(self.profile),
                         helpfulness=enc(self.helpfulness), score=enc(self.score), time=enc(self.time),
                         summary=enc(self.summary), text=text) + list(self.extra)


def copies(asins: list[str], user: str, **kw) -> list[Rec]:
    """同一条评论在多个 ASIN 上各一份（平台复制）。"""
    return [Rec(a, user, **kw) for a in asins]
