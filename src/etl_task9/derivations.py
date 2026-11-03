"""集成层 build_derivations（spec R14、R16–R20、R22；重放补定位 R21 在 relocate.py）：把 assertions 阶段的断言连成
派生图。每条边是“子断言 ← 父断言或父源记录”，带操作类型（R17 闭集）、OpenLineage 标注（R18）、上游规则与版本、
上游任务、运行与写入时间、置信度与角色；同一子、父与操作只有一条边（主键），重跑不重复（ON CONFLICT DO NOTHING）。

边的家族（单元名 → 子断言 → 父节点，操作）：

| 单元 | 子 | 父 | 操作 |
|---|---|---|---|
| e_extract | 全部抽取级断言 | 源记录 | extract |
| e_t4_split | 任务 4 有父断言 | 任务 4 父断言 | split |
| e_t6 | 任务 6 年份候选 | IMDb start_year 断言或任务 4 标题年份断言 | normalize |
| e_t7 | 任务 7 候选 | 任务 4 断言、任务 3 字段、IMDb 列、任务 6 候选；版本候选 | normalize；select |
| e_merge | 任务 4 合并决策 | 特征所用的页面断言与 IMDb 作品、演职员行 | link |
| e_product_movie | 商品 → 作品 | 合并决策；singleton 为页面源记录；人工决策为 manual 源记录 | link |
| e_link | 任务 5 外部 ID 判定 | 提及、IMDb 演职员行与人名行 | link |
| e_mention_person | 提及 → 人物 | 提及（依据、轮次与决策引用写在 detail）；采用的外部 ID 判定 | merge；link |
| e_person | 人物规范名、外部 ID；任务 7 IMDb 独有人物 | IMDb 人名行或提及；外部 ID 判定 | select；link |
| e_credit | 署名对齐；演职员成员 | 提及 → 人物、IMDb 演职员行；对齐；IMDb 独有人物 | align；union；link |
| e_genre | 类型成员 | IMDb 类型断言；映射到该受控词的页面词面 | union；vocab_map |
| e_bound_media | 发售日期证据的推断上界 | 所用 media_release 断言 | infer |
| relocate.* | 类型词面、版本特征、评论证据的推断上界 | 重放补出的页面断言；全部依赖评论 | relocate；aggregate |

边按子断言 ID 的前 3 位十六进制（4,096 个桶）分批：桶是 entity_stage 的实体，和数据同事务提交（R38）。
父节点可能不存在的边（候选引用的 IMDb 行未登记等）只在父断言存在时写入；之后 check_graph 检查悬空边、
没有父节点的断言与环（R19），并写质量问题。字段选择（T9-03）的边写在 selections 阶段键下，同一函数可一并检查。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Sequence

import psycopg

from .assertions import base_params, upstream_params
from .rules import OPERATIONS, sql_as_id
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, Unit, add_issues, process_units, register_stage_key,
                  require_stage_complete)

STAGE = "derivations"
BUCKETS = [f"{n:03x}" for n in range(4096)]
EDGE_COLUMNS = ("stage_key, child_kind, child_id, parent_kind, parent_id, operation, ol_type, ol_subtype, rule_id, "
                "rule_version, upstream_task, upstream_run_id, occurred_at, confidence, role, relocated, detail, run_id")
_OL_TYPE = "CASE e.operation " + " ".join(f"WHEN '{op}' THEN '{t}'" for op, (t, _s) in OPERATIONS.items()) + " END"
_OL_SUB = "CASE e.operation " + " ".join(f"WHEN '{op}' THEN '{s}'" for op, (_t, s) in OPERATIONS.items()) + " END"

# 上游运行的写入时间：各任务运行表的开始时间、任务 1 装载运行的结束时间、IMDb 快照的装载时间（R18）
RUNS = """(SELECT run_id, started_at AS at FROM task3.classify_run
           UNION ALL SELECT run_id, started_at FROM task4.run
           UNION ALL SELECT run_id, started_at FROM task5.run
           UNION ALL SELECT run_id, started_at FROM task6.run
           UNION ALL SELECT run_id, started_at FROM task7.run
           UNION ALL SELECT run_id, coalesce(finished_at, started_at) FROM public.etl_run
           UNION ALL SELECT snapshot_id, coalesce(loaded_at, downloaded_at) FROM imdb.snapshot)"""
FAMILY_COLUMNS = ("child_kind, child_id, parent_kind, parent_id, operation, rule_id, rule_version, upstream_task, "
                  "upstream_run_id, occurred_at, confidence, role, relocated, detail")


def edge_insert(family_sql: str) -> str:
    """把一个家族的 SELECT（列为 FAMILY_COLUMNS）包成写派生边的 INSERT：补阶段键、OpenLineage 标注与写入时间。
    操作不在闭集内时标注为 NULL，违反 NOT NULL 而整批失败（R17）。"""
    return f"""
