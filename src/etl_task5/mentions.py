"""人物提及：从 task3.page_field 的署名行与详情要点取导演、演员，每个名字一行（spec R01–R07，AC01）。

范围：任务 4 指定映射版本里 match_scope = matched 的商品（电影类与合集）。导演取署名行，没有署名行导演时才取详情要点；
演员取署名行，且详情要点一律读（和任务 4 不同）。编剧、制片等不建提及。每批（按 ASIN 排序）的提及、质量问题、
实体标记与批次审计在同一事务提交，中断后只处理未标记的商品。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import names
from .run import BeforeCommit, RunContext, copy_rows, mark_batch, mark_entities, next_batch_no

STAGE = "mentions"
ROLE_OF_KEY = {"director": "director", "actors": "actor"}
COLUMNS = ("extraction_id", "parser_version", "norm_version", "mention_key", "asin", "product_kind", "section",
           "page_ordinal", "list_position", "label_raw", "role_class", "is_byline", "byline_rank", "raw_value",
           "display_name", "nickname", "disambiguator", "paren_text", "suffix", "honorific", "strict_key",
           "loose_key", "tokenset_key", "family_key", "initial_key", "flags", "status", "t4_assertion_id",
           "source_record_id", "run_id")


@dataclass
class Mention:
    asin: str
    section: str
    page_ordinal: int
    list_position: int
    label_raw: str | None
    role_class: str
    byline_rank: int | None
    raw: str
    name: names.Name
    status: str

    @property
    def key(self) -> str:
        return f"{self.asin}:{self.section}:{self.page_ordinal}:{self.list_position}"


@dataclass
class MentionStats:
    entities: int = 0
    mentions: int = 0
    batches: int = 0
    remaining: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


def extract_product(asin: str, fields: list[tuple[str, int, str | None, str | None, str | None]]) -> tuple[
        list[Mention], list[tuple[str, dict[str, Any]]]]:
    """纯函数：一个商品的页面字段 (区域, 序号, 标签, 值, 字段键) → 提及与质量问题。"""
    out: list[Mention] = []
    issues: list[tuple[str, dict[str, Any]]] = []
    has_byline_director = any(f[0] == "byline" and f[4] == "director" and (f[3] or "").strip() for f in fields)
    ranks: dict[str | None, int] = {}
    for section, ordinal, label, value, key in sorted(fields, key=lambda f: (f[0], f[1])):
        role = ROLE_OF_KEY.get(key or "")
        if role is None or section not in ("byline", "detail_bullets") or not (value or "").strip():
            continue
        if section == "byline":
            ranks[label] = ranks.get(label, 0) + 1
            n = names.parse(value)
            out.append(Mention(asin, section, ordinal, 1, label, role, ranks[label], value, n,
                               "placeholder" if n.placeholder else "resolvable"))
        elif role == "actor" or not has_byline_director:
            tokens, ambiguous = names.split_list(value)
            if ambiguous:
                issues.append(("ambiguous_names", {"section": section, "ordinal": ordinal, "role": role,
                                                   "value": value}))
            for pos, token in enumerate(tokens, start=1):
                n = names.parse(token)
                status = "ambiguous" if ambiguous else "placeholder" if n.placeholder else "resolvable"
                out.append(Mention(asin, section, ordinal, pos, label, role, None, token, n, status))
    return out, issues


def _scope(conn: psycopg.Connection, ctx: RunContext) -> tuple[str, str, str, str]:
    """映射版本的 (抽取, 任务 3 规则版本, 解析版本, 任务 4 规范化版本)。"""
    return conn.execute("SELECT extraction_id, t3_rule_version, parser_version, norm_version "
                        "FROM task4.mapping_version WHERE mapping_version_id = %s",
                        (ctx.inputs.t4_mapping_version_id,)).fetchone()


_PENDING_FROM = """FROM task4.product_edition e
           WHERE e.extraction_id = %(eid)s AND e.t3_rule_version = %(t3)s AND e.match_scope = 'matched'
             AND NOT EXISTS (SELECT 1 FROM task5.entity_stage s
                             WHERE s.extraction_id = e.extraction_id AND s.stage = 'mentions'
                               AND s.stage_key = %(key)s AND s.entity_id = e.asin)"""


def count_pending(conn: psycopg.Connection, ctx: RunContext) -> int:
    eid, t3, _, _ = _scope(conn, ctx)
    return conn.execute(f"SELECT count(*) {_PENDING_FROM}",
                        {"eid": eid, "t3": t3, "key": ctx.inputs.stage_key(STAGE)}).fetchone()[0]


def _pending(conn: psycopg.Connection, ctx: RunContext, after: str, n: int) -> list[tuple[str, str]]:
    eid, t3, _, _ = _scope(conn, ctx)
    return conn.execute(f"SELECT e.asin, e.product_kind {_PENDING_FROM} AND e.asin COLLATE \"C\" > %(after)s "
                        "ORDER BY e.asin COLLATE \"C\" LIMIT %(n)s",
                        {"eid": eid, "t3": t3, "key": ctx.inputs.stage_key(STAGE), "after": after, "n": n}).fetchall()


def _t4_links(conn: psycopg.Connection, ctx: RunContext, asins: list[str]) -> tuple[dict, dict]:
    """任务 4 的导演、演员断言 (ASIN, 字段, 区域, 页面序号) → [(断言 ID, 原值)]，以及 ASIN → 页面快照来源记录。"""
    eid, _, parser, t4_norm = _scope(conn, ctx)
    assertions: dict[tuple, list[tuple[int, str]]] = {}
    for aid, asin, fname, loc, raw in conn.execute(
            "SELECT assertion_id, entity_id, field_name, source_locator, raw_value FROM task4.field_assertion "
            "WHERE extraction_id = %s AND parser_version = %s AND norm_version = %s AND entity_id = ANY(%s) "
            "AND field_name IN ('director', 'actor') ORDER BY entity_id, field_name, ordinal",
            (eid, parser, t4_norm, asins)).fetchall():
        assertions.setdefault((asin, fname, loc["section"], loc["ordinal"]), []).append((aid, raw))
    sources = dict(conn.execute(
        "SELECT entity_id, source_record_id FROM task4.field_assertion WHERE extraction_id = %s AND parser_version = %s "
        "AND norm_version = %s AND entity_id = ANY(%s) AND field_name = 'title' AND ordinal = 1 "
        "AND entity_type = 'product_edition'", (eid, parser, t4_norm, asins)).fetchall())
    return assertions, sources


def _assertion_for(m: Mention, assertions: dict, used: set[int]) -> int | None:
    """署名行一行一条断言；详情要点按原值对应（断言按拆分顺序排列），对不上的留空。"""
    rows = assertions.get((m.asin, m.role_class, m.section, m.page_ordinal), [])
    for aid, raw in rows:
        if aid not in used and (m.section == "byline" or raw == m.raw):
            used.add(aid)
            return aid
    return None


def _write_batch(conn: psycopg.Connection, ctx: RunContext, batch_no: int, products: list[tuple[str, str]],
                 before_commit: BeforeCommit | None, stats: MentionStats) -> None:
    i = ctx.inputs
    eid, t3, parser, _ = _scope(conn, ctx)
    asins = [p[0] for p in products]
    by_asin: dict[str, list] = {a: [] for a in asins}
    for asin, section, ordinal, label, value, key in conn.execute(
            "SELECT asin, section, ordinal, label_raw, value_raw, field_key FROM task3.page_field "
            "WHERE extraction_id = %s AND parser_version = %s AND asin = ANY(%s) ORDER BY asin, section, ordinal",
            (eid, parser, asins)).fetchall():
        by_asin[asin].append((section, ordinal, label, value, key))
    assertions, sources = _t4_links(conn, ctx, asins)
    rows, issues, counts = [], [], []
    for asin, kind in products:
        found, problems = extract_product(asin, by_asin[asin])
        used: set[int] = set()
        for m in found:
            n = m.name
            rows.append((eid, i.parser_version, i.norm_version, m.key, asin, kind, m.section, m.page_ordinal,
                         m.list_position, m.label_raw, m.role_class, m.section == "byline", m.byline_rank, m.raw,
                         n.display, n.nickname, n.disambiguator, n.paren_text, n.suffix, n.honorific, n.strict,
                         n.loose, n.tokenset, n.family, n.initial,
                         Jsonb({k: True for k, v in (("reversed", n.reversed), ("encoding_damaged", n.encoding_damaged))
                                if v}), m.status, _assertion_for(m, assertions, used), sources.get(asin), ctx.run_id))
            stats.by_status[m.status] = stats.by_status.get(m.status, 0) + 1
        issues += [(ctx.run_id, eid, asin, code, Jsonb(detail)) for code, detail in problems]
        counts.append((asin, len(found)))
    with conn.transaction():
        copy_rows(conn, "task5.person_mention", COLUMNS, rows)
        if issues:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO task5.quality_issue (run_id, extraction_id, entity_id, issue_code, "
                                "detail) VALUES (%s, %s, %s, %s, %s)", issues)
        mark_entities(conn, ctx, STAGE, batch_no, counts)
        mark_batch(conn, ctx, STAGE, batch_no, asins[0], asins[-1], len(rows))
        if before_commit:
            before_commit(STAGE, batch_no)
    stats.entities += len(products)
    stats.mentions += len(rows)
    stats.batches += 1


def extract_mentions(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int, limit: int | None = None,
                     before_commit: BeforeCommit | None = None) -> MentionStats:
    """参与匹配的商品按 ASIN 分批抽取提及；只处理本输入版本下还没有标记的商品（中断后重跑只补剩余）。"""
    if not conn.autocommit:
        raise ValueError("装载连接必须是自动提交模式（使用 etl_task1.db.connect）")
    stats, after = MentionStats(), ""
    first_batch = next_batch_no(conn, ctx, STAGE)
    while limit is None or stats.entities < limit:
        n = batch_size if limit is None else min(batch_size, limit - stats.entities)
        products = _pending(conn, ctx, after, n)
        if not products:
            break
        _write_batch(conn, ctx, first_batch + stats.batches, products, before_commit, stats)
        after = products[-1][0]
    stats.remaining = count_pending(conn, ctx)
    return stats
