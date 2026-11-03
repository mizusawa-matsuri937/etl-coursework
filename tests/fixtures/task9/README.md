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
| 补成正式库形状（T9-02，任务 7 运行前） | `_prod_shape_t6_candidates`、`_merge_decisions`、`_missing_mentions`、`_person_links` | 任务 7 夹具省略了几处正式库都有的列，派生边要靠它们找父节点；形状逐项对照 `etl_movies` 的只读查询：任务 6 候选的 `source_ref`（IMDb 为文件、tconst、行号、快照；页面标题年份为 ASIN、区域、序号、任务 4 断言 ID，`source_key = <ASIN>:<断言 ID>`）；`B0Lg`、`B0LgC#1`、`B0C1` 改为 `auto_decision`，写 `task4.candidate` 与 `merge_decision`（`features.evidence` 的 page/imdb 结构同正式库）；没有来源提及的 `movie_person` 补一条署名行提及（正式库每条都有来源提及）；页面人物的 `canonical_source` 改为 `mention:<提及键>`；已关联 IMDb 的人物第一条提及经 `credits`（`link_candidate` + auto `link_decision`，证据为演职员表序号与行号），其余提及经 `co_credit` 第 2 轮 `person_decision` |
| 评论时间与推断上界（T9-02，评论复制之后） | `_review_times_and_bounds` | `task6.product_review_time`、`movie_review_time` 由任务 6 自己的 `reviews.product_review_times` / `movie_review_times` 写入；每个推断上界补上正式库都有的 `bound_evidence`（`basis`、B1–B5 或 M1–M4）与 `inferred_release_bound.basis`、`conditions` |
| 商品层补形（T9-04，只在查询测试里调用） | `_task9_helpers.add_products` | 任务 3 类别：已采集参与匹配商品为 `dp_page`、`t3p1`、电影单品 `MV1`、合集 `CL2`（正式库 t3r2 的结构化规则）；未采集商品 `B0NC` 的清单行取默认状态 `not_collected`，类别按任务 3 `_classify_no_page` 的写法为 `pending`/`missing`/`NP1`、`pending_reason = not_collected`（该函数不能直接调用：夹具 `final_url` 不是 amazon.com，任务 3 分组 SQL 会把全部夹具商品归为 `other_site`）。任务 4 匹配状态按 `cluster` 的口径：同一作品多个商品为 `merged`，其余 `singleton`，合集 `collection_split`、原因“已链接/成员数 members linked” |

## 案例与 AC 场景