INSERT INTO task9.derivation ({EDGE_COLUMNS})
SELECT DISTINCT ON (e.child_kind, e.child_id, e.parent_kind, e.parent_id, e.operation)
       %(dk)s, e.child_kind, e.child_id, e.parent_kind, e.parent_id, e.operation, {_OL_TYPE}, {_OL_SUB}, e.rule_id,
       e.rule_version, e.upstream_task, e.upstream_run_id, coalesce(e.occurred_at, r.at), e.confidence, e.role,
       e.relocated, e.detail, %(run)s
FROM ({family_sql}) AS e({FAMILY_COLUMNS})
LEFT JOIN {RUNS} r ON r.run_id = e.upstream_run_id
ORDER BY e.child_kind, e.child_id, e.parent_kind, e.parent_id, e.operation, e.role NULLS LAST
ON CONFLICT DO NOTHING"""


def _as(table: str, key: str) -> str:
    return sql_as_id(f"'{table}'", key)


_BUCKET = "substr(a.assertion_id, 4, 3) = ANY(%(batch)s)"
_CHILD = "FROM task9.assertion a WHERE a.stage_key = %(ak)s AND a.upstream_table = '{table}' AND " + _BUCKET
_EXISTS = "JOIN task9.assertion p ON p.stage_key = %(ak)s AND p.assertion_id = v.pid"
_COMMON = "a.rule_id, a.extract_rule_version"
_FL = "a.field_locator"

# ---------------------------------------------------------------- 抽取与任务 4 拆分

E_EXTRACT = f"""
SELECT 'assertion', a.assertion_id, 'source_record', a.source_record_id, 'extract', 'extract:' || a.upstream_table,
       coalesce(a.extract_rule_version, a.upstream_table),
       CASE split_part(a.upstream_table, '.', 1) WHEN 'public' THEN 'task1' ELSE split_part(a.upstream_table, '.', 1) END,
       coalesce(a.upstream_run_id, CASE split_part(a.upstream_table, '.', 1) WHEN 'imdb' THEN %(snap)s
                                        WHEN 'public' THEN %(t1run)s WHEN 'task4' THEN %(t4run)s END),
       NULL::timestamptz, NULL::numeric, NULL::text, false, '{{}}'::jsonb
FROM task9.assertion a WHERE a.stage_key = %(ak)s AND a.level = 'extract' AND {_BUCKET}
"""
E_T4_SPLIT = f"""
SELECT 'assertion', a.assertion_id, 'assertion', {_as('task4.field_assertion', f"({_FL}->>'parent_assertion_id')")},
       'split', {_COMMON}, 'task4', %(t4run)s, NULL::timestamptz, NULL::numeric, NULL::text, false,
       jsonb_build_object('task4_parent_assertion_id', {_FL}->'parent_assertion_id')
FROM task9.assertion a WHERE a.stage_key = %(ak)s AND a.level = 'derived' AND a.upstream_table = 'task4.field_assertion'
  AND {_BUCKET}
"""

# ---------------------------------------------------------------- 任务 6、7 候选

E_T6 = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, 'normalize', {_COMMON}, 'task6', a.upstream_run_id,
       NULL::timestamptz, a.confidence, NULL::text, false, jsonb_build_object('via', v.via)
FROM task9.assertion a
CROSS JOIN LATERAL (VALUES
    (CASE WHEN a.rule_id <> 'amazon_title_year'
          THEN {_as('imdb.title', f"coalesce({_FL}->>'snapshot', %(snap)s) || '|' || ({_FL}->>'source_key') || '|start_year'")} END,
     'imdb.title.start_year'),
    (CASE WHEN {_FL} ? 'assertion_id' THEN {_as('task4.field_assertion', f"({_FL}->>'assertion_id')")} END,
     'task4.field_assertion')) AS v(pid, via)
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task6.release_candidate' AND {_BUCKET}
"""
_T3_KEY = ("%(eid)s || '|' || ({o}->>'asin') || '|' || %(parser)s || '|' || ({o}->>'section') || '|' || "
           "({o}->>'ordinal')")
E_T7 = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, v.op, {_COMMON}, 'task7', a.upstream_run_id,
       NULL::timestamptz, a.confidence, NULL::text, false, jsonb_build_object('via', v.via)
