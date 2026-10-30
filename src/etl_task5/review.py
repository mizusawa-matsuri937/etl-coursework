"""待复核队列与人工决策（spec R29、R30，AC07）。

- export_queue：一个人物映射版本里当前为 review 的候选（跨作品候选取最终判定，外部 ID 候选取 link_decision），
  按“采纳后能连起的作品数”降序（并列按候选 ID 升序）写 CSV，带可直接填写的 decision（must_link/cannot_link）与 note 列。
  文件已存在时不覆盖，除非 force（保护已填写的决定）。
- read_queue / import_decisions：只取 decision 非空的行，转换成人工决策并整体校验——引用不存在的提及或 nconst、
  要求合并两个不同 nconst 时整个文件拒绝，不写任何部分行。导入后人工决策集的哈希变化，重新运行即得到新的人物映射版本。
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .run import human_set_hash

COLUMNS = ("queue_rank", "impact", "kind", "candidate_id", "left_mention", "right_mention", "left_raw", "right_raw",
           "role", "left_work", "right_work", "nconst", "imdb_url", "collaborators", "reason", "decision", "note")


class ReviewInputError(ValueError):
    """人工决策文件不合格（确定性错误，整个文件被拒绝）。"""


@dataclass(frozen=True)
class HumanRow:
    kind: str
    left: str
    right: str | None
    note: str = ""


def queue_rows(conn: psycopg.Connection, pmap: str) -> list[tuple]:
    """一个人物映射版本里当前为 review 的候选，按影响降序（并列：跨作品在先、候选 ID 升序）。
    每行 (impact, 类型序, 候选 ID, (kind, cid, left, right, left_raw, right_raw, role, left_work, right_work, nconst, imdb_url, collaborators, reason))。"""
    v = conn.execute("SELECT extraction_id, rule_version, parser_version, norm_version, "
                     "t4_mapping_version_id || '|' || parser_version || '|' || norm_version || '|' || rule_version "
                     "|| '|' || imdb_snapshot_id "
                     "FROM task5.person_mapping_version WHERE person_mapping_version_id = %s", (pmap,)).fetchone()
    if v is None:
        raise ReviewInputError(f"人物映射版本 {pmap} 不存在")
    eid, rule, parser, norm, link_stage_key = v
    person_of = dict(conn.execute("SELECT mention_key, person_id FROM task5.mention_person WHERE "
                                  "person_mapping_version_id = %s", (pmap,)).fetchall())
    works_of: dict[str, set[str]] = {}
    group_person: dict[str, str] = {}
    for gkey, mkey in conn.execute("SELECT group_key, mention_key FROM task5.group_member m JOIN task5.person_group g "
                                   "USING (extraction_id, stage_key, group_key) WHERE g.extraction_id = %s",
                                   (eid,)).fetchall():
        if mkey in person_of:
            group_person[gkey] = person_of[mkey]
            works_of.setdefault(person_of[mkey], set()).add(gkey.split("|", 1)[0])
    raw = dict(conn.execute("SELECT mention_key, raw_value FROM task5.person_mention WHERE extraction_id = %s "
                            "AND parser_version = %s AND norm_version = %s", (eid, parser, norm)).fetchall())
    rows = []
    for cid, ga, gb, tier, reason, feats, gen in conn.execute(
            "SELECT c.person_candidate_id, c.group_a, c.group_b, d.tier, d.reason, d.features, c.generator "
            "FROM task5.person_candidate c JOIN task5.person_decision d USING (person_candidate_id) "
            "WHERE d.rule_version = %s AND d.round = (SELECT max(round) FROM task5.person_decision x "
            "WHERE x.person_candidate_id = c.person_candidate_id AND x.rule_version = d.rule_version) "
            "AND d.tier = 'review' AND c.extraction_id = %s ORDER BY c.person_candidate_id", (rule, eid)).fetchall():
        pa, pb = group_person.get(ga), group_person.get(gb)
        if pa is None or pb is None or pa == pb:
            continue
        impact = len(works_of[pa] | works_of[pb]) - max(len(works_of[pa]), len(works_of[pb]))
        left, right = ga.split("|", 1)[1], gb.split("|", 1)[1]
        rows.append((impact, 0, cid, ("person", cid, left, right, raw[left], raw[right],
                                      "/".join(feats["roles"]["a"]), ga.split("|")[0], gb.split("|")[0], "", "",
                                      ";".join(feats["collaborators"]["same_name"]), reason)))
    for cid, mkey, nconst, reason in conn.execute(
            "SELECT c.link_candidate_id, c.mention_key, c.nconst, d.reason FROM task5.link_candidate c "
            "JOIN task5.link_decision d USING (link_candidate_id) WHERE c.extraction_id = %s AND d.rule_version = %s "
            "AND d.tier = 'review' AND c.stage_key = %s AND c.mention_key = ANY(%s) ORDER BY c.link_candidate_id",
            (eid, rule, link_stage_key, list(person_of))).fetchall():
        person = person_of[mkey]
        ext = conn.execute("SELECT 1 FROM task5.person_external_id WHERE person_mapping_version_id = %s AND "
                           "person_id = %s", (pmap, person)).fetchone()
        if ext:
            continue
        other = conn.execute("SELECT person_id FROM task5.person_external_id WHERE person_mapping_version_id = %s AND "
                             "external_id = %s", (pmap, nconst)).fetchone()
        impact = (len(works_of.get(other[0], set())) + 1 - len(works_of[person])) if other else 1
        rows.append((impact, 1, cid, ("link", cid, mkey, f"imdb:{nconst}", raw[mkey], nconst, "", "", "", nconst,
                                      f"https://www.imdb.com/name/{nconst}/", "", reason)))
    rows.sort(key=lambda r: (-r[0], r[1], r[2]))
    return rows


def export_queue(conn: psycopg.Connection, pmap: str, path: Path, *, force: bool = False) -> int:
    path = Path(path)
    if path.exists() and not force:
        raise FileExistsError(f"{path} 已存在，不覆盖已填写的决定（加 force 覆盖）")
    rows = queue_rows(conn, pmap)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for rank, (impact, _, _, body) in enumerate(rows, start=1):
            kind, cid, left, right, lraw, rraw, role, lw, rw, nc, url, collab, reason = body
            w.writerow([rank, impact, kind, cid, left, right, lraw, rraw, role, lw, rw, nc, url, collab, reason, "", ""])
    return len(rows)


def read_queue(path: Path) -> list[HumanRow]:
    out = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            decision = (r.get("decision") or "").strip()
            if not decision:
                continue
            if decision not in ("must_link", "cannot_link", "detach"):
                raise ReviewInputError(f"第 {r.get('queue_rank')} 行的 decision {decision!r} 不是 must_link/cannot_link/detach")
            out.append(HumanRow(decision, r["left_mention"], None if decision == "detach" else r["right_mention"],
                                r.get("note") or ""))
    return out


def import_decisions(conn: psycopg.Connection, extraction_id: str, rows: list[HumanRow], *, annotator: str,
                     file_sha256: str, pmap: str | None = None) -> int:
    """整体校验后一次写入；任何一行不合格都不写。返回写入行数。"""
    v = conn.execute("SELECT person_mapping_version_id, parser_version, norm_version FROM "
                     "task5.person_mapping_version WHERE extraction_id = %s "
                     + ("AND person_mapping_version_id = %s " if pmap else "")
                     + "ORDER BY created_at DESC, person_mapping_version_id DESC LIMIT 1",
                     (extraction_id, pmap) if pmap else (extraction_id,)).fetchone()
    if v is None:
        raise ReviewInputError("还没有人物映射版本，不能导入人工决策")
    pmap, parser, norm = v
    known = {r[0] for r in conn.execute("SELECT mention_key FROM task5.person_mention WHERE extraction_id = %s AND "
                                        "parser_version = %s AND norm_version = %s", (extraction_id, parser, norm)).fetchall()}
    ext = dict(conn.execute("SELECT mp.mention_key, e.external_id FROM task5.mention_person mp JOIN "
                            "task5.person_external_id e ON e.person_mapping_version_id = mp.person_mapping_version_id "
                            "AND e.person_id = mp.person_id WHERE mp.person_mapping_version_id = %s", (pmap,)).fetchall())
    problems = []
    for n, r in enumerate(rows, start=1):
        for side in (r.left, r.right):
            if side is None:
                continue
            if side.startswith("imdb:"):
                if not conn.execute("SELECT 1 FROM task5.person_mapping_version p JOIN imdb.name n ON n.snapshot_id = "
                                    "p.imdb_snapshot_id AND n.nconst = %s WHERE p.person_mapping_version_id = %s",
                                    (side[5:], pmap)).fetchone():
                    problems.append(f"第 {n} 行：IMDb 人物 {side[5:]} 不存在")
            elif side not in known:
                problems.append(f"第 {n} 行：提及 {side} 不存在")
        if r.kind == "must_link" and not problems:
            a = ext.get(r.left)
            b = r.right[5:] if r.right.startswith("imdb:") else ext.get(r.right)
            if a and b and a != b:
                problems.append(f"第 {n} 行：must_link 要合并两个不同的 IMDb 人物（{a} 与 {b}），这是两个人")
    if problems:
        raise ReviewInputError("人工决策整体被拒绝，未写入任何行：" + "；".join(problems))
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO task5.human_decision (extraction_id, set_hash, kind, left_entity, right_entity, note, "
                "annotator, import_file_sha256, person_mapping_version_id) VALUES (%s, 'pending', %s, %s, %s, %s, %s, "
                "%s, %s) ON CONFLICT DO NOTHING",
                [(extraction_id, r.kind, r.left, r.right, r.note, annotator, file_sha256, pmap) for r in rows])
        conn.execute("UPDATE task5.human_decision SET set_hash = %s WHERE extraction_id = %s",
                     (human_set_hash(conn, extraction_id), extraction_id))
    return len(rows)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- 命令行

def main(argv: list[str] | None = None) -> int:
    """python -m etl_task5.review queue [--pmap X] [--force] | import <csv> --annotator <姓名>。
    导入只写人工决策；之后再触发一次 DAG（换 run id）即得到新的人物映射版本。"""
    import argparse
    import json

    from etl_task1.config import load_settings
    from etl_task1.db import connect
    from etl_task2.crawl.crawler import latest_full_extraction

    from .check import latest_pmap

    ap = argparse.ArgumentParser(description="任务 5 待复核队列与人工决策")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue", help="导出待复核队列（按影响降序）")
    i = sub.add_parser("import", help="导入已填写的队列文件")
    i.add_argument("file")
    i.add_argument("--annotator", required=True)
    for p in (q, i):
        p.add_argument("--extraction-id")
        p.add_argument("--pmap")
    q.add_argument("--force", action="store_true", help="覆盖已存在的队列文件（会丢掉已填写的决定）")
    q.add_argument("--out", help="输出路径，默认 outputs/task5/<抽取>/<版本>/review/review_queue.csv")
    args = ap.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        pmap = args.pmap or latest_pmap(conn, eid)
        if args.cmd == "queue":
            path = Path(args.out) if args.out else s.output_dir / "task5" / eid / pmap / "review" / "review_queue.csv"
            n = export_queue(conn, pmap, path, force=args.force)
            print(json.dumps({"person_mapping_version_id": pmap, "path": str(path), "rows": n}, ensure_ascii=False))
        else:
            path = Path(args.file)
            n = import_decisions(conn, eid, read_queue(path), annotator=args.annotator, file_sha256=file_sha256(path),
                                 pmap=pmap)
            print(json.dumps({"imported": n, "human_set_hash": human_set_hash(conn, eid),
                              "next": "触发 DAG amazon_movies_task5_person（新的 run id）生成新的人物映射版本"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
