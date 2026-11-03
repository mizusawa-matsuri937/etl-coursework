"""任务 9 的上游只读取数（spec R01、R03）：每条查询都带抽取与各上游版本条件，不在任务 1–8 与 imdb 的表上写任何东西。

这里集中定义“被下游引用”的实体集合（design“build_sources”）：
- 页面：任务 4 参与匹配（match_scope = matched）且登记了页面源记录的商品（正式库 9,590 个）。
- IMDb 作品行（title.basics）：任务 7 有效 tconst、任务 4 映射作品的 tconst、任务 4 已登记的 IMDb 源记录、任务 6
  上映版本考虑过的 IMDb 候选与任务 7 两个阶段键下 IMDb 候选的 tconst 的并集。
- IMDb 演职员行（title.crew、title.principals）：只取任务 7 有效 tconst 与任务 4 映射作品 tconst（演职员并集与对齐所用）。
- IMDb 别名行（title.akas）：只取任务 7 作品别名（cv 的 scalar 阶段键，alias_kind = aka）引用的行号。
- IMDb 人名行（name.basics）：上面演职员行的 nconst、任务 5 外部 ID（pmap）与任务 7 登记的 IMDb 独有人物。
- 评论：推断上界以评论为证据的作品（rv），其 edition 关系电影类商品上的全部有效评论（任务 6 R20 的口径，R22）。
"""

from __future__ import annotations

from typing import Any

import psycopg

from .run import RunInputs


def page_asins(conn: psycopg.Connection, i: RunInputs) -> list[str]:
    """参与匹配且有任务 4 页面源记录的商品（排序后返回）。"""
    return [r[0] for r in conn.execute(
        "SELECT e.asin FROM task4.product_edition e "
        "WHERE e.extraction_id = %s AND e.t3_rule_version = %s AND e.match_scope = 'matched' "
        "AND EXISTS (SELECT 1 FROM task4.source_record s WHERE s.source_type = 'page' "
        "            AND s.locator->>'extraction_id' = e.extraction_id AND s.locator->>'asin' = e.asin) "
        "ORDER BY e.asin COLLATE \"C\"", (i.extraction_id, i.t3_rule_version))]


def _cv_keys(i: RunInputs) -> tuple[str, str]:
    """(scalar_stage_key, edition_stage_key)。"""
    assert i.cv_stage_keys is not None
    edition, _link, scalar, _multi = i.cv_stage_keys
    return scalar, edition


def credit_tconsts(conn: psycopg.Connection, i: RunInputs) -> set[str]:
    """演职员行所属的 tconst：任务 7 有效 tconst 与任务 4 映射作品的 tconst。"""
    return {r[0] for r in conn.execute(
        "SELECT effective_tconst FROM task7.movie_resolved WHERE conflict_version_id = %s AND effective_tconst IS NOT NULL "
        "UNION SELECT tconst FROM task4.movie WHERE mapping_version_id = %s AND tconst IS NOT NULL",
        (i.conflict_version_id, i.t4_mapping_version_id))}


def title_tconsts(conn: psycopg.Connection, i: RunInputs) -> set[str]:
    """作品行（title.basics）被引用的 tconst（见模块说明）。"""
    scalar, edition = _cv_keys(i)
    rows = conn.execute(
        "SELECT locator->>'tconst' FROM task4.source_record WHERE source_type = 'imdb' AND locator->>'snapshot' = %s "
        "UNION SELECT c.source_key FROM task6.release_selection s CROSS JOIN LATERAL unnest(s.considered_candidate_ids) x(id) "
        "  JOIN task6.release_candidate c ON c.candidate_id = x.id "
        "  WHERE s.release_version_id = %s AND c.source <> 'amazon_title_year' "
        "UNION SELECT source_ref->>'tconst' FROM task7.field_candidate WHERE extraction_id = %s "
        "  AND stage_key IN (%s, %s) AND source_ref ? 'tconst'",
        (i.imdb_snapshot_id, i.t6_release_version_id, i.extraction_id, scalar, edition)).fetchall()
    return {r[0] for r in rows if r[0]} | credit_tconsts(conn, i)


