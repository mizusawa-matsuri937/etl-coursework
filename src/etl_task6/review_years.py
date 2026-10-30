"""评论正文年份提及（spec R11，模式版本 t6m1）：只作证据记录与共现反查的消歧，不单独采用为年份。

模式（不区分大小写，年份 1900–2012；晚于该评论年份的提及不计）：
- paren：`(YYYY)`
- year_word：`YYYY('s)? film / movie / version / original / classic / feature / picture / 常见类型词`
- made_in：`made|filmed|shot|premiered|produced in YYYY`
- released_in：`released (theatrically) in YYYY`
计票：每条评论每个年份一票（votes 为不同评论数），另记各模式的票数。评论正文不导出，只存源评论 ID。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .rules import RuleSet
from .run import DEFAULT_BATCH, BeforeCommit, RunContext, copy_rows, mark_batch, mark_entities, next_batch_no
from . import sources

MENTION_COLUMNS = ("extraction_id", "stage_key", "movie_id", "year", "votes", "source_review_ids", "patterns",
                   "run_id")
_YEAR = r"(19\d\d|20[01]\d)"
MAX_YEAR = 2012
PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("paren", re.compile(rf"\({_YEAR}\)")),
    ("year_word", re.compile(rf"\b{_YEAR}(?:'s)?\s+(?:film|movie|version|original|classic|feature|picture|release|"
                             r"drama|comedy|horror|western|documentary|animation|anime|short)\b", re.I)),
    ("made_in", re.compile(rf"\b(?:made|filmed|shot|premiered|produced)\s+in\s+{_YEAR}\b", re.I)),
    ("released_in", re.compile(rf"\breleased\s+(?:theatrically\s+)?in\s+{_YEAR}\b", re.I)),
)


def extract_years(summary: str | None, text: str | None) -> set[tuple[int, str]]:
    """一条评论里命中的 (年份, 模式) 集合。"""
    found: set[tuple[int, str]] = set()
    for name, pattern in PATTERNS:
        for m in pattern.finditer(f"{summary or ''} {text or ''}"):
            year = int(m.group(1))
            if year <= MAX_YEAR:
                found.add((year, name))
    return found


def extract_mentions(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *,
                     batch_size: int = DEFAULT_BATCH, before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """按作品分批抽取年份提及并计票（目标 = 有 edition 商品的全部作品，等到择优时按规则版本算计票门槛）。"""
    i = ctx.inputs
    world = sources_world(conn, ctx)
    targets = [m for m, _ in world.movies if world.edition_asins.get(m)]
    key, stage = i.stage_key("review_years"), "review_years"
    unmarked = conn.execute(
        "SELECT m.movie_id FROM task4.movie m LEFT JOIN task6.entity_stage es ON es.extraction_id = %s "
        "AND es.stage = 'review_years' AND es.stage_key = %s AND es.entity_id = m.movie_id "
        "WHERE m.mapping_version_id = %s AND es.entity_id IS NULL AND EXISTS (SELECT 1 FROM task4.product_movie pm "
        "WHERE pm.mapping_version_id = m.mapping_version_id AND pm.movie_id = m.movie_id "
        "AND pm.relation_type = 'edition') ORDER BY m.movie_id COLLATE \"C\"",
        (i.extraction_id, key, i.t4_mapping_version_id)).fetchall()
    todo = [r[0] for r in unmarked]
    counts: dict[str, Any] = {"movies": 0, "with_mentions": 0, "mention_rows": 0}
    batch_no = next_batch_no(conn, ctx, stage)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        with conn.transaction():
            asins = sorted({a for m in batch for a in world.edition_asins.get(m, ())})
            # 评论归属：asin → 本批作品
            asin_movie = {a: m for m in batch for a in world.edition_asins.get(m, ())}
            votes: dict[tuple[str, int], dict[str, Any]] = defaultdict(
                lambda: {"reviews": set(), "patterns": defaultdict(int)})
            for k in range(0, len(asins), 500):
                for asin, rid, when, summary, text in conn.execute(
                        "SELECT product_id, source_review_id, review_time_utc, summary_raw, review_text_raw "
                        "FROM review_staging WHERE extraction_id = %s AND product_id = ANY(%s)",
                        (i.extraction_id, asins[k:k + 500])):
                    movie = asin_movie.get(asin)
                    if movie is None or when is None:
                        continue
                    for year, pattern in extract_years(summary, text):
                        if year > when.year:                     # 晚于该评论年份的提及不计
                            continue
                        v = votes[(movie, year)]
                        if rid not in v["reviews"]:
                            v["reviews"].add(rid)
                            v["patterns"][pattern] += 1
            rows: list[tuple] = []
            entities: list[tuple[str, int]] = []
            for movie in batch:
                n = 0
                for (m, year) in sorted(k for k in votes if k[0] == movie):
                    v = votes[(m, year)]
                    rows.append((i.extraction_id, key, m, year, len(v["reviews"]),
                                 sorted(v["reviews"]), Jsonb(dict(v["patterns"])), ctx.run_id))
                    n += 1
                    counts["mention_rows"] += 1
                if n:
                    counts["with_mentions"] += 1
                entities.append((movie, n))
            copy_rows(conn, "task6.review_year_mention", MENTION_COLUMNS, rows)
            mark_entities(conn, ctx, stage, batch_no, entities)
            mark_batch(conn, ctx, stage, batch_no, batch[0], batch[-1], len(rows))
            if before_commit is not None:
                before_commit(stage, batch_no)
        counts["movies"] += len(batch)
        batch_no += 1
    return counts


def sources_world(conn: psycopg.Connection, ctx: RunContext):
    """candidates.World 的轻量包装（各阶段共用的只读输入）。"""
    from .candidates import World
    return World(conn, ctx)
