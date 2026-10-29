"""产品页字段抽取（spec R06、R07）：HTML 字节 → 证据区域内的原始字段列表。纯函数，不访问数据库。

只看 7 个证据区域；先删除 script/style/noscript，导航、推荐、评论、描述等区域的文字一律不取。
值只压缩空白、去掉不可见方向符，不做规范化；标签按 LABEL_MAP 映射到字段键，未知标签的字段键为 None。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import html as lxml_html

SECTIONS = ("title", "breadcrumb", "detail_bullets", "product_overview", "product_details", "byline",
            "format_swatch")

_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿]")
_SPACE = re.compile(r"\s+")

# (区域, 标签小写) → 字段键；版本见 etl_task3.LABEL_MAP_VERSION（design“字段键与标签映射”）
_BY_LABEL = {
    "director": "director", "actors": "actors", "writers": "writers", "producers": "producers",
    "studio": "studio", "manufacturer": "studio", "media format": "media_format", "format": "media_format",
    "run time": "runtime", "runtime": "runtime", "mpaa rating": "mpaa_rating", "rated": "mpaa_rating",
    "genre": "genre", "language": "language", "subtitles": "subtitles", "subtitle": "subtitles",
    "dubbed": "dubbed", "number of discs": "number_of_discs", "aspect ratio": "aspect_ratio",
    "release date": "product_release_date", "date first available": "product_release_date",
    "publication date": "product_release_date", "upc": "upc", "edition": "edition",
    "contributor": "contributor",
}
# 署名行里的角色（括号内）→ 字段键
_BY_ROLE = {"director": "director", "actor": "actors", "writer": "writers", "producer": "producers"}


@dataclass(frozen=True)
class Field:
    section: str
    ordinal: int            # 区域内从 1 开始的顺序
    label_raw: str | None   # 标题、面包屑、已选格式没有标签
    value_raw: str
    field_key: str | None


def clean(text: str | None) -> str:
    return _SPACE.sub(" ", _INVISIBLE.sub("", text or "")).strip()


def _label(text: str) -> str:
    """标签去掉末尾的冒号与空白（页面写作 “Dubbed: ‏ : ‎”）。"""
    return re.sub(r"[\s:]+$", "", clean(text))


def field_key(section: str, label: str | None) -> str | None:
    if section == "title":
        return "title"
    if section == "breadcrumb":
        return "breadcrumb"
    if section == "format_swatch":
        return "media_format"
    if label is None:
        return None
    if section == "byline":
        return _BY_ROLE.get(label.lower(), "contributor") if label.lower() not in ("rated", "format") \
            else _BY_LABEL[label.lower()]
    return _BY_LABEL.get(label.lower())


def _text(el) -> str:
    return clean("".join(el.itertext()))


def _first(doc, element_id: str):
    found = doc.xpath(f'//*[@id="{element_id}"]')
    return found[0] if found else None


def parse_document(body: bytes):
    parser = lxml_html.HTMLParser(encoding="utf-8", remove_comments=True)
    doc = lxml_html.document_fromstring(body, parser=parser)
    for el in doc.xpath("//script|//style|//noscript"):
        el.drop_tree()
    return doc


def _detail_bullets(root) -> list[tuple[str, str]]:
    out = []
    for li in root.xpath('.//li'):
        bold = li.xpath('.//span[contains(concat(" ", normalize-space(@class), " "), " a-text-bold ")]')
        if not bold:
            continue
        label = _label(_text(bold[0]))
        parts = [bold[0].tail or ""] + ["".join(sib.itertext()) + (sib.tail or "")
                                         for sib in bold[0].itersiblings()]
        value = clean("".join(parts))
        if label:
            out.append((label, value))
    return out


def _rows(root, label_xpath: str, value_xpath: str) -> list[tuple[str, str]]:
    out = []
    for tr in root.xpath(".//tr"):
        k, v = tr.xpath(label_xpath), tr.xpath(value_xpath)
        if k and v:
            label = _label(_text(k[0]))
            if label:
                out.append((label, _text(v[0])))
    return out


def _byline(root) -> list[tuple[str, str]]:
    """署名按页面顺序：每个角色一行（“(Director, Writer)” 拆成两行）；“Rated:”“Format:” 各一行。"""
    out = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        cls = f" {el.get('class', '')} "
        if " author " in cls:
            name = el.xpath('./a') or el.xpath('.//a')
            contrib = el.xpath('.//span[contains(@class, "contribution")]')
            who = _text(name[0]) if name else ""
            roles = clean(_text(contrib[0]) if contrib else "").strip(" ,()")
            for role in (r.strip(" ()") for r in roles.split(",")):
                if who:
                    out.append((role or "Contributor", who))
        elif " a-color-secondary " in cls and _label(_text(el)) in ("Rated", "Format") \
                and not el.xpath('ancestor::*[contains(@class, "author")]'):
            label = _label(_text(el))
            value = ""
            for sib in el.itersiblings():  # 值在紧跟的兄弟元素里（Rated 在一个盒子里，Format 在 span 里）
                if sib.tag == "i":
                    break
                value = _text(sib)
                if value:
                    break
            out.append((label, value))
    return out


def extract_fields(body: bytes) -> tuple[list[Field], dict[str, bool]]:
    """返回字段列表与 7 个区域各自是否存在。"""
    doc = parse_document(body)
    present: dict[str, bool] = {}
    fields: list[Field] = []

    def add(section: str, items: list[tuple[str | None, str]]) -> None:
        for i, (label, value) in enumerate(items, start=1):
            fields.append(Field(section, i, label, value, field_key(section, label)))

    title = _first(doc, "productTitle")
    present["title"] = title is not None
    if title is not None:
        add("title", [(None, _text(title))])

    crumbs = _first(doc, "wayfinding-breadcrumbs_feature_div")
    present["breadcrumb"] = crumbs is not None
    if crumbs is not None:
        add("breadcrumb", [(None, _text(a)) for a in crumbs.xpath(".//li//a") if _text(a)])

    bullets = _first(doc, "detailBullets_feature_div")
    present["detail_bullets"] = bullets is not None
    if bullets is not None:
        add("detail_bullets", _detail_bullets(bullets))

    overview = _first(doc, "productOverview_feature_div")
    present["product_overview"] = overview is not None
    if overview is not None:
        add("product_overview", _rows(overview, "./td[1]", "./td[2]"))

    tables = doc.xpath('//table[starts-with(@id, "productDetails_")]')
    present["product_details"] = bool(tables)
    rows: list[tuple[str, str]] = []
    for t in tables:
        rows += _rows(t, "./th", "./td")
    add("product_details", rows)

    byline = _first(doc, "bylineInfo")
    present["byline"] = byline is not None
    if byline is not None:
        add("byline", _byline(byline))

    swatches = _first(doc, "tmmSwatches")
    present["format_swatch"] = swatches is not None
    if swatches is not None:
        chosen = []
        for li in swatches.xpath('.//li[contains(concat(" ", normalize-space(@class), " "), " selected ")]'):
            name = li.xpath('(.//a//span[@aria-label])[1]') or li.xpath('.//span[contains(@class, "slot-title")]')
            value = _text(name[0]) if name else _text(li)
            if value:
                chosen.append((None, value))
        add("format_swatch", chosen)
    return fields, present