def aka_lines(conn: psycopg.Connection, i: RunInputs) -> dict[str, list[int]]:
    """tconst → 任务 7 作品别名引用的 title.akas 行号。"""
    scalar, _ = _cv_keys(i)
    out: dict[str, list[int]] = {}
    for tconst, line in conn.execute(
            "SELECT DISTINCT source_ref->>'tconst', (source_ref->>'line_no')::bigint FROM task7.movie_title_alias "
            "WHERE extraction_id = %s AND stage_key = %s AND alias_kind = 'aka' ORDER BY 1, 2",
            (i.extraction_id, scalar)):
        out.setdefault(tconst, []).append(line)
    return out


def name_nconsts(conn: psycopg.Connection, i: RunInputs, credit: set[str]) -> list[str]:
    """人名行被引用的 nconst（排序后返回）。"""
    rows = conn.execute(
        "SELECT nconst FROM imdb.title_director WHERE snapshot_id = %s AND tconst = ANY(%s) "
        "UNION SELECT nconst FROM imdb.title_principal WHERE snapshot_id = %s AND tconst = ANY(%s) "
        "UNION SELECT external_id FROM task5.person_external_id WHERE person_mapping_version_id = %s AND system = 'imdb' "
        "UNION SELECT nconst FROM task7.imdb_person_ext WHERE extraction_id = %s AND imdb_snapshot_id = %s",
        (i.imdb_snapshot_id, sorted(credit), i.imdb_snapshot_id, sorted(credit), i.t5_person_mapping_version_id,
         i.extraction_id, i.imdb_snapshot_id)).fetchall()
    return sorted({r[0] for r in rows if r[0]})


def review_bound_movies(conn: psycopg.Connection, i: RunInputs) -> list[str]:
    """推断上界以评论为证据的作品（排序后返回）。"""
    return [r[0] for r in conn.execute(
        "SELECT movie_id FROM task6.inferred_release_bound WHERE release_version_id = %s AND chosen_evidence = 'review' "
        "ORDER BY movie_id COLLATE \"C\"", (i.t6_release_version_id,))]


# 某批作品的依赖评论：edition 关系、任务 4 商品版本为电影类的商品上的全部有效评论（任务 6 R20）。
# 命名参数见 review_params。
REVIEW_DEPENDENCIES_SQL = """
SELECT DISTINCT b.movie_id, s.source_review_id, s.product_id
FROM task6.inferred_release_bound b
JOIN task4.product_movie pm ON pm.mapping_version_id = %(map)s AND pm.movie_id = b.movie_id
                           AND pm.relation_type = 'edition'
JOIN task4.product_edition pe ON pe.extraction_id = %(eid)s AND pe.t3_rule_version = %(t3)s AND pe.asin = pm.asin
                             AND pe.product_kind = 'movie'
JOIN public.review_staging s ON s.extraction_id = %(eid)s AND s.product_id = pm.asin
WHERE b.release_version_id = %(rv)s AND b.chosen_evidence = 'review' AND b.movie_id = ANY(%(movies)s)
"""


def review_params(i: RunInputs, movies: list[str]) -> dict[str, Any]:
    return {"map": i.t4_mapping_version_id, "eid": i.extraction_id, "t3": i.t3_rule_version,
            "rv": i.t6_release_version_id, "movies": list(movies)}


def t6_movies(conn: psycopg.Connection, i: RunInputs) -> list[str]:
    """上映版本有选择记录的作品（排序后返回）。"""
    return [r[0] for r in conn.execute(
        "SELECT movie_id FROM task6.release_selection WHERE release_version_id = %s ORDER BY movie_id COLLATE \"C\"",
        (i.t6_release_version_id,))]


def t7_entities(conn: psycopg.Connection, i: RunInputs) -> tuple[list[str], list[str]]:
    """任务 7 cv 两个阶段键下有候选的作品与商品（各自排序后返回）。"""
    scalar, edition = _cv_keys(i)
    movies = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s "
        "AND entity_type = 'movie' ORDER BY 1", (i.extraction_id, scalar))]
    editions = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s "
        "AND entity_type = 'edition' ORDER BY 1", (i.extraction_id, edition))]
    return sorted(movies), sorted(editions)


def manual_tables(conn: psycopg.Connection, i: RunInputs) -> dict[str, int]:
    """任务 4、5 的人工决策行数（正式库均为 0，H07）。"""
    return {t: conn.execute(f"SELECT count(*) FROM {t} WHERE extraction_id = %s", (i.extraction_id,)).fetchone()[0]
            for t in ("task4.human_decision", "task5.human_decision")}
