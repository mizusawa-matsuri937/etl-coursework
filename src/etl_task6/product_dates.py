"""商品发售日期（spec R18–R19）：从任务 4 的 edition.product_release_date 断言出发，按页面标签分成发售
（media_release）与上架（listing_first_available）两类，按 ASIN 单独保存，不进年份候选。

标签：`release date`、`publication date` → media_release；`date first available` → listing_first_available；
其他标签记质量问题 unknown_date_label，不入表。标记（R19）：1 月 1 日 → jan_first_suspect；
晚于有效评论最大时间 → after_review_period。只标记，不修改、不删除。
"""

from __future__ import annotations

from typing import Any

import psycopg

from .run import DEFAULT_BATCH, BeforeCommit, RunContext, add_quality_issue, copy_rows, mark_batch, mark_entities, \
    next_batch_no
from . import sources

DATE_COLUMNS = ("extraction_id", "parser_version", "assertion_id", "asin", "kind", "label_raw", "raw_value",
                "value_date", "precision", "flags", "section", "page_ordinal", "source_record_id", "run_id")
MAX_DATE_LEN = {"day": 10, "month": 7, "year": 4}


def _normalize(value: str | None) -> tuple[str | None, str | None]:
    """任务 4 的规范值 → (date, precision)；只有年份或年月的按实际精度，不补日。"""
    if not value or not value[0].isdigit():
        return None, None
    v = value.strip()
    if len(v) == 4 and v.isdigit():
        return f"{v}-01-01", "year"
    if len(v) == 7 and v[4] == "-":
        return f"{v}-01", "month"
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        return v, "day"
    return None, None


def extract_product_dates(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                          before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R18–R19：按商品分批写商品发售日期；实体与其他阶段一致。"""
    i = ctx.inputs
    world = sources_world(conn, ctx)
    asins = sorted(world.products)
    review_max = sources.review_time_range(conn, i.extraction_id)[1]
    key, stage = i.stage_key("product_dates"), "product_dates"
    unmarked = conn.execute(
        "SELECT e.asin FROM task4.product_edition e LEFT JOIN task6.entity_stage es ON es.extraction_id = e.extraction_id "
        "AND es.stage = 'product_dates' AND es.stage_key = %s AND es.entity_id = e.asin "
        "WHERE e.extraction_id = %s AND e.t3_rule_version = %s AND e.match_scope = 'matched' "
        "AND es.entity_id IS NULL ORDER BY e.asin COLLATE \"C\"",
        (key, i.extraction_id, world.t3)).fetchall()
    todo = [r[0] for r in unmarked]
    # 断言与页面标签（只读一次）；ordinal 是 section 内序号，键必须带 section 才不与其他区域的行冲突
    by_asin: dict[str, list[dict[str, Any]]] = {a: [] for a in asins}
    for a in sources.assertions_by_entity(conn, i.extraction_id, i.parser_version, "edition.product_release_date",
                                          list(asins)):
        by_asin.setdefault(a["entity_id"], []).append(a)
    labels: dict[tuple[str, str, int], str | None] = {}
    for asin, section, ordinal, label in conn.execute(
            "SELECT asin, section, ordinal, label_raw FROM task3.page_field WHERE extraction_id = %s "
            "AND parser_version = %s AND asin = ANY(%s)", (i.extraction_id, i.parser_version, asins)):
        labels[(asin, section, ordinal)] = label

    counts: dict[str, Any] = {"products": 0, "rows": 0, "by_kind": {}, "unknown_labels": 0}
    batch_no = next_batch_no(conn, ctx, stage)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        with conn.transaction():
            rows: list[tuple] = []
            entities: list[tuple[str, int]] = []
            for asin in batch:
                n = 0
                for a in sorted(by_asin.get(asin, ()), key=lambda x: x["assertion_id"]):
                    locator = a["source_locator"] or {}
                    label = labels.get((asin, locator.get("section"), locator.get("ordinal")))
                    low = (label or "").strip().lower()
                    if low in ("release date", "publication date"):
                        kind = "media_release"
                    elif low == "date first available":
                        kind = "listing_first_available"
                    else:
                        counts["unknown_labels"] += 1
                        add_quality_issue(conn, ctx, asin, "unknown_date_label",
                                          {"asin": asin, "label": label, "raw_value": a["raw_value"]})
                        continue
                    value, precision = _normalize(a["normalized_value"])
                    if value is None:
                        add_quality_issue(conn, ctx, asin, "unparsable_date",
                                          {"asin": asin, "raw_value": a["raw_value"],
                                           "normalized": a["normalized_value"]})
                        continue
                    flags = []
                    if value.endswith("-01-01") and precision != "year":
                        flags.append("jan_first_suspect")
                    if value > review_max:
                        flags.append("after_review_period")
                    rows.append((i.extraction_id, i.parser_version, a["assertion_id"], asin, kind, label,
                                 a["raw_value"], value, precision, flags, locator.get("section"),
                                 locator.get("ordinal"), a["source_record_id"], ctx.run_id))
                    n += 1
                entities.append((asin, n))
            copy_rows(conn, "task6.product_release_date", DATE_COLUMNS, rows)
            mark_entities(conn, ctx, stage, batch_no, entities)
            mark_batch(conn, ctx, stage, batch_no, batch[0], batch[-1], len(rows))
            if before_commit is not None:
                before_commit(stage, batch_no)
        counts["products"] += len(batch)
        batch_no += 1
    for kind, n in conn.execute("SELECT kind, count(*) FROM task6.product_release_date WHERE extraction_id = %s "
                                "AND parser_version = %s GROUP BY 1", (i.extraction_id, i.parser_version)):
        counts["by_kind"][kind] = n
    counts["rows"] = sum(counts["by_kind"].values())
    return counts


def sources_world(conn: psycopg.Connection, ctx: RunContext):
    from .candidates import World
    return World(conn, ctx)