FROM task9.assertion a
CROSS JOIN LATERAL (
    SELECT x.pid, x.op, x.via FROM (VALUES
        ({_as('task4.field_assertion', f"coalesce({_FL}->>'assertion_id', {_FL}->'task4_source'->>'assertion_id')")},
         'normalize', 'task4.field_assertion'),
        ({_as('task6.release_candidate', f"({_FL}->>'task6_candidate_id')")}, 'normalize', 'task6.release_candidate'),
        (CASE WHEN {_FL}->>'table' = 'imdb.title'
              THEN {_as('imdb.title', f"({_FL}->>'snapshot') || '|' || ({_FL}->>'tconst') || '|' || ({_FL}->>'column')")}
              WHEN {_FL}->>'file' = 'title.basics.tsv.gz'
              THEN {_as('imdb.title', f"({_FL}->>'snapshot') || '|' || ({_FL}->>'tconst') || '|start_year'")} END,
         'normalize', 'imdb.title'),
        (CASE WHEN {_FL}->>'task3_table' = 'page_field' THEN {_as('task3.page_field', _T3_KEY.format(o=_FL))}
              WHEN {_FL}->'task3_source'->>'task3_table' = 'page_field'
              THEN {_as('task3.page_field', _T3_KEY.format(o=f"({_FL}->'task3_source')"))} END,
         'normalize', 'task3.page_field')) AS x(pid, op, via)
    UNION ALL
    SELECT {_as('task7.field_candidate', 'y')}, 'select', 'task7.field_candidate'
    FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof({_FL}->'edition_candidate_ids') = 'array'
                                        THEN {_FL}->'edition_candidate_ids' ELSE '[]'::jsonb END) AS y
) v
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task7.field_candidate' AND {_BUCKET}
"""

# ---------------------------------------------------------------- 实体合并：商品 → 作品（R20）

_ARR = "CASE WHEN jsonb_typeof({x}) = 'array' THEN {x} ELSE '[]'::jsonb END"
E_MERGE = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, 'link', {_COMMON}, 'task4', a.upstream_run_id,
       ({_FL}->>'decided_at')::timestamptz, a.confidence, NULL::text, false, jsonb_build_object('feature', v.feature)
FROM task9.assertion a
CROSS JOIN LATERAL (
    SELECT {_as('task4.field_assertion', 'y')} AS pid, k AS feature
    FROM jsonb_each(CASE WHEN jsonb_typeof({_FL}->'features'->'evidence'->'page') = 'object'
                         THEN {_FL}->'features'->'evidence'->'page' ELSE '{{}}'::jsonb END) AS kv(k, v)
    CROSS JOIN LATERAL jsonb_array_elements_text({_ARR.format(x='kv.v')}) AS y
    UNION ALL
    SELECT {_as('imdb.title', f"({_FL}->>'snapshot') || '|' || a.member_key || '|primary_title'")}, 'title'
    UNION ALL
    SELECT {_as('imdb.title_principal', "tp.snapshot_id || '|' || tp.tconst || '|' || tp.ordering")}, 'actor'
    FROM jsonb_array_elements({_ARR.format(x=f"{_FL}->'features'->'evidence'->'imdb'->'actors'")}) AS x
    JOIN imdb.title_principal tp ON tp.snapshot_id = {_FL}->>'snapshot' AND tp.tconst = a.member_key
                                AND tp.line_no = (x->>'line_no')::bigint
    UNION ALL
    SELECT {_as('imdb.title_director', "td.snapshot_id || '|' || td.tconst || '|' || td.ordinal")}, 'director'
    FROM jsonb_array_elements({_ARR.format(x=f"{_FL}->'features'->'evidence'->'imdb'->'directors'")}) AS x
    JOIN imdb.title_director td ON td.snapshot_id = {_FL}->>'snapshot' AND td.tconst = a.member_key
                               AND td.nconst = x->>'nconst'
) v
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task4.merge_decision' AND {_BUCKET}
"""
E_PRODUCT_MOVIE = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, 'link', 'auto_decision', a.extract_rule_version,
       'task4', a.upstream_run_id, NULL::timestamptz, a.confidence, NULL::text, false,
       jsonb_build_object('basis', 'auto_decision', 'decision_id', {_FL}->'decision_id')
FROM task9.assertion a
CROSS JOIN LATERAL (VALUES ({_as('task4.merge_decision', f"({_FL}->>'decision_id')")})) AS v(pid)
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task4.product_movie' AND a.rule_id = 'auto_decision' AND {_BUCKET}
UNION ALL
SELECT 'assertion', a.assertion_id, 'source_record', s.source_record_id, 'link', a.rule_id, a.extract_rule_version,
       'task4', a.upstream_run_id, NULL::timestamptz, NULL::numeric, NULL::text, false,
       jsonb_build_object('basis', a.rule_id, 'human_decision_id', {_FL}->'human_decision_id')
FROM task9.assertion a
JOIN task9.source_record s ON s.stage_key = %(sk)s
     AND ((a.rule_id = 'singleton' AND s.upstream_table = 'task2.product_manifest' AND s.upstream_key = %(eid)s || '|' || a.entity_id)
       OR (a.rule_id = 'human' AND s.upstream_table = 'task4.human_decision' AND s.upstream_key = {_FL}->>'human_decision_id'))
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task4.product_movie' AND a.rule_id IN ('singleton', 'human')
  AND {_BUCKET}
