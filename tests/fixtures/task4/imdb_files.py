"""任务 4 合成 IMDb 文件（spec AC02）：按 IMDb 格式手写的小 .tsv.gz 与下载记录 download.json，非真实数据。

文件在测试时现场写入临时目录（gzip 头时间固定为 0，结果可重复），不提交 .tsv.gz。
行用字符串列表表示，None 写成 `\\N`。预期值在 BASIC_EXPECTED，按数据手写，不由实现生成。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path

from etl_task4.imdb import HEADERS

BASE_URL = "https://example.invalid/imdb/"   # 合成数据，非真实来源
DATE = "2026-10-30"
SNAPSHOT_ID = f"imdb_{DATE}"

N = None


def write_gz(path: Path, header: tuple[str, ...], rows: list[list[str | None]]) -> None:
    text = "\t".join(header) + "\n" + "".join("\t".join("\\N" if v is None else v for v in r) + "\n" for r in rows)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(text.encode("utf-8"))
    path.write_bytes(buf.getvalue())


def build_snapshot(directory: Path, *, basics, akas, crew, principals, names, date: str = DATE) -> Path:
    """写五个文件和 download.json，返回目录。"""
    directory.mkdir(parents=True, exist_ok=True)
    data = {"title.basics.tsv.gz": basics, "title.akas.tsv.gz": akas, "title.crew.tsv.gz": crew,
            "title.principals.tsv.gz": principals, "name.basics.tsv.gz": names}
    files = []
    for name, rows in data.items():
        write_gz(directory / name, HEADERS[name], rows)
        raw = (directory / name).read_bytes()
        files.append({"name": name, "url": BASE_URL + name, "started_at": f"{date}T00:00:00+00:00",
                      "finished_at": f"{date}T00:01:00+00:00", "last_modified": "Fri, 30 Oct 2026 00:00:00 GMT",
                      "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    record = {"dataset": "synthetic", "snapshot_id": f"imdb_{date}", "base_url": BASE_URL,
              "downloaded_at": f"{date}T00:02:00+00:00", "files": files}
    (directory / "download.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return directory


def flip_one_byte(directory: Path, name: str) -> None:
    """改动一个字节（大小不变），文件的 SHA-256 随之改变。"""
    path = directory / name
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    path.write_bytes(bytes(raw))


# --------------------------------------------------------------------------------- AC02 数据集

BASIC = dict(
    basics=[
        ["tt0000001", "movie", "Casablanca", "Casablanca", "0", "1942", N, "102", "Drama,Romance"],
        ["tt0000002", "tvMovie", "Cinderella", "Cinderella", "0", "1997", N, "88", "Family,Musical"],
        ["tt0000003", "video", "Fixture Video", "Fixture Video", "0", "2001", N, N, N],
        ["tt0000004", "tvSeries", "Fixture Series", "Fixture Series", "0", "1999", "2003", "45", "Drama"],
        ["tt0000005", "short", "Fixture Short", "Fixture Short", "0", "1990", N, "9", "Short"],
        ["tt0000006", "tvEpisode", "Fixture Episode", "Fixture Episode", "0", "1999", N, "45", "Drama"],
        ["tt0000007", "movie", "Unknown Year", N, "1", N, N, N, N],
        ["tt0000008", "movie", "Breathless", "À bout de souffle", "0", "1960", N, "90", "Crime,Drama"],
    ],
    akas=[
        ["tt0000001", "1", "Casablanca", "US", N, N, N, "0"],
        ["tt0000001", "2", "卡萨布兰卡", "CN", "zh", N, N, "0"],
        ["tt0000008", "1", "À bout de souffle", "FR", "fr", "original", N, "1"],
        ["tt0000008", "2", "Breathless", "US", "en", "imdbDisplay", N, "0"],
        ["tt0000004", "1", "Fixture Series Alias", "US", N, N, N, "0"],   # 属于 tvSeries，不装载
        ["tt0999999", "1", "Not In Titles", "US", N, N, N, "0"],           # 不在保留作品里
    ],
    crew=[
        ["tt0000001", "nm0000001", "nm0000005"],
        ["tt0000002", "nm0000004", N],
        ["tt0000003", N, N],
        ["tt0000004", "nm0000001", N],                                     # tvSeries，不装载
        ["tt0000008", "nm0000006,nm0000001", N],                           # 两位导演，保留串内序号
    ],
    principals=[
        ["tt0000001", "1", "nm0000002", "actor", N, '["Rick"]'],
        ["tt0000001", "2", "nm0000003", "actress", N, '["Ilsa"]'],
        ["tt0000001", "3", "nm0000001", "director", N, N],
        ["tt0000001", "4", "nm0000005", "writer", N, N],                   # category 不在范围
        ["tt0000004", "1", "nm0000002", "actor", N, N],                    # tvSeries
        ["tt0000008", "1", "nm0000006", "director", N, N],
    ],
    names=[
        ["nm0000001", "Michael Curtiz", "1886", "1962", "director", "tt0000001"],
        ["nm0000002", "Humphrey Bogart", "1899", "1957", "actor", "tt0000001"],
        ["nm0000003", "Ingrid Bergman", "1915", "1982", "actress", "tt0000001"],
        ["nm0000004", "Clyde Geronimi", N, N, "director", N],
        ["nm0000005", "Julius J. Epstein", N, N, "writer", N],
        ["nm0000006", "Jean-Luc Godard", "1930", N, "director", N],
        ["nm0000009", "Unreferenced Person", N, N, N, N],                  # 没有被保留作品引用
    ],
)

# 手写预期：各文件的 (rows_read, rows_kept)。
# 作品：tt1、tt2、tt3、tt7、tt8 共 5 部；导演行：tt1×1、tt2×1、tt8×2（tt3 无导演、tt4 不在保留作品里）；
# 人：被保留的导演与演员引用 nm1、nm2、nm3、nm4、nm6，writer（nm5）与无人引用的 nm9 不装载。
BASIC_EXPECTED = {
    "title.basics.tsv.gz": (8, 5),
    "title.akas.tsv.gz": (6, 4),
    "title.crew.tsv.gz": (5, 4),
    "title.principals.tsv.gz": (6, 4),
    "name.basics.tsv.gz": (7, 5),
}
TITLE_TYPES = {"movie": 3, "tvMovie": 1, "video": 1}
OTHER_TYPES_KEPT = {"tt0000004": "tvSeries", "tt0000005": "short"}


# --------------------------------------------------------------------------------- 匹配用数据集（AC05、AC06 的 IMDb 一侧）
# 作品：Casablanca、1960 与 1983 年两部 Breathless、Rocky 与 Rocky II、两部完全等价的 Twin Peak（并列）、
# 只有 tvSeries 的 Only Series、没有人物的 Lonely Title、Alien 与 Aliens。

MATCH = dict(
    basics=[
        ["tt0000101", "movie", "Casablanca", "Casablanca", "0", "1942", N, "102", "Drama,Romance"],
        ["tt0000102", "movie", "Breathless", "À bout de souffle", "0", "1960", N, "90", "Crime,Drama"],
        ["tt0000103", "movie", "Breathless", "Breathless", "0", "1983", N, "100", "Crime,Drama"],
        ["tt0000104", "movie", "Rocky", "Rocky", "0", "1976", N, "119", "Drama,Sport"],
        ["tt0000105", "movie", "Rocky II", "Rocky II", "0", "1979", N, "119", "Drama,Sport"],
        ["tt0000106", "movie", "Twin Peak", "Twin Peak", "0", "1990", N, "100", "Drama"],
        ["tt0000107", "movie", "Twin Peak", "Twin Peak", "0", "1991", N, "100", "Drama"],
        ["tt0000108", "tvSeries", "Only Series", "Only Series", "0", "1999", "2003", "45", "Drama"],
        ["tt0000109", "movie", "Lonely Title", "Lonely Title", "0", "1950", N, N, N],
        ["tt0000110", "movie", "Alien", "Alien", "0", "1979", N, "117", "Horror,Sci-Fi"],
        ["tt0000111", "movie", "Aliens", "Aliens", "0", "1986", N, "137", "Action,Sci-Fi"],
    ],
    akas=[
        ["tt0000102", "1", "À bout de souffle", "FR", "fr", "original", N, "1"],
        ["tt0000102", "2", "Breathless", "US", "en", "imdbDisplay", N, "0"],
        ["tt0000102", "3", "Sin aliento", "ES", "es", N, N, "0"],
        ["tt0000101", "1", "Casablanca", "US", N, N, N, "0"],
    ],
    crew=[
        ["tt0000101", "nm0000101", N], ["tt0000102", "nm0000104", N], ["tt0000103", "nm0000106", N],
        ["tt0000104", "nm0000108", N], ["tt0000105", "nm0000109", N], ["tt0000106", "nm0000110", N],
        ["tt0000107", "nm0000110", N], ["tt0000109", N, N], ["tt0000110", "nm0000112", N],
        ["tt0000111", "nm0000113", N],
    ],
    principals=[
        ["tt0000101", "1", "nm0000102", "actor", N, N], ["tt0000101", "2", "nm0000103", "actress", N, N],
        ["tt0000102", "1", "nm0000105", "actress", N, N], ["tt0000103", "1", "nm0000107", "actor", N, N],
        ["tt0000104", "1", "nm0000109", "actor", N, N], ["tt0000105", "1", "nm0000109", "actor", N, N],
        ["tt0000106", "1", "nm0000111", "actress", N, N], ["tt0000107", "1", "nm0000111", "actress", N, N],
        ["tt0000110", "1", "nm0000114", "actress", N, N], ["tt0000111", "1", "nm0000114", "actress", N, N],
    ],
    names=[[f"nm0000{n}", name, N, N, N, N] for n, name in (
        (101, "Michael Curtiz"), (102, "Humphrey Bogart"), (103, "Ingrid Bergman"), (104, "Jean-Luc Godard"),
        (105, "Jean Seberg"), (106, "Jim McBride"), (107, "Richard Gere"), (108, "John G. Avildsen"),
        (109, "Sylvester Stallone"), (110, "Pat Roe"), (111, "Ann Lee"), (112, "Ridley Scott"),
        (113, "James Cameron"), (114, "Sigourney Weaver"))],
)


# --------------------------------------------------------------------------------- 一致性检查用数据集（AC07）
# Duo Film 有两位导演（X Dir、Y Dir）：页面上只写其中一位的商品都能自动关联到它，但彼此导演完全不重叠。

CONFLICT = dict(
    basics=[["tt0000201", "movie", "Duo Film", "Duo Film", "0", "1980", N, "100", "Drama"]],
    akas=[],
    crew=[["tt0000201", "nm0000201,nm0000202", N]],
    principals=[["tt0000201", "1", "nm0000203", "actor", N, N]],
    names=[["nm0000201", "X Dir", N, N, N, N], ["nm0000202", "Y Dir", N, N, N, N],
           ["nm0000203", "Lead Actor", N, N, N, N]],
)
