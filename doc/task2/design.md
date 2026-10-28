# 任务 2 设计思路（第一部分：ASIN 采集清单）

状态：第一轮设计，2026-10-28 编写，尚未实施。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明如何实现与为什么这样选。爬虫部分由用户另行设计，本文只定义它与清单之间的接口约定。

## 整体思路

采集清单本质上是“每个 ASIN 一行”的产品维表，再附上爬虫回写的处理状态。它完全可以从任务 1 已装载的数据推导出来，所以本阶段不读原始 gzip，也不发网络请求：用一条以 `extraction_id` 限定的聚合查询得到全部 observed ASIN 及其来源统计，由 Python 构造候选链接，在单个事务内写入清单表。

清单的列按“谁负责写”分成两组：构建拥有的列（ASIN、链接、来源统计、评论时间）只在首次插入时写入；爬虫拥有的列（状态、HTTP 状态、尝试次数、快照等）构建永不触碰。这样重跑构建不会冲掉爬虫进度，爬虫也无需关心清单怎么来的（R07、R12）。

由 Airflow 编排检查、构建、核对、导出和门禁，保持与任务 1 相同的失败语义，满足作业“爬虫与复杂匹配由 ETL 作业调用”的要求（R13）。

## 输入依据与规划时快照

- 任务依据：[作业说明](../../../作业/数据存储与管理_ETL作业说明.docx) 第三部分、任务 2、第十部分“数量与完整性”。
- 上游数据：业务库 `etl_movies` 中任务 1 全量抽取 `f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`。其 `summary.json` 记录 `distinct_products_observed=253,059`、`distinct_products_valid=252,603`。库里另有样本抽取 `39019c6f…`，按 R01 不得用于正式构建。
- 规划时只读检查（2026-10-28，对 `outputs/task1/f1e8b845…/full_run_2_rerun/distinct_products.csv`）：253,059 行；211,293 个为 `B`＋9 位大写字母数字，41,766 个为 9 位数字＋数字或 `X`；无小写、空白、大小写重复；456 个 `records_valid=0`。这只是规划参考，正式数字以 T2-03 的构建结果为准。

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/101_task2_schema.sql` | 创建 schema `task2` 与三张表、约束、索引；在任务 1 的 `schema_migration` 登记版本 |
| `sql/task2_manifest_source.sql` | 以 `extraction_id` 为参数，聚合每个 observed ASIN 的来源统计与有效记录评论时间 |
| `src/etl_task2/urls.py` | 纯函数：校验 ASIN 格式、按模板生成链接或返回 `url_issue`；模板常量与版本号 |
| `src/etl_task2/manifest.py` | 抽取前置检查、构建（比对、插入、审计）、核对、导出 |
| `src/etl_task2/pipeline.py` | 供 DAG 调用的步骤函数、失败记录、收尾与门禁判断 |
| `dags/amazon_products_task2_manifest.py` | Airflow DAG，手动触发 |

复用任务 1 的工具，不复制实现：`etl_task1.config.load_settings`（连接与输出目录配置）、`etl_task1.db.connect`、`etl_task1.db.advisory_key`（构建锁）、`etl_task1.reporting.write_atomic` / `file_digest` / `utc_now`、`etl_task1.pipeline.is_transient`（暂时错误判定）。`run_step`、`record_failure`、`gate_decision` 的写法在 `etl_task2.pipeline` 中仿照，若可直接复用则直接导入。schema 应用另设 `apply_task2_schema`，不改动任务 1 的 `apply_schema`。

## 数据模型

全部表在 schema `task2`，外键指向任务 1 的 `public.extraction`，不在任务 1 的表上加列或约束（R15）。

**`task2.manifest_build`**：一次“抽取 × 模板版本 × 规则版本”的构建定义。

- `build_id`：`sha256(extraction_id | url_template_version | rule_version)`，同输入同规则得到同一 ID，用于判断复用。
- `extraction_id`（FK）、`scope`（复制自抽取，便于报告区分样本与全量）、`url_template`、`url_template_version`、`rule_version`、`asin_count`、`created_at`。

**`task2.manifest_run`**：每次执行的审计，与任务 1 的 `etl_run` 同义。

- `run_id`、`build_id`（FK）、`dag_id`、`airflow_run_id`、`status`（`running`/`succeeded`/`failed`/`reused`）、`inserted_rows`、`started_at`、`finished_at`、`failure_reason`。
- 实施说明（2026-10-28，T2-01）：构建记录与 `running` 审计先单独提交，主事务失败后另行登记 `failed`，因此失败审计不随主事务回滚；抽取前置检查失败发生在此之前，不建构建记录，由 T2-02 的失败文件与 Airflow 日志记录。来源行用服务端游标每次取 10,000 行，生成链接后 `COPY` 进临时表。

**`task2.product_manifest`**：采集清单，主键 `(extraction_id, asin)`。

| 列 | 所有者 | 说明 |
|---|---|---|
| `extraction_id`、`asin` | 构建 | 主键；ASIN 原样（R04） |
| `original_url`、`url_issue`、`url_template_version`、`first_build_id` | 构建 | 候选链接或不能生成的原因（R05、R06） |
| `in_valid_records` | 构建 | R03 |
| `records_total`、`records_valid`、`records_quarantined`、`first_record_no`、`last_record_no` | 构建 | 来源定位（R11），口径同 `sql/task1_distinct_products.sql` |
| `earliest_review_time`、`latest_review_time` | 构建 | 有效记录 `review_time_utc` 的最小/最大值，可为 NULL（R10） |
| `status` | 爬虫 | 默认 `not_collected`（R08） |
| `final_url`、`http_status`、`attempt_count`、`fetched_at`、`snapshot_path`、`content_hash`、`status_reason`、`updated_at` | 爬虫 | 本阶段为 NULL 或 0（R07） |

约束：

- `status IN ('success','not_found','access_restricted','timeout','parse_failed','not_collected')`。
- `attempt_count >= 0`；`http_status` 为空或在 100–599。
- `status <> 'success' OR (final_url, http_status, fetched_at, snapshot_path, content_hash 全部非空)`（R09）。
- `(original_url IS NULL) = (url_issue IS NOT NULL)`。
- `records_total = records_valid + records_quarantined`，`in_valid_records = (records_valid > 0)`。
- 索引 `(extraction_id, status)`，供爬虫按状态取待采集行。

为什么主键带 `extraction_id`：所有查询须限定抽取版本（项目约定）；若将来任务 1 规则变化产生新抽取，可以并存一份新清单而不覆盖旧清单的采集结果。代价是爬虫读写时必须带 `extraction_id`，这写入接口约定。

## 数据流

1. **检查抽取**（R01）：读 `public.extraction`，要求存在、`status=complete`；正式模式还要求 `scope=full`、`source_eof_reached`、`gzip_verified`；`allow_sample=true` 时允许样本，只应在测试库使用。不满足即确定性失败。
2. **构建**（单事务，R12、R13）：
   - 取 `advisory_key(build_id)` 事务级锁，避免两个运行同时构建。
   - 插入或复用 `manifest_build`，插入 `manifest_run(status=running)`。
   - 执行 `task2_manifest_source.sql` 得到全部 observed ASIN 的来源行，用 `COPY` 写入临时表；Python 逐行调用 `urls.build_candidate` 生成 `original_url`/`url_issue`。
   - 比对：与 `product_manifest` 中已有同键行的构建拥有列逐列比较，任何不一致抛 `ManifestConflict`，整个事务回滚。
   - `INSERT … ON CONFLICT (extraction_id, asin) DO NOTHING` 只插入缺失行；冲突子句不更新任何列，从机制上保证不碰爬虫列。
   - 插入 0 行且已有行数等于来源行数时，运行状态记为 `reused`；否则 `succeeded`。提交。
3. **核对**（R02、R03、R08）：在新的只读事务中，用 SQL 现算该抽取的 observed/valid 产品数，与清单行数、`in_valid_records` 为真行数比较；状态分布之和等于总行数；构建拥有列的约束重查一次。不一致即确定性失败。
4. **导出**（R14）：按 `asin COLLATE "C"` 排序导出 `product_manifest.csv`（`summary.json` 在收尾时写，见下）到 `outputs/task2/<extraction_id>/<build_id>/<run_key>/`，先写临时文件再原子改名，记录 SHA-256 与字节数。
5. **收尾与门禁**：`all_done` 触发，汇总上游状态写报告；任一上游失败则门禁失败，DAG 为 failed。

实施说明（2026-10-28，T2-02）：DAG 为 `check_extraction → build_manifest → verify_manifest → export_manifest → finalize_report → run_gate`。核对用 `sql/task2_manifest_checks.sql` 一次取出清单侧计数与任务 1 现算的 observed/valid 产品数；导出用 `sql/task2_manifest_export.sql` 经 `COPY … TO STDOUT` 流式写 CSV，再逐行计数与库比对。`summary.json` 与 `report.md` 由收尾步骤统一写出，这样失败运行也有同格式的摘要；失败文件写在 `outputs/task2/_failures/<run_key>/`。门禁复用 `etl_task1.pipeline.gate_decision`。故障注入（`before_commit`、`transient_once:<标记>`）只在 `ETL_ENV=test` 下接受。

链接用 Python 而不是 SQL 正则生成：规则可单元测试（AC03），模板版本集中在一个常量里，SQL 只负责聚合。25 万次字符串拼接的开销可忽略。

## 爬虫接口约定

清单是用户爬虫的输入与状态存储，约定如下，爬虫阶段可在自己的规则中细化，但不应改变本阶段已定义的列语义：

- 读：`SELECT … FROM task2.product_manifest WHERE extraction_id = %s AND status = 'not_collected' AND original_url IS NOT NULL`，按 ASIN 排序分批。
- 写：只更新爬虫拥有的列；`original_url` 和其他构建列不改。最终跳转地址写 `final_url`，不覆盖 `original_url`。
- 置 `success` 前必须备齐 R09 的五个字段，否则数据库拒绝；验证页、错误页等是否算成功，由爬虫阶段规则决定。
- 需要每次请求的明细（逐次尝试日志、重定向链）时，在 `task2` 下另建表，以 `(extraction_id, asin)` 外键关联清单；不在清单行里堆叠历史。
- 新增状态值属于规则变化，须先修改 spec 与约束迁移，再由爬虫写入。

## 异常与恢复

| 情况 | 处理 |
|---|---|
| 抽取不存在、未完成、样本未授权 | 检查步骤确定性失败，不重试；不建构建记录 |
| 来源行与已有清单冲突 | `ManifestConflict`，事务回滚，审计记 failed，不重试 |
| 数据库暂时断连 | 由 `is_transient` 判定，Airflow 最多重试 2 次；因单事务提交，重试从头构建，不会重复插入 |
| 构建中途进程终止 | 事务未提交即无部分行；运行记录停在 `running`，下次运行把同 build 的遗留 `running` 标为 failed 后重新构建 |
| 导出目录不可写 | 导出失败，门禁失败；清单数据已提交且有效，修复后重跑会走 `reused` 路径并重新导出 |

不分批的理由：25 万行、每行约数百字节，单事务写入量在几十 MB 量级，远小于任务 1 每批的量；单事务让“要么全有要么全无”最简单，也让 R12 的比对在同一快照内完成。代价是失败后要整次重算聚合（分钟级），对这个规模可以接受。

## 资源预算

以下为预算，不是实测：

- 主要耗时是对 `review_record` 与 `review_staging` 按 `extraction_id` 的全表聚合。任务 1 交接文档记录这类聚合需要几分钟；执行前先 `ANALYZE`，并依赖已有索引 `review_staging_product_idx`。
- 清单表与索引预计几十 MB；导出 CSV 预计几十 MB，放在被忽略的 `outputs/`。
- 实测（2026-10-28，正式库 `t2_full_1`）：构建 24.5 s（含聚合与写入 253,059 行），核对 12.8 s，导出约 14 s，整次 DAG 约 45 s；`task2.product_manifest` 含索引 132 MB；导出 CSV 37,359,597 字节。预算中的“分钟级”偏保守。
- 实施修正：`manifest_run.finished_at` 原用 `now()`（事务开始时间），使审计里的耗时接近 0；已改为 `clock_timestamp()`。正式库前两条审计保留原值，耗时以 summary.json 的 `elapsed_seconds` 为准。

## 验证顺序

先用单元测试锁定链接规则（AC03），再在 `etl_test` 用任务 1 固定样本验证表约束、构建、幂等与冲突（AC02、AC04–AC06），再验证失败与 Airflow 端到端（AC01、AC07、AC08，经 `compose.e2e.yaml` 写测试库），最后在正式库全量构建（AC09）并做交付检查（AC10）。