"""

# ---------------------------------------------------------------- 实体合并：提及 → 人物、外部 ID（R20）

_MENTION = _as('task5.person_mention', "%(eid)s || '|' || %(parser)s || '|' || %(t5norm)s || '|' || a.entity_id")
_NAME = "(%(snap)s || '|' || {n} || '|primary_name')"
E_LINK = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, 'link', {_COMMON}, 'task5', a.upstream_run_id,
       ({_FL}->>'decided_at')::timestamptz, a.confidence, NULL::text, false, jsonb_build_object('via', v.via)
FROM task9.assertion a
CROSS JOIN LATERAL (VALUES
    ({_MENTION}, 'mention'),
    (CASE {_FL}->'evidence'->>'table'
          WHEN 'title_principal' THEN {_as('imdb.title_principal', f"({_FL}->'evidence'->>'snapshot') || '|' || ({_FL}->>'tconst') || '|' || ({_FL}->'evidence'->>'ordinal')")}
          WHEN 'title_director' THEN {_as('imdb.title_director', f"({_FL}->'evidence'->>'snapshot') || '|' || ({_FL}->>'tconst') || '|' || ({_FL}->'evidence'->>'ordinal')")}
     END, 'imdb_credit'),
    ({_as('imdb.name', _NAME.format(n=f"({_FL}->>'nconst')"))}, 'imdb_name')) AS v(pid, via)
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task5.link_decision' AND {_BUCKET}
"""
E_MENTION_PERSON = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, v.op, {_COMMON}, 'task5', a.upstream_run_id,
       NULL::timestamptz, NULL::numeric, NULL::text, false,
       jsonb_build_object('basis', a.rule_id, 'round', {_FL}->'round', 'group_key', {_FL}->'group_key',
                          'group_reason', {_FL}->'group_reason', 'link_decision_ids', {_FL}->'link_decision_ids',
                          'person_decision_ids', {_FL}->'person_decision_ids')
FROM task9.assertion a
CROSS JOIN LATERAL (
    SELECT {_MENTION} AS pid, 'merge' AS op
    UNION ALL
    SELECT {_as('task5.link_decision', 'y')}, 'link'
    FROM jsonb_array_elements_text({_ARR.format(x=f"{_FL}->'link_decision_ids'")}) AS y
) v
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task5.mention_person' AND {_BUCKET}
"""
E_PERSON = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, v.op, {_COMMON}, v.task, a.upstream_run_id,
       NULL::timestamptz, NULL::numeric, NULL::text, false, jsonb_build_object('via', v.via)
FROM task9.assertion a
CROSS JOIN LATERAL (
    SELECT CASE WHEN starts_with({_FL}->>'canonical_source', 'imdb:')
                THEN {_as('imdb.name', _NAME.format(n=f"substr({_FL}->>'canonical_source', 6)"))}
                WHEN starts_with({_FL}->>'canonical_source', 'mention:')
                THEN {_as('task5.person_mention', f"%(eid)s || '|' || %(parser)s || '|' || %(t5norm)s || '|' || substr({_FL}->>'canonical_source', 9)")}
           END AS pid, 'select' AS op, 'canonical_source' AS via, 'task5' AS task
    WHERE a.upstream_table = 'task5.person'
    UNION ALL
    SELECT {_as('imdb.name', _NAME.format(n='a.raw_value'))}, 'link', 'imdb_name', 'task5'
    WHERE a.upstream_table = 'task5.person_external_id'
    UNION ALL
    SELECT {_as('task5.link_decision', 'y')}, 'select', 'basis_decision', 'task5'
    FROM jsonb_array_elements_text({_ARR.format(x=f"{_FL}->'basis_decision_ids'")}) AS y
    WHERE a.upstream_table = 'task5.person_external_id'
    UNION ALL
    SELECT {_as('imdb.name', _NAME.format(n=f"({_FL}->>'nconst')"))}, 'link', 'imdb_name', 'task7'
    WHERE a.upstream_table = 'task7.imdb_person_ext'
) v
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table IN ('task5.person', 'task5.person_external_id', 'task7.imdb_person_ext')
  AND {_BUCKET}
"""

# ---------------------------------------------------------------- 演职员：署名对齐与成员（R16）

# 页面人物在该作品该角色下的提及 → 人物断言（经任务 5 movie_person_source）
_MPS = f"""SELECT {_as('task5.mention_person', "mps.person_mapping_version_id || '|' || mps.mention_key")} AS pid
           FROM task5.movie_person_source mps
           WHERE mps.person_mapping_version_id = %(pmap)s AND mps.movie_id = a.entity_id
             AND mps.person_id = {{pp}} AND mps.role = {{role}}"""
# 作品有效 tconst 下该人的 IMDb 演职员行断言（导演 title.crew，其余 title.principals）
_IMDB_CREDIT = """SELECT x.assertion_id AS pid FROM task9.assertion x
           WHERE x.stage_key = %(ak)s AND x.entity_type = 'imdb_title' AND x.entity_id = {tconst}
             AND x.field_name = CASE WHEN {role} = 'director' THEN 'director' ELSE 'principal' END
             AND x.raw_value = {nconst} {extra}"""
