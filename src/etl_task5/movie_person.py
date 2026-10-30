"""MoviePerson：作品–人物关系，角色 director、starring、cast，保留来源角色与署名顺序（spec R31–R34，AC08）。

对每条 resolvable 且归属到作品（attribution = movie）的提及：导演 → director；署名行演员 → starring 与 cast；详情要点演员 → cast。
同一（作品、人物、角色）合成一行，全部来源提及写入 movie_person_source。署名顺序：director、starring 取署名行序号，
cast 有署名行来源时取其序号、只有详情要点来源时为 NULL（字母顺序不是署名顺序）；多个来源不一致取最小并设 order_conflict。
合集提及只有在外部 ID 把它归到成员作品时才有关系（person_group.movie_id），其余为 unattributed，不建关系。
全局步骤，一个事务生成某个人物映射版本的全部关系；已生成时（版本 counts 里有 movie_person）什么都不做，所以 reused 不重写。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .run import BeforeCommit, copy_rows

STAGE = "movie_person"
ROLES = ("director", "starring", "cast")


def roles_of(role_class: str, is_byline: bool) -> tuple[str, ...]:
    """提及的角色类别与区域 → MoviePerson 角色（R32）。"""
    if role_class == "director":
        return ("director",)
    return ("starring", "cast") if is_byline else ("cast",)


def built(conn: psycopg.Connection, pmap: str) -> bool:
    row = conn.execute("SELECT counts ? 'movie_person' FROM task5.person_mapping_version "
                       "WHERE person_mapping_version_id = %s", (pmap,)).fetchone()
    if row is None:
        raise ValueError(f"人物映射版本 {pmap} 不存在")
    return bool(row[0])


def build_movie_person(conn: psycopg.Connection, pmap: str, *,
                       before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """为一个人物映射版本生成 MoviePerson（单事务，幂等）。返回计数；已生成过时 reused = True。"""
    v = conn.execute(
        "SELECT extraction_id, parser_version, norm_version, run_id, counts, t4_mapping_version_id, rule_version, "
        "imdb_snapshot_id FROM task5.person_mapping_version WHERE person_mapping_version_id = %s", (pmap,)).fetchone()
    if v is None:
        raise ValueError(f"人物映射版本 {pmap} 不存在")
    eid, parser, norm, run_id, counts, t4, rule, snap = v
    if "movie_person" in counts:
        return {**counts["movie_person"], "reused": True}
    stage_key = "|".join((t4, parser, norm, rule, snap))        # 与 RunInputs.stage_key 的非提及阶段同形
    rows = conn.execute(
        "SELECT mp.mention_key, mp.person_id, g.movie_id, m.role_class, m.is_byline, m.byline_rank, m.list_position, "
        "m.label_raw, m.section, m.asin, m.product_kind "
        "FROM task5.mention_person mp "
        "JOIN task5.person_mention m ON m.extraction_id = mp.extraction_id AND m.parser_version = mp.parser_version "
        "AND m.norm_version = mp.norm_version AND m.mention_key = mp.mention_key "
        "JOIN task5.person_group g ON g.extraction_id = mp.extraction_id AND g.stage_key = %s "
        "AND g.group_key = mp.group_key "
        "WHERE mp.person_mapping_version_id = %s AND mp.attribution = 'movie' ORDER BY mp.mention_key COLLATE \"C\"",
        (stage_key, pmap)).fetchall()
    unattributed = conn.execute("SELECT count(*) FROM task5.mention_person WHERE person_mapping_version_id = %s "
                                "AND attribution = 'unattributed'", (pmap,)).fetchone()[0]
    sources: dict[tuple[str, str, str], list[tuple]] = defaultdict(list)        # (作品, 人物, 角色) → 来源提及
    for mkey, pid, movie, rclass, byline, rank, pos, label, section, asin, kind in rows:
        tag = f"{section}:{label or section}"
        for role in roles_of(rclass, byline):
            sources[(movie, pid, role)].append((mkey, asin, rank, pos, tag, kind))
    mp_rows, src_rows, issues = [], [], []
    for (movie, pid, role), srcs in sorted(sources.items()):
        ranks = sorted({s[2] for s in srcs if s[2] is not None})
        conflict = len(ranks) > 1
        labels = sorted({s[4] for s in srcs} | {f"collection:{s[1]}" for s in srcs if s[5] == "collection"})
        mp_rows.append((pmap, movie, pid, role, ranks[0] if ranks else None, conflict, labels, len(srcs)))
        src_rows += [(pmap, movie, pid, role, s[0], s[1], s[2], s[3]) for s in srcs]
        if conflict:
            issues.append((run_id, eid, f"{pmap}:{movie}:{pid}:{role}", "order_conflict",
                           Jsonb({"ranks": ranks, "mentions": [s[0] for s in srcs]})))
    by_role = Counter(r[3] for r in mp_rows)
    roles_in_work: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in mp_rows:
        roles_in_work[(r[1], r[2])].add("cast" if r[3] == "starring" else r[3])      # starring 与 cast 不算多角色
    out = {"relations": len(mp_rows), "sources": len(src_rows), "by_role": dict(sorted(by_role.items())),
           "by_source_section": dict(sorted(Counter(s[4].split(":")[0] for ss in sources.values() for s in ss).items())),
           "order_conflicts": sum(1 for r in mp_rows if r[5]),
           "multi_role_persons": len({pid for (_, pid), rs in roles_in_work.items() if len(rs) > 1}),
           "unattributed_mentions": unattributed}
    with conn.transaction():
        copy_rows(conn, "task5.movie_person", ("person_mapping_version_id", "movie_id", "person_id", "role",
                                               "credit_order", "order_conflict", "source_labels", "mention_count"),
                  mp_rows)
        copy_rows(conn, "task5.movie_person_source", ("person_mapping_version_id", "movie_id", "person_id", "role",
                                                      "mention_key", "asin", "byline_rank", "list_position"), src_rows)
        if issues:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO task5.quality_issue (run_id, extraction_id, entity_id, issue_code, detail) "
                                "VALUES (%s, %s, %s, %s, %s)", issues)
        conn.execute("UPDATE task5.person_mapping_version SET counts = counts || jsonb_build_object('movie_person', "
                     "%s::jsonb) WHERE person_mapping_version_id = %s", (Jsonb(out), pmap))
        if before_commit:
            before_commit(STAGE, 1)
    return {**out, "reused": False}
