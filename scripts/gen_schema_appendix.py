#!/usr/bin/env python3
"""从运行中的 PostgreSQL 目录表生成字段字典附录（列类型、可空、默认值、约束、索引、触发器），写入 data-dictionary.md。

只读查询系统目录，不读业务数据。用法（工作目录：项目根目录，需先 `docker compose up -d`）：
    python3 scripts/gen_schema_appendix.py [库名，默认 etl_movies]
附录位于文档内两个标记之间，重复运行会整体替换，不影响标记之外的手写内容。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "doc" / "task1" / "data-dictionary.md"
BEGIN = "<!-- schema-appendix:begin（由 scripts/gen_schema_appendix.py 生成，勿手改） -->"
END = "<!-- schema-appendix:end -->"
TABLE_ORDER = ("source_file", "extraction", "etl_run", "etl_batch", "review_record",
               "review_staging", "quarantine_record", "quality_issue", "schema_migration")


def psql(db: str, sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", db,
         "-At", "-F", "\x1f", "-c", sql],
        capture_output=True, text=True, check=True).stdout
    return [line.split("\x1f") for line in out.splitlines() if line]


def esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def build(db: str) -> str:
    cols = psql(db, """
        SELECT c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               coalesce(pg_get_expr(d.adbin, d.adrelid), '')
        FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
        ORDER BY c.relname, a.attnum""")
    cons = psql(db, """
        SELECT c.relname, k.contype, k.conname, pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
        WHERE c.relnamespace = 'public'::regnamespace AND k.contype NOT IN ('n', 't')  -- NOT NULL 已在"可空"列，约束触发器列在触发器一节
        ORDER BY c.relname, array_position(ARRAY['p','u','f','c'], k.contype::text), k.conname""")
    idx = psql(db, """
        SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'
        ORDER BY tablename, indexname""")
    trg = psql(db, """
        SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal
        ORDER BY c.relname, t.tgname""")
    version = psql(db, "SELECT string_agg(version, ',') FROM schema_migration")[0][0]
    pg = psql(db, "SHOW server_version")[0][0].split(" ")[0]

    kind = {"p": "主键", "u": "唯一", "f": "外键", "c": "CHECK"}
    tables = [t for t in TABLE_ORDER if any(r[0] == t for r in cols)]
    extra = sorted({r[0] for r in cols} - set(tables))
    L = [BEGIN, "", "## 附录：列类型、约束与索引", "",
         f"由 PostgreSQL {pg} 的系统目录自动生成（库 `{db}`，schema_migration 版本 {version}）。含义见上文，"
         "此处只列数据库实际的类型、可空性、默认值、约束、索引和触发器；与 `sql/001_task1_schema.sql` 一致。", ""]
    for t in tables + extra:
        L += [f"### {t}", "", "| 列 | 类型 | 可空 | 默认值 |", "|---|---|---|---|"]
        for r in cols:
            if r[0] == t:
                L.append(f"| `{r[2]}` | `{esc(r[3])}` | {'否' if r[4] == 't' else '是'} | "
                         f"{('`' + esc(r[5]) + '`') if r[5] else ''} |")
        c = [r for r in cons if r[0] == t]
        if c:
            L += ["", "约束：", ""]
            L += [f"- {kind.get(r[1], r[1])} `{r[2]}`：`{esc(r[3])}`" for r in c]
        i = [r for r in idx if r[0] == t]
        if i:
            L += ["", "索引：", ""]
            L += [f"- `{r[1]}`：`{esc(r[2].split(' USING ', 1)[-1])}`" for r in i]
        g = [r for r in trg if r[0] == t]
        if g:
            L += ["", "触发器：", ""]
            L += [f"- `{r[1]}`：`{esc(r[2])}`" for r in g]
        L.append("")
    L.append(END)
    return "\n".join(L) + "\n"


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "etl_movies"
    appendix = build(db)
    text = DOC.read_text(encoding="utf-8")
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1]
        text = head.rstrip("\n") + "\n\n" + appendix + tail.lstrip("\n")
    else:
        text = text.rstrip("\n") + "\n\n" + appendix
    DOC.write_text(text, encoding="utf-8")
    print(f"已写入 {DOC}（{appendix.count(chr(10))} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