E_CREDIT = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, v.op, {_COMMON}, 'task7', a.upstream_run_id,
       NULL::timestamptz, NULL::numeric, NULL::text, false, jsonb_build_object('via', v.via)
FROM task9.assertion a
LEFT JOIN task7.movie_resolved mr ON mr.conflict_version_id = %(cv)s AND mr.movie_id = a.entity_id
CROSS JOIN LATERAL (
    -- 署名对齐：页面人物的提及 → 人物，以及对上的 IMDb 演职员行
    SELECT q.pid, 'align' AS op, 'page_credit' AS via
    FROM ({_MPS.format(pp=f"({_FL}->>'page_person_id')", role=f"({_FL}->>'role')")}) q
    WHERE a.field_name = 'credit_alignment'
    UNION ALL
    SELECT q.pid, 'align', 'imdb_credit'
    FROM ({_IMDB_CREDIT.format(tconst='mr.effective_tconst', role=f"({_FL}->>'role')", nconst=f"({_FL}->>'nconst')", extra='')}) q
    WHERE a.field_name = 'credit_alignment' AND {_FL}->>'nconst' IS NOT NULL
    UNION ALL
    -- 演职员成员：成员所含页面人物（resolution_note.page_person_ids）在本角色下的提及 → 人物；页面人物经对齐
    -- 才成为成员本人时再连到对齐断言（任务 7 每个页面人物只对齐一次，记在 cast 角色下，starring 成员沿用）
    SELECT q.pid, 'union', 'page_credit'
    FROM jsonb_array_elements_text({_ARR.format(x=f"{_FL}->'resolution_note'->'page_person_ids'")}) AS pp
    CROSS JOIN LATERAL ({_MPS.format(pp='pp', role=f"({_FL}->>'role')")}) q
    WHERE a.field_name <> 'credit_alignment'
    UNION ALL
    SELECT x.assertion_id, 'align', 'credit_alignment'
    FROM task9.assertion x
    WHERE a.field_name <> 'credit_alignment' AND x.stage_key = %(ak)s AND x.entity_type = 'movie'
      AND x.entity_id = a.entity_id AND x.field_name = 'credit_alignment'
      AND {_FL}->'resolution_note'->'page_person_ids' ? (x.field_locator->>'page_person_id')
      AND x.field_locator->>'resolved_person_id' = {_FL}->>'person_id' AND x.upstream_status = 'aligned'
    UNION ALL
    SELECT q.pid, 'union', 'imdb_credit'
    FROM ({_IMDB_CREDIT.format(tconst=f"coalesce({_FL}->>'tconst', mr.effective_tconst)", role=f"({_FL}->>'role')", nconst=f"({_FL}->>'nconst')", extra=f"AND (({_FL}->>'imdb_ordering') IS NULL OR x.member_key = {_FL}->>'imdb_ordering')")}) q
    WHERE a.field_name <> 'credit_alignment' AND {_FL}->>'nconst' IS NOT NULL
      AND {_FL}->>'source_coverage' IN ('both', 'imdb_only')
    UNION ALL
    SELECT {_as('task7.imdb_person_ext', f"%(eid)s || '|' || %(snap)s || '|' || ({_FL}->>'person_id')")}, 'link',
           'imdb_person_ext'
    WHERE a.field_name <> 'credit_alignment'
) v
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table IN ('task7.credit_alignment', 'task7.movie_person_resolved')
  AND {_BUCKET}
"""

# ---------------------------------------------------------------- 类型成员、发售日期上界

E_GENRE = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, v.op, {_COMMON}, 'task7', a.upstream_run_id,
       NULL::timestamptz, NULL::numeric, NULL::text, false, jsonb_build_object('via', v.via)
FROM task9.assertion a
CROSS JOIN LATERAL (
    SELECT {_as('imdb.title', f"%(snap)s || '|' || ({_FL}->'source_ref'->>'tconst') || '|genres|' || a.member_key")} AS pid,
           'union' AS op, 'imdb_genre' AS via
    WHERE a.upstream_status IN ('both', 'imdb_only')
    UNION ALL
    SELECT x.assertion_id, 'vocab_map', 'page_term'
    FROM task9.assertion x
    WHERE a.upstream_status IN ('both', 'page_only') AND x.stage_key = %(ak)s AND x.entity_type = 'movie'
      AND x.entity_id = a.entity_id AND x.field_name = 'genre_term' AND x.field_locator->'mapped_genres' ? a.member_key
) v
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task7.movie_genre' AND {_BUCKET}
"""
E_BOUND_MEDIA = f"""
SELECT 'assertion', a.assertion_id, 'assertion', p.assertion_id, 'infer', 'media_release:M1-M4', a.extract_rule_version,
       'task6', a.upstream_run_id, NULL::timestamptz, a.confidence, NULL::text, false,
       jsonb_build_object('checks', v.checks, 'evidence_time', v.evidence_time, 'asin', v.asin)
FROM task9.assertion a
CROSS JOIN LATERAL (
    SELECT {_as('task4.field_assertion', "(ev->'basis'->>'assertion_id')")} AS pid, ev->'checks' AS checks,
           ev->'evidence_time' AS evidence_time, ev->'basis'->'asin' AS asin
    FROM jsonb_array_elements({_ARR.format(x=f"{_FL}->'evidence'")}) AS ev
    WHERE ev->>'evidence' = 'media_release'
) v
{_EXISTS}
WHERE a.stage_key = %(ak)s AND a.upstream_table = 'task6.inferred_release_bound'
  AND {_FL}->>'chosen_evidence' = 'media_release' AND {_BUCKET}
"""

