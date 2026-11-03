# 任务 9 合成夹具

全部为合成数据（非真实商品、作品与评论），预期值按 spec 的 AC 表手算或逐列对照上游行，不由实现生成。任务 9 只读任务 1–8 的结果，所以夹具的重点是**上游表与正式库同形**（任务 7 交接第 6 节第 1 条的教训）。`tests/integration/_task9_helpers.seed` 按下表生成上游行，每个测试开始时重建：

| 上游 | 生成方式 | 说明 |
|---|---|---|
| 任务 2 清单、任务 3 页面字段、任务 4 断言与映射、任务 5 人物、任务 6 上映版本、IMDb 行 | `_task7_helpers.seed`（任务 7 的夹具写法，已与正式库对照过形状） | 世界为 `_task7_worlds.rich_world()` 加 Legend 案例（`_task9_helpers.legend_cases`），`decoys=True` 另写 `map0001`、`rv0002`、`t4n0`、`t3p0` 与旧快照 `imdb_2026-01-01` 的干扰行 |
| 任务 4 有父断言 | 任务 4 自己的 `etl_task4.editions._title_block` 拆 `Legend Picture (1986) [DVD]` | 说明段、`edition.media_format`、`title_year`，父断言为标题断言，来源记录与定位同父断言 |
| 任务 7 结果 | `etl_task7.runner.execute_conflict` 真实运行两次：`t7r3` → `cv0001`（任务 9 默认读它，相当于正式库 `cv0003`）、`t7r1` → `cv0002`（干扰） | `field_candidate`、`movie_title_alias`、`movie_resolved` 等全部由任务 7 代码写入 |
| 任务 1 评论 | `_task8` 同法用任务 1 装载器装入另一个抽取，再把 `etl_run`、`etl_batch`、`review_record`、`review_staging` 原样复制到夹具抽取下，并让夹具抽取指向装载器登记的来源文件 | manifest 的 `download_completed_at` 设为 `2026-10-21T02:25:40+00:00`；装载器的抽取留作“另一个抽取”的干扰 |
| 任务 2 采集日志 | 仿 `_task3_helpers.simulate_crawl` 写 `crawl_run`、`fetch_attempt`；快照写在 tmp 目录 | 正文为 ASIN 字节（任务 7 夹具登记的 `content_hash = sha256(ASIN)`）；`B0Y1` 先超时再成功（尝试序号 2） |
| IMDb 来源文件 | `imdb.source_file` 五个文件各一行（合成 SHA-256） | 列与约束同迁移 301 |
| 任务 4 人工决策 | 任务 4 自己的 `etl_task4.review.import_decisions` | `must_link B0R1–B0R1B` 一条 |
| 任务 8 版本 | `dedup_version` 两行（`rd0001` 基于 `cv0001`、`rd0002` 基于 `cv0002`），连同 `task8.run` 与四个阶段键 | T9-01 只读这些元数据；评论层视图（AC04，T9-03）需改用任务 8 的真实运行 |

## 案例与 AC 场景

| 测试文件 | 案例 | 对应 AC 场景 |
|---|---|---|
| `test_task9_sources.py` | 上游各 schema 全表摘要；装载器的另一个抽取、旧 IMDb 快照；合集 B0LgC 与其他作品的评论 | AC01 原始层 |
| `test_task9_sources.py` | 迁移 801 前后的关系清单；cv、rd、map 不一致或 cv 未核实；另一连接持锁 | AC01、R38 |
| `test_task9_sources.py` | 页面 B0Y1（尝试序号 2）、Legend 的 `title.basics` 行、评论 LU1、人工决策；B0R6 的 `fetched_at` 置空 | AC02 |
| `test_task9_sources.py` | 用 `cv0002`/`rd0002` 再建一次源记录；第 2 批注入故障后续跑 | AC02（ID 稳定、续跑） |
| `test_task9_sources.py` | 快照 B0Lg 正常、B0LgB 内容被改、B0LgD 文件删除 | AC03（源记录层；字段 `trace_status` 在 T9-03 验证） |
| `test_task9_assertions.py` | 页面字段、任务 4 无父与有父断言、提及、IMDb 五类行、Legend 6 条评论 | AC05 |
| `test_task9_assertions.py` | Legend 标题候选：IMDb 与 B0Lg adopted、B0LgB（IMDb 别名）`superseded_by_specific`、B0LgD（无关标题）`rejected`；年份 1985 selected、1986 not_selected | AC05（R15） |
| `test_task9_assertions.py` | `t4n0`、`t3p0` 断言，`rv0002` 候选，`cv0002` 阶段键下的候选 | AC01 断言部分 |