| 测试文件 | 案例 | 对应 AC 场景 |
|---|---|---|
| `test_task9_sources.py` | 上游各 schema 全表摘要；装载器的另一个抽取、旧 IMDb 快照；合集 B0LgC 与其他作品的评论 | AC01 原始层 |
| `test_task9_sources.py` | 迁移 801 前后的关系清单；cv、rd、map 不一致或 cv 未核实；另一连接持锁 | AC01、R38 |
| `test_task9_sources.py` | 页面 B0Y1（尝试序号 2）、Legend 的 `title.basics` 行、评论 LU1、人工决策；B0R6 的 `fetched_at` 置空 | AC02 |
| `test_task9_sources.py` | 用 `cv0002`/`rd0002` 再建一次源记录；第 2 批注入故障后续跑 | AC02（ID 稳定、续跑） |
| `test_task9_sources.py` | 快照 B0Lg 正常、B0LgB 内容被改、B0LgD 文件删除 | AC03（源记录层） |
| `test_task9_assertions.py` | 页面字段、任务 4 无父与有父断言、提及、IMDb 五类行、Legend 6 条评论 | AC05 |
| `test_task9_assertions.py` | Legend 标题候选：IMDb 与 B0Lg adopted、B0LgB（IMDb 别名）`superseded_by_specific`、B0LgD（无关标题）`rejected`；年份 1985 selected、1986 not_selected | AC05（R15） |
| `test_task9_assertions.py` | `t4n0`、`t3p0` 断言，`rv0002` 候选，`cv0002` 阶段键下的候选 | AC01 断言部分 |
| `test_task9_derivations.py` | Y2 页面导演 Sam Director 与 IMDb nm6000001 对齐；C1 Es One 兼 starring 与 cast；C9 多人串；另造 St：页面主演 Garey Starling 按 cast 对齐到 IMDb 后 starring 沿用 | AC06（R16） |
| `test_task9_derivations.py` | B0Lg、B0LgC#1 经合并决策，B0LgB 为 singleton；Es One 的提及 credits 与 co_credit 第 2 轮；G1 类型成员 | AC06（R20） |
| `test_task9_derivations.py` | 全部边的闭集与标注；无环与终止；注入环、悬空父断言与父源记录、删掉一条抽取边；第 3 批提交前注入故障后续跑；同输入重跑 | AC07、AC01 派生部分 |
| `test_task9_relocate.py` | G1 的类型词面（genre 区与面包屑）；B0R1B 的 video_system PAL；Legend 第二早评论 LU2；改动上游 `movie_genre_term`、`attributes_detail`、`second_review_time` 制造不一致 | AC08 |
| `test_task9_relocate.py` | Legend 三个 edition 商品共 7 条评论（加 B0LgD 的 LU8）与合集 B0LgC 的 LU6；Mv1 只有 B0E1 的发售日期证据（`inferred = media_release`） | AC09 |
| `test_task9_selections.py` | 作品与版本标量的 task7 状态（含 conflict_unresolved、missing）；演职员与类型成员、director 为空的作品；Legend 推断上界与没有上界的作品 | AC10（R23、R24） |
| `test_task9_selections.py` | 改 Legend `title` 的选择值、删一个类型成员选择；夹具人工决策（未被 `map0002` 采用）与补给 B0R1B 商品断言的人工修订边 | AC10（R26、R27） |
| `test_task9_selections.py` | IMDb 行、提及、评论、合集成员有断言但无选择；无任务 8 断言与边 | AC01 选择部分 |
| `test_task9_selections.py` | B0LgB 快照内容被改、B0LgD 删除后的版本字段；同 AC08 不一致夹具下 G1 类型成员、B0R1B PAL、Legend 上界 | AC03、AC08 字段部分 |
| `test_task9_selections.py` | 任务 8 小世界（`_task8_helpers.seed_small_world` 经任务 1、8 自己的运行）的 EU1、EU13、EU12、EL1、EL2；t9 夹具的 B0Lg 商品视图（夹具无 task3 分类与 task4 匹配状态行，列为空） | AC04 |
| `test_task9_query.py` | Legend `release_year`（IMDb 1985 采用、两条 1986 rejected）；Y2 `director`（页面与 IMDb title.crew 对齐） | AC11 |
| `test_task9_query.py` | B0Lg（电影单品，merged）、B0LgC（合集，collection_split）、B0NC（未采集）、不在清单的 ASIN；任务 8 小世界 EU1、EU13、EU12、EL1、EL2；Legend 最早评论 LU1 | AC12 |
| `test_task9_query.py` | B0Lg 快照、Legend 的 tconst、Y2 导演 nm6000001、评论 LU1：查询结果与测试里沿派生边手工追踪的闭包与最短路径相同 | AC13 |
| `test_task9_query.py` | Es One（credits 1 条、co_credit 第 2 轮 2 条提及，外部 ID nm7000010，C1 的 starring 与 cast） | AC14 |
| `test_task9_impact.py` | 改写 B0Lg 快照；IMDb 文件不在容器（unavailable）；`year.table:*` 规则与规则版本 `t6r1`；测试登记的任务 7 规则版本 `t7x9`（只改片长容差 5 → 2）生成第二个 cv，两个血缘版本 `lv0001`、`lv0002` 的 diff | AC15 |
| `test_task9_runs.py` | 同输入运行两次（reused）；批大小 5、assertions 第 2 批提交前注入故障后续跑，与清空 task9 后一次跑完的内容摘要相同；另一连接持锁；verify 注入失败后重跑；DAG 七个步骤共用一条运行、另一个 DAG run 全部 reused | AC16 |
| `test_task9_dag.py` | DAG 10 个节点与触发规则、重试 2 次、参数默认值；按 DAG 顺序执行的成功与 reused、上游 cv 缺失、verify 失败后重跑、非测试环境拒绝故障注入、before_commit 故障后续跑 | AC16 |
| `test_task9_report.py` | 世界加 Mv1 只有发售日期证据的上界（年份缺失）；导出 summary.json、examples.md、report.md、quality_issues.csv 与 checksums.json | AC17 |