FAMILIES: dict[str, list[str]] = {
    "e_extract": [E_EXTRACT],
    "e_t4_split": [E_T4_SPLIT],
    "e_t6": [E_T6],
    "e_t7": [E_T7],
    "e_merge": [E_MERGE],
    "e_product_movie": [E_PRODUCT_MOVIE],
    "e_link": [E_LINK],
    "e_mention_person": [E_MENTION_PERSON],
    "e_person": [E_PERSON],
    "e_credit": [E_CREDIT],
    "e_genre": [E_GENRE],
    "e_bound_media": [E_BOUND_MEDIA],
}


# ---------------------------------------------------------------- 参数与单元

def edge_params(conn: psycopg.Connection, ctx: RunContext, dk: str, ak: str, sk: str) -> dict[str, Any]:
    """派生边各家族共用的命名参数：三个阶段键、R01 各版本、任务 1 装载运行、任务 4 映射运行、任务 5/7 规则版本。"""
    i = ctx.inputs
    p = {**base_params(ctx, ak, sk), **upstream_params(conn, ctx), "dk": dk, "ak": ak}
    p["sk"] = sk
    row = conn.execute("SELECT run_id FROM public.etl_run WHERE extraction_id = %s "
                       "ORDER BY (status = 'succeeded') DESC, started_at DESC LIMIT 1", (i.extraction_id,)).fetchone()
    p["t1run"] = row[0] if row else i.extraction_id
    p["t4run"] = conn.execute("SELECT run_id FROM task4.mapping_version WHERE mapping_version_id = %s",
                              (i.t4_mapping_version_id,)).fetchone()[0]
    return p


def _family_unit(conn: psycopg.Connection, name: str, sqls: Sequence[str], params: dict[str, Any]) -> Unit:
    statements = [edge_insert(s) for s in sqls]

    def write(batch: Sequence[str], _: Any) -> int:
        p = {**params, "batch": list(batch)}
        return sum(conn.execute(s, p).rowcount for s in statements)

    return Unit(name, BUCKETS, write)


