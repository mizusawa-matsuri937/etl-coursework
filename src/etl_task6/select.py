"""择优与状态（spec R14–R17、R27）：共现消解 → 收集候选 → 固定顺序择优 → 选择记录 → 上映信息版本。

全局步骤，在一个事务里完成（约 8,659 部作品，允许一次提交）。已生成本次运行的版本时直接复用（Airflow
步骤重试不重建版本；输入键相同的第二版本受唯一约束阻止）。missing 的原因优先级：hint_ambiguous >
cooccur_ambiguous > title_years_disagree > no_candidate。商品发售日期、评论时间、评论年份提及与推断上界
没有任何路径写进选择记录（R17）。
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import cooccur, sources
from .candidates import World
from .rules import CandBrief, RuleSet, choose_candidates, evaluate_disagreement
from .run import DEFAULT_BATCH, RunContext, add_quality_issue, copy_rows, next_release_version, set_release_version

SEL_COLUMNS = ("release_version_id", "movie_id", "status", "missing_reason", "release_year", "release_date",
               "precision", "region", "release_type", "regional_release_missing_reason", "selected_candidate_id",
               "considered_candidate_ids", "selection_rule", "reason", "confidence", "disagreement")
IMDB_SOURCES = {"imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur"}


def _pending_ok(conn: psycopg.Connection, ctx: RunContext, world: World) -> None:
    """各分批阶段的实体是否全部处理完（limit 试跑或中断后续跑时，择优不冒充全量）。"""
    i = ctx.inputs
    t4 = i.t4_mapping_version_id
    checks = {
        "candidates": conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s",
                                   (t4,)).fetchone()[0],
        "cooccur": conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND tconst IS NULL",
                                (t4,)).fetchone()[0],
        "review_years": conn.execute(
            "SELECT count(*) FROM task4.movie m WHERE m.mapping_version_id = %s AND EXISTS (SELECT 1 FROM "
            "task4.product_movie pm WHERE pm.mapping_version_id = m.mapping_version_id AND pm.movie_id = m.movie_id "
            "AND pm.relation_type = 'edition')", (t4,)).fetchone()[0],
        "product_dates": conn.execute(
            "SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s "
            "AND match_scope = 'matched'", (i.extraction_id, world.t3)).fetchone()[0],
        "product_reviews": conn.execute(
            "SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s "
            "AND match_scope = 'matched'", (i.extraction_id, world.t3)).fetchone()[0],
    }
    for stage, expected in checks.items():
        got = conn.execute("SELECT count(*) FROM task6.entity_stage WHERE extraction_id = %s AND stage = %s "
                           "AND stage_key = %s", (i.extraction_id, stage, i.stage_key(stage))).fetchone()[0]
        if got != expected:
            raise RuntimeError(f"阶段 {stage} 已标记 {got} 个实体，应为 {expected}（试跑 limit 或中断后未跑完），"
                               "不生成上映信息版本")


def build_selection(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *,
                    before_commit=None) -> str:
    """生成或复用本次运行的上映信息版本，返回 release_version_id。"""
    i = ctx.inputs
    have = conn.execute("SELECT release_version_id FROM task6.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0]
    if have:
        return have
    with conn.transaction():
        have = conn.execute("SELECT release_version_id FROM task6.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0]
        if have:
            return have
        world = World(conn, ctx)
        _pending_ok(conn, ctx, world)
        cooccur_counts = cooccur.resolve(conn, ctx, rules)
        cands: dict[str, list[CandBrief]] = {}
        for cid, movie, source, skey, year, flags in conn.execute(
                "SELECT candidate_id, movie_id, source, source_key, value_year, flags FROM task6.release_candidate "
                "WHERE extraction_id = %s AND stage_key IN (%s, %s) AND value_year IS NOT NULL "
                "ORDER BY candidate_id", (i.extraction_id, i.stage_key("candidates"), i.stage_key("cooccur"))):
            cands.setdefault(movie, []).append(CandBrief(cid, source, skey, year, tuple(flags or []),
                                                         skey if source in IMDB_SOURCES else None))
        multiple_hint = {r[0] for r in conn.execute(
            "SELECT DISTINCT movie_id FROM task6.hint_use WHERE extraction_id = %s AND stage_key = %s "
            "AND action = 'multiple_tconst'", (i.extraction_id, i.stage_key("candidates")))}
        ambiguous_cooccur = {r[0] for r in conn.execute(
            "SELECT movie_id FROM task6.cooccur_result WHERE extraction_id = %s AND stage_key = %s "
            "AND outcome = 'ambiguous'", (i.extraction_id, i.stage_key("cooccur")))}

        rv = next_release_version(conn, i.extraction_id)
        parent = conn.execute("SELECT release_version_id FROM task6.release_version WHERE extraction_id = %s "
                              "ORDER BY created_at DESC, release_version_id DESC LIMIT 1",
                              (i.extraction_id,)).fetchone()
        statuses: dict[str, int] = {}
        reasons: dict[str, int] = {}
        diff1 = year_bad = tconst_bad = 0
        sel_rows: list[tuple] = []
        for movie, _tconst in world.movies:
            briefs = cands.get(movie, [])
            choice = choose_candidates(briefs, multiple_tconst_hint=movie in multiple_hint,
                                       cooccur_ambiguous=movie in ambiguous_cooccur)
            dis = evaluate_disagreement(choice, rules)
            conf = rules.confidence_of(choice.adopted.source, choice.adopted.flags) if choice.adopted else 0.0
            year = None if choice.adopted is None else choice.adopted.value_year
            sel_rows.append((rv, movie, choice.status, choice.missing_reason, year, None, "year", None,
                             "first_release", "no_permitted_dated_source",
                             choice.adopted.candidate_id if choice.adopted else None,
                             [c.candidate_id for c in choice.considered],
                             f"{rules.version}:order:imdb_t4_link>imdb_t5_hint>imdb_t6_cooccur>amazon_title_year",
                             choice.reason, conf,
                             Jsonb({"year_diff_1": list(dis.year_diff_1), "year_disagreement": list(dis.year_disagreements),
                                    "tconst_disagreement": list(dis.tconst_disagreement)})))
            statuses[choice.status] = statuses.get(choice.status, 0) + 1
            if choice.missing_reason:
                reasons[choice.missing_reason] = reasons.get(choice.missing_reason, 0) + 1
            diff1 += len(dis.year_diff_1)
            year_bad += len(dis.year_disagreements)
            tconst_bad += len(dis.tconst_disagreement)
            for _ in dis.year_disagreements:
                add_quality_issue(conn, ctx, movie, "year_disagreement", {"movie_id": movie})
            for t in dis.tconst_disagreement:
                add_quality_issue(conn, ctx, movie, "tconst_disagreement", {"movie_id": movie, "tconst": t})
        counts = {"movies": len(world.movies), "status": statuses, "missing_reasons": reasons,
                  "candidates": {s: sum(1 for m in cands.values() for c in m if c.source == s)
                                 for s in ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur", "amazon_title_year")},
                  "cooccur": cooccur_counts, "year_diff_1": diff1, "year_disagreement": year_bad,
                  "tconst_disagreement": tconst_bad}
        conn.execute("INSERT INTO task6.release_version (release_version_id, extraction_id, parent_version_id, "
                     "run_id, input_key, t4_mapping_version_id, t5_person_mapping_version_id, parser_version, "
                     "rule_version, imdb_snapshot_id, counts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (rv, i.extraction_id, parent[0] if parent else None, ctx.run_id, i.key(),
                      i.t4_mapping_version_id, i.t5_person_mapping_version_id, i.parser_version, i.rule_version,
                      i.imdb_snapshot_id, Jsonb(counts)))
        copy_rows(conn, "task6.release_selection", SEL_COLUMNS, sel_rows)
        set_release_version(conn, ctx.run_id, rv)
    return rv
