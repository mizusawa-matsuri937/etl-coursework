"""商品版本、标准化与字段断言的写入（spec R02、R03、R06–R11、R16、R17、R19，AC01、AC03、AC04、AC06）。

数据流见 design“数据模型”“标准化”：
- build_editions：全部清单 ASIN 各一行商品版本（一条 INSERT … SELECT），按任务 3 类别和页面状态定匹配范围。
- normalize_extraction：参与匹配的商品（movie、collection）按 ASIN 分批，从 task3.page_field 生成字段断言
  （原值与规范值分开、指回任务 3 字段的区域与序号、派生值用 parent 指回来源断言），合集同时拆出成员实体。
  每批的来源记录、断言、质量问题、实体标记与批次审计在同一事务提交。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task3.extract import Field

from . import normalize as nz
from .run import BeforeCommit, RunContext, copy_rows, mark_batch, mark_entities, next_batch_no

STAGE = "normalize"

# 页面字段键 → 版本属性（其余的属性由标题说明段给出）
ATTRIBUTE_FIELDS = {
    "media_format": "edition.media_format", "language": "edition.language", "subtitles": "edition.subtitles",
    "dubbed": "edition.dubbed", "number_of_discs": "edition.discs", "studio": "edition.publisher",
    "product_release_date": "edition.product_release_date", "upc": "edition.upc",
    "aspect_ratio": "edition.aspect_ratio",
}
RUNTIME_SECTIONS = ("detail_bullets", "product_overview", "product_details")
ASSERTION_COLUMNS = ("assertion_id", "extraction_id", "parser_version", "norm_version", "entity_type", "entity_id",
                     "field_name", "ordinal", "raw_value", "normalized_value", "qualifier", "extra",
                     "source_record_id", "source_locator", "parent_assertion_id", "rule_id")


@dataclass
class Assertion:
    key: int                                 # 批内临时编号，写库时换成序列值
    entity_type: str
    entity_id: str
    field_name: str
    ordinal: int
    raw_value: str | None
    normalized_value: str | None
    rule_id: str
    qualifier: str | None = None
    extra: dict[str, Any] | None = None
    source_locator: dict[str, Any] | None = None
    parent: int | None = None


@dataclass
class ProductNorm:
    asin: str
    assertions: list[Assertion] = field(default_factory=list)
    issues: list[tuple[str, dict[str, Any]]] = field(default_factory=list)   # (问题编码, 细节)


class _Builder:
    def __init__(self, asin: str):
        self.out = ProductNorm(asin)
        self._ordinals: dict[tuple[str, str], int] = {}
        self._key = 0

    def add(self, entity_id: str, entity_type: str, field_name: str, raw: str | None, normalized: str | None,
            rule_id: str, **kw: Any) -> Assertion:
        n = self._ordinals[(entity_id, field_name)] = self._ordinals.get((entity_id, field_name), 0) + 1
        self._key += 1
        a = Assertion(self._key, entity_type, entity_id, field_name, n, raw, normalized, rule_id, **kw)
        self.out.assertions.append(a)
        return a


def _loc(f: Field) -> dict[str, Any]:
    return {"section": f.section, "ordinal": f.ordinal, "field_key": f.field_key}


def _title_block(b: _Builder, entity_id: str, entity_type: str, raw: str, loc: dict[str, Any] | None,
                 qualifier: str | None = None, extra: dict[str, Any] | None = None,
                 parent: int | None = None) -> Assertion:
    """标题、说明段、标题年份与说明带来的版本属性；说明、年份、属性都指回标题断言（R07、R08）。"""
    parts = nz.split_title(raw)
    title = b.add(entity_id, entity_type, "title", raw, parts.key, "title.key", qualifier=qualifier,
                  extra={**(extra or {}), "body": parts.body}, source_locator=loc, parent=parent)
    for d in parts.descriptors:
        b.add(entity_id, entity_type, "descriptor", d.raw, None, "title.descriptor", qualifier=d.category,
              extra={"position": d.position, "attributes": [list(a) for a in d.attributes]},
              source_locator=loc, parent=title.key)
        for fname, value in d.attributes:
            b.add(entity_id, entity_type, fname, d.raw, value, f"descriptor.{d.category}", source_locator=loc,
                  parent=title.key)
    if parts.title_year is not None:
        b.add(entity_id, entity_type, "title_year", str(parts.title_year), str(parts.title_year), "title.year",
              source_locator=loc, parent=title.key)
    return title


def normalize_product(asin: str, product_kind: str, fields: list[Field]) -> ProductNorm:
    """纯函数：一个商品的页面字段 → 断言与质量问题。不访问数据库。"""
    b = _Builder(asin)
    titles = [f for f in fields if f.field_key == "title"]
    title_assertion: Assertion | None = None
    if not titles:
        b.out.issues.append(("missing_title", {}))
    else:
        t = titles[0]
        title_assertion = _title_block(b, asin, "product_edition", t.value_raw, _loc(t))
    # 人物：署名行优先；只有无署名行时才按逗号拆分详情要点（R10）
    for role, fname in (("director", "director"), ("actors", "actor")):
        byline = [(f.ordinal, f.value_raw) for f in fields if f.section == "byline" and f.field_key == role]
        detail = [(f.ordinal, f.value_raw) for f in fields if f.section == "detail_bullets" and f.field_key == role]
        section = "byline" if byline else "detail_bullets"
        for p in nz.parse_people(byline, detail):
            b.add(asin, "product_edition", fname, p.raw, p.strict, "name.strict", qualifier=p.qualifier,
                  extra={"loose": p.loose}, source_locator={"section": section, "ordinal": p.ordinal,
                                                            "field_key": role})
        if any(p.qualifier == "name_ambiguous" for p in nz.parse_people(byline, detail)):
            b.out.issues.append(("name_ambiguous", {"role": fname}))
    # 片长（R11）：详情要点优先，没有时用概览；多个来源且不同时都存，特征取较小值
    runtimes = [(f, nz.runtime_minutes(f.value_raw)) for sec in RUNTIME_SECTIONS for f in fields
                if f.section == sec and f.field_key == "runtime"]
    runtimes = [(f, m) for f, m in runtimes if m is not None]
    conflict = len({m for _, m in runtimes}) > 1
    for f, m in runtimes:
        b.add(asin, "product_edition", "runtime_min", f.value_raw, str(m), "runtime.minutes",
              qualifier="conflict" if conflict else None, source_locator=_loc(f))
    for f in fields:
        if f.field_key == "genre":
            for tag in nz.genre_tags(f.value_raw):
                b.add(asin, "product_edition", "genre", f.value_raw, tag, "genre.tag", source_locator=_loc(f))
        elif f.field_key == "edition" and f.value_raw:
            category, attrs, _year, _cov = nz.classify_segment(f.value_raw)
            for fname, value in attrs:
                if fname != "title_year":
                    b.add(asin, "product_edition", fname, f.value_raw, value, f"descriptor.{category}",
                          qualifier="page_edition", source_locator=_loc(f))
        elif f.field_key in ATTRIBUTE_FIELDS and f.value_raw:
            fname, value = ATTRIBUTE_FIELDS[f.field_key], f.value_raw.lower()
            if f.field_key == "product_release_date":    # 只作版本属性，不是作品年份的证据
                value = nz.release_date_iso(f.value_raw)
            elif f.field_key == "number_of_discs":
                value = f.value_raw.strip() if f.value_raw.strip().isdigit() else None
            b.add(asin, "product_edition", fname, f.value_raw, value, "page.attribute", qualifier=f.section,
                  source_locator=_loc(f))
            if f.field_key == "media_format":
                for system in ("pal", "ntsc"):
                    if system in f.value_raw.lower().replace(",", " ").split():
                        b.add(asin, "product_edition", "edition.video_system", f.value_raw, system, "page.attribute",
                              qualifier=f.section, source_locator=_loc(f))
    # 合集：显式分隔才拆成员，每个成员一个实体，指回合集标题（R17）
    if product_kind == "collection" and title_assertion is not None:
        split = nz.split_collection(titles[0].value_raw)
        if split is None:
            b.out.issues.append(("collection_unsplit", {"reason": "no_delimiter"}))
        elif not split.valid:
            b.out.issues.append(("collection_unsplit", {"reason": "member_count", "mode": split.mode,
                                                        "members": len(split.members)}))
        else:
            for k, member in enumerate(split.members, start=1):
                _title_block(b, f"{asin}#{k}", "collection_member", member, _loc(titles[0]), qualifier=split.mode,
                             extra={"member_ordinal": k, "members": len(split.members)}, parent=title_assertion.key)
    return b.out


# ---------------------------------------------------------------- 商品版本（R02、R03）

def build_editions(conn: psycopg.Connection, ctx: RunContext) -> int:
    """全部清单 ASIN 各一行（product_id 与 edition_id 都等于 ASIN）；重复运行不新增、不修改。返回新增行数。
    匹配范围：movie、collection 且有可解析页面 → matched；有页面的其他类别 → not_applicable；没有可解析页面 → no_page。"""
    i = ctx.inputs
    return conn.execute(
        """INSERT INTO task4.product_edition
               (extraction_id, t3_rule_version, asin, product_kind, basis_level, match_scope, parser_version,
                content_hash, final_url, fetched_at, run_id)
           SELECT c.extraction_id, c.rule_version, c.asin, c.category, c.basis_level,
                  CASE WHEN p.asin IS NULL THEN 'no_page'
                       WHEN c.category IN ('movie', 'collection') THEN 'matched'
                       ELSE 'not_applicable' END,
                  p.parser_version, p.content_hash, p.final_url, p.fetched_at, %(run)s
           FROM task3.product_classification c
           LEFT JOIN task3.page_parse p
                  ON p.extraction_id = c.extraction_id AND p.asin = c.asin AND p.parser_version = c.parser_version
                 AND p.parse_status = 'parsed'
           WHERE c.extraction_id = %(eid)s AND c.rule_version = %(rv)s
           ON CONFLICT DO NOTHING""",
        {"run": ctx.run_id, "eid": i.extraction_id, "rv": i.t3_rule_version}).rowcount


# ---------------------------------------------------------------- 分批标准化

@dataclass
class NormalizeStats:
    entities: int = 0
    assertions: int = 0
    batches: int = 0
    remaining: int = 0
    issues: dict[str, int] = field(default_factory=dict)


def _pending(conn: psycopg.Connection, ctx: RunContext, after: str, n: int) -> list[tuple]:
    i = ctx.inputs
    return conn.execute(
        """SELECT e.asin, e.product_kind, e.content_hash, e.final_url, e.fetched_at, p.snapshot_path
           FROM task4.product_edition e
           JOIN task3.page_parse p ON p.extraction_id = e.extraction_id AND p.asin = e.asin
                                  AND p.parser_version = e.parser_version
           WHERE e.extraction_id = %(eid)s AND e.t3_rule_version = %(t3)s AND e.match_scope = 'matched'
             AND e.asin COLLATE "C" > %(after)s
             AND NOT EXISTS (SELECT 1 FROM task4.entity_stage s
                             WHERE s.extraction_id = e.extraction_id AND s.stage = 'normalize'
                               AND s.stage_key = %(key)s AND s.entity_id = e.asin)
           ORDER BY e.asin COLLATE "C" LIMIT %(n)s""",
        {"eid": i.extraction_id, "t3": i.t3_rule_version, "after": after, "key": i.stage_key(STAGE),
         "n": n}).fetchall()


def count_pending(conn: psycopg.Connection, ctx: RunContext) -> int:
    i = ctx.inputs
    return conn.execute(
        """SELECT count(*) FROM task4.product_edition e
           WHERE e.extraction_id = %(eid)s AND e.t3_rule_version = %(t3)s AND e.match_scope = 'matched'
             AND NOT EXISTS (SELECT 1 FROM task4.entity_stage s
                             WHERE s.extraction_id = e.extraction_id AND s.stage = 'normalize'
                               AND s.stage_key = %(key)s AND s.entity_id = e.asin)""",
        {"eid": i.extraction_id, "t3": i.t3_rule_version, "key": i.stage_key(STAGE)}).fetchone()[0]


def _fields(conn: psycopg.Connection, ctx: RunContext, asins: list[str]) -> dict[str, list[Field]]:
    out: dict[str, list[Field]] = {a: [] for a in asins}
    for asin, section, ordinal, label, value, key in conn.execute(
            "SELECT asin, section, ordinal, label_raw, value_raw, field_key FROM task3.page_field "
            "WHERE extraction_id = %s AND parser_version = %s AND asin = ANY(%s) ORDER BY asin, section, ordinal",
            (ctx.inputs.extraction_id, ctx.inputs.parser_version, asins)).fetchall():
        out[asin].append(Field(section, ordinal, label, value, key))
    return out


def _source_records(conn: psycopg.Connection, ctx: RunContext, pages: list[tuple]) -> dict[str, int]:
    """页面来源记录：一个页面快照登记一次（R06）。"""
    eid = ctx.inputs.extraction_id
    keys = [f"page:{eid}:{p[0]}:{p[2]}" for p in pages]
    locators = [json.dumps({"extraction_id": eid, "asin": p[0], "snapshot_path": p[5], "final_url": p[3]},
                           sort_keys=True) for p in pages]
    rows = conn.execute(
        """INSERT INTO task4.source_record (source_type, locator_key, locator, content_hash, acquired_at)
           SELECT 'page', k, l::jsonb, h, a
           FROM unnest(%s::text[], %s::text[], %s::text[], %s::timestamptz[]) AS t(k, l, h, a)
           ON CONFLICT (source_type, locator_key) DO UPDATE SET locator_key = EXCLUDED.locator_key
           RETURNING locator_key, source_record_id""",
        (keys, locators, [p[2] for p in pages], [p[4] for p in pages])).fetchall()
    by_key = dict(rows)
    return {p[0]: by_key[k] for p, k in zip(pages, keys)}


def _write_batch(conn: psycopg.Connection, ctx: RunContext, batch_no: int, pages: list[tuple],
                 before_commit: BeforeCommit | None, stats: NormalizeStats) -> None:
    fields = _fields(conn, ctx, [p[0] for p in pages])
    norms = [normalize_product(p[0], p[1], fields[p[0]]) for p in pages]
    total = sum(len(n.assertions) for n in norms)
    i = ctx.inputs
    with conn.transaction():
        sources = _source_records(conn, ctx, pages)
        ids = [r[0] for r in conn.execute("SELECT nextval('task4.assertion_id_seq') FROM generate_series(1, %s)",
                                          (max(total, 1),)).fetchall()] if total else []
        rows, cursor = [], 0
        for n in norms:
            local = {a.key: ids[cursor + k] for k, a in enumerate(n.assertions)}
            cursor += len(n.assertions)
            for a in n.assertions:
                rows.append((local[a.key], i.extraction_id, i.parser_version, i.norm_version, a.entity_type,
                             a.entity_id, a.field_name, a.ordinal, a.raw_value, a.normalized_value, a.qualifier,
                             Jsonb(a.extra) if a.extra is not None else None, sources[n.asin],
                             Jsonb(a.source_locator) if a.source_locator is not None else None,
                             local[a.parent] if a.parent is not None else None, a.rule_id))
        copy_rows(conn, "task4.field_assertion", ASSERTION_COLUMNS, rows)
        issue_rows = [(ctx.run_id, i.extraction_id, n.asin, code, Jsonb(detail)) for n in norms
                      for code, detail in n.issues]
        if issue_rows:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO task4.quality_issue (run_id, extraction_id, entity_id, issue_code, "
                                "detail) VALUES (%s, %s, %s, %s, %s)", issue_rows)
        mark_entities(conn, ctx, STAGE, batch_no, [(n.asin, len(n.assertions)) for n in norms])
        mark_batch(conn, ctx, STAGE, batch_no, pages[0][0], pages[-1][0], len(rows))
        if before_commit:
            before_commit(STAGE, batch_no)
    stats.entities += len(pages)
    stats.assertions += len(rows)
    stats.batches += 1
    for n in norms:
        for code, _ in n.issues:
            stats.issues[code] = stats.issues.get(code, 0) + 1


def normalize_extraction(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int,
                         limit: int | None = None, before_commit: BeforeCommit | None = None) -> NormalizeStats:
    """参与匹配的商品按 ASIN 分批标准化；只处理本输入版本下还没有标记的商品（中断后重跑只补剩余）。"""
    if not conn.autocommit:
        raise ValueError("装载连接必须是自动提交模式（使用 etl_task1.db.connect）")
    stats, after = NormalizeStats(), ""
    first_batch = next_batch_no(conn, ctx, STAGE)
    while limit is None or stats.entities < limit:
        n = batch_size if limit is None else min(batch_size, limit - stats.entities)
        pages = _pending(conn, ctx, after, n)
        if not pages:
            break
        _write_batch(conn, ctx, first_batch + stats.batches, pages, before_commit, stats)
        after = pages[-1][0]
    stats.remaining = count_pending(conn, ctx)
    return stats