def build_derivations(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                      before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """写全部派生边与重放补定位（调用方已持锁并建好运行；assertions 阶段须已完成）。最后检查无环与终止（R19）。"""
    from . import relocate

    sk = require_stage_complete(conn, ctx, "sources")
    ak = require_stage_complete(conn, ctx, "assertions")
    dk = register_stage_key(conn, ctx, STAGE)
    p = edge_params(conn, ctx, dk, ak, sk)
    units = [_family_unit(conn, name, sqls, p) for name, sqls in FAMILIES.items()]
    units += relocate.units(conn, ctx, p)
    graph: dict[str, Any] = {}
    units.append(_final_unit(conn, ctx, dk, ak, sk, graph))
    counts: dict[str, Any] = {"stage_key": dk, "units": process_units(conn, ctx, STAGE, dk, units,
                                                                      batch_size=batch_size,
                                                                      before_commit=before_commit)}
    counts["graph"] = graph or {"skipped": "graph_check 已在之前的运行中完成"}
    counts.update(summary(conn, dk))
    return counts


def imdb_person_gaps(conn: psycopg.Connection, ctx: RunContext, dk: str, ak: str) -> list[str]:
    """IMDb 人物断言（任务 5 外部 ID 与以 IMDb 为规范名来源的人物、任务 7 IMDb 独有人物）在 imdb.name 里没有行时
    （上游事实，如 nm17757708 在演职员表与任务 7 中有、人名文件里没有），退而连到同一 nconst 的 IMDb 演职员行断言，
    并写 dangling_edge 质量问题（detail 说明缺 imdb.name 行）。返回这样的断言 ID。"""
    rows = conn.execute(
        "SELECT a.assertion_id, CASE a.upstream_table WHEN 'task7.imdb_person_ext' THEN a.field_locator->>'nconst' "
        "WHEN 'task5.person' THEN substr(a.field_locator->>'canonical_source', 6) ELSE a.raw_value END, "
        "a.upstream_table, a.extract_rule_version, a.upstream_run_id FROM task9.assertion a "
        "WHERE a.stage_key = %s AND (a.upstream_table IN ('task7.imdb_person_ext', 'task5.person_external_id') "
        "OR (a.upstream_table = 'task5.person' AND starts_with(a.field_locator->>'canonical_source', 'imdb:'))) "
        "AND NOT EXISTS (SELECT 1 FROM task9.derivation d WHERE d.stage_key = %s AND d.child_kind = 'assertion' "
        "AND d.child_id = a.assertion_id AND d.detail->>'via' IN ('imdb_name', 'canonical_source'))",
        (ak, dk)).fetchall()
    out = []
    for aid, nconst, table, version, run in rows:
        credits = [r[0] for r in conn.execute(
            "SELECT assertion_id FROM task9.assertion WHERE stage_key = %s "
            "AND upstream_table IN ('imdb.title_principal', 'imdb.title_director') AND raw_value = %s "
            "ORDER BY assertion_id", (ak, nconst))]
        insert_edges(conn, ctx, dk, [dict(child_id=aid, parent_id=c, operation="link", rule_id="imdb_name_missing",
                                          rule_version=version or ctx.inputs.imdb_snapshot_id,
                                          upstream_task=table.split(".", 1)[0], upstream_run_id=run,
                                          detail={"via": "imdb_credit_fallback", "nconst": nconst})
                                     for c in credits])
        add_issues(conn, ctx, dk, [("assertion", aid, "dangling_edge",
                                    {"reason": "imdb.name 缺少该 nconst 的行，路径改连演职员行", "nconst": nconst,
                                     "upstream_table": table, "fallback_parents": len(credits)})])
        out.append(aid)
    return out


def insert_edges(conn: psycopg.Connection, ctx: RunContext, dk: str, rows: Iterable[dict[str, Any]]) -> int:
    """逐行写边（重放与补救用的小批量）：子、父默认是断言；OpenLineage 标注由操作类型得到（不在闭集内即报错）；
    写入时间取上游运行的时间，取不到时取当前时间。"""
    from psycopg.types.json import Jsonb
    from .rules import ol_of
    data = []
    for r in rows:
        t, s = ol_of(r["operation"])
        data.append((dk, r.get("child_kind", "assertion"), r["child_id"], r.get("parent_kind", "assertion"),
                     r["parent_id"], r["operation"], t, s, r["rule_id"], r["rule_version"], r["upstream_task"],
                     r.get("upstream_run_id"), r.get("upstream_run_id"), r.get("confidence"), r.get("role"),
                     r.get("relocated", False), Jsonb(r.get("detail") or {}), ctx.run_id))
    if not data:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO task9.derivation ({EDGE_COLUMNS}) "
            "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            f"coalesce((SELECT r.at FROM {RUNS} r WHERE r.run_id = %s LIMIT 1), clock_timestamp()), "
            "%s, %s, %s, %s, %s ON CONFLICT DO NOTHING", data)
    return len(data)


def _final_unit(conn: psycopg.Connection, ctx: RunContext, dk: str, ak: str, sk: str, out: dict[str, Any]) -> Unit:
    """阶段最后一个单元：补 IMDb 人名缺行的路径，再检查无环与终止（R19）。与阶段完成标记前的最后一批同事务。"""
    def write(_batch: Sequence[str], _: Any) -> int:
        gaps = imdb_person_gaps(conn, ctx, dk, ak)
        out.update(check_graph(conn, ctx, edge_keys=[dk], ak=ak, sk=sk, issue_key=dk))
        out["imdb_name_missing"] = len(gaps)
        return len(gaps)
    return Unit("graph_check", ["all"], write)


# ---------------------------------------------------------------- 图检查（R19）

def check_graph(conn: psycopg.Connection, ctx: RunContext | None, *, edge_keys: Sequence[str], ak: str, sk: str,
                issue_key: str | None = None) -> dict[str, Any]:
    """R19：派生图无环、每条路径终止于源记录。检查三类问题（issue_key 给出时写质量问题）：
    - dangling：边的子或父不存在（断言不在 ak、源记录不在 sk）；
    - no_parent：断言或字段选择没有任何父边（路径到此中断，到不了源记录）；
    - cycle：断言之间的边成环（Kahn 剥离后剩下的节点）。
    返回各类计数与样例。"""
    keys = list(edge_keys)
    dangling = conn.execute(
        "SELECT d.child_kind, d.child_id, d.parent_kind, d.parent_id, d.operation FROM task9.derivation d "
        "WHERE d.stage_key = ANY(%(keys)s) AND ("
        " (d.child_kind = 'assertion' AND NOT EXISTS (SELECT 1 FROM task9.assertion a WHERE a.stage_key = %(ak)s "
        "   AND a.assertion_id = d.child_id))"
        " OR (d.child_kind = 'selection' AND NOT EXISTS (SELECT 1 FROM task9.field_selection f "
        "   WHERE f.stage_key = d.stage_key AND f.selection_id = d.child_id))"
        " OR (d.parent_kind = 'assertion' AND NOT EXISTS (SELECT 1 FROM task9.assertion a WHERE a.stage_key = %(ak)s "
        "   AND a.assertion_id = d.parent_id))"
        " OR (d.parent_kind = 'source_record' AND NOT EXISTS (SELECT 1 FROM task9.source_record s "
        "   WHERE s.stage_key = %(sk)s AND s.source_record_id = d.parent_id)))",
        {"keys": keys, "ak": ak, "sk": sk}).fetchall()
    no_parent = conn.execute(
        "SELECT 'assertion', a.assertion_id, a.upstream_table FROM task9.assertion a WHERE a.stage_key = %(ak)s "
        "AND NOT EXISTS (SELECT 1 FROM task9.derivation d WHERE d.stage_key = ANY(%(keys)s) "
        "AND d.child_kind = 'assertion' AND d.child_id = a.assertion_id) "
        "UNION ALL SELECT 'selection', f.selection_id, f.entity_type || '.' || f.field_name FROM task9.field_selection f "
        "WHERE f.stage_key = ANY(%(keys)s) AND NOT EXISTS (SELECT 1 FROM task9.derivation d "
        "WHERE d.stage_key = ANY(%(keys)s) AND d.child_kind = 'selection' AND d.child_id = f.selection_id)",
        {"keys": keys, "ak": ak}).fetchall()
    cycle = _cycle_nodes(conn, keys)
    if issue_key is not None and ctx is not None:
        issues = [("derivation", f"{c[0]}:{c[1]}<-{c[2]}:{c[3]}:{c[4]}", "dangling_edge", {"kind": "missing_node"})
                  for c in dangling]
        issues += [(k, i, "dangling_edge", {"kind": "no_parent", "upstream": u}) for k, i, u in no_parent]
        issues += [("assertion", n, "derivation_cycle", {}) for n in cycle]
        add_issues(conn, ctx, issue_key, issues)
    by_table: dict[str, int] = defaultdict(int)
    for _k, _i, u in no_parent:
        by_table[u] += 1
    return {"dangling": len(dangling), "no_parent": len(no_parent), "no_parent_by_upstream": dict(by_table),
            "cycle_nodes": len(cycle), "samples": {"dangling": [list(x) for x in dangling[:5]],
                                                   "no_parent": [list(x) for x in no_parent[:5]],
                                                   "cycle": cycle[:5]}}


def _cycle_nodes(conn: psycopg.Connection, keys: list[str]) -> list[str]:
    """断言之间（与选择 → 断言）的边上做 Kahn 剥离：先去掉没有断言父节点的节点，逐层剥离；剩下的节点在环上或依赖环。"""
    ids: dict[str, int] = {}
    parents: list[list[int]] = []
    children: dict[int, list[int]] = defaultdict(list)

    def nid(x: str) -> int:
        n = ids.get(x)
        if n is None:
            n = ids[x] = len(parents)
            parents.append([])
        return n

    with conn.transaction(), conn.cursor(name="t9_cycle") as cur:     # 服务器端游标须在事务内
        cur.itersize = 100_000
        cur.execute("SELECT child_kind || ':' || child_id, 'assertion:' || parent_id FROM task9.derivation "
                    "WHERE stage_key = ANY(%s) AND parent_kind = 'assertion'", (keys,))
        for c, p in cur:
            ci, pi = nid(c), nid(p)
            parents[ci].append(pi)
            children[pi].append(ci)
    out_deg = [len(ps) for ps in parents]
    queue = [n for n, d in enumerate(out_deg) if d == 0]
    while queue:
        n = queue.pop()
        for c in children.get(n, ()):
            out_deg[c] -= 1
            if out_deg[c] == 0:
                queue.append(c)
    names = {v: k for k, v in ids.items()} if any(out_deg) else {}
    return sorted(names[n].split(":", 1)[1] for n, d in enumerate(out_deg) if d > 0)


def summary(conn: psycopg.Connection, dk: str) -> dict[str, Any]:
    by_op = dict(conn.execute("SELECT operation, count(*) FROM task9.derivation WHERE stage_key = %s GROUP BY 1 "
                              "ORDER BY 1", (dk,)).fetchall())
    relocated = conn.execute("SELECT count(*) FROM task9.derivation WHERE stage_key = %s AND relocated",
                             (dk,)).fetchone()[0]
    by_target = {f"{k}/{'consistent' if c else 'replay_mismatch'}": n for k, c, n in conn.execute(
        "SELECT target_kind, consistent, count(*) FROM task9.relocation WHERE stage_key = %s GROUP BY 1, 2 "
        "ORDER BY 1, 2", (dk,))}
    issues = dict(conn.execute("SELECT issue_code, count(*) FROM task9.quality_issue WHERE stage_key = %s "
                               "GROUP BY 1 ORDER BY 1", (dk,)).fetchall())
    return {"by_operation": by_op, "relocated_edges": relocated, "relocation": by_target, "issues": issues}
