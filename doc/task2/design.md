# 任务 2 设计思路（ASIN 采集清单与产品页面爬虫）

状态：第一部分（清单）2026-10-28 编写并实施；第二部分（爬虫）见文末“爬虫”一节，2026-10-29 按已实现代码补写。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明如何实现与为什么这样选。

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

## 爬虫（第二部分，R16–R31）

用户 2026-10-28 把爬虫改为由 Claude（Claude Code）实现、用户复核，并明确“效率优先、不追求成功率”。代码先于规则完成，本节按实际实现记录。AI 辅助说明（作业要求记录）：代码、参数实验与本节文字由 Claude 生成；验证方式是单元/集成测试（假请求、本地 HTTP 服务）、正式库只读核对脚本 `etl_task2.crawl.check`，以及用户按 checklist 命令复核（截至 2026-10-29 尚未进行）。

### 思路

站点的限制是主要约束，带宽和 CPU 不是。所以设计重点是“每个出口 IP 慢慢地、持续地请求”，靠多个出口叠加吞吐量，而不是让单个 IP 加速。每个 ASIN 只请求 `/dp/<ASIN>` 一次（默认 `--max-attempts 1`）：被拦截就记 `access_restricted`，保留证据，先把全部 ASIN 过一遍，再用 `--retry-status` 补爬。清单本身就是任务队列和进度表（R16、R23）：取 `status='not_collected'` 的行，处理完一行就在同一事务里写日志并更新清单，所以中断后重跑同一命令即可续接，不需要另外的检查点文件。

### 组件

| 组件 | 职责 |
|---|---|
| `sql/102_task2_crawl.sql` | `task2.crawl_run`（每次运行一行）、`task2.fetch_attempt`（每次请求一行，外键到清单）；登记迁移 `102` |
| `src/etl_task2/crawl/crawler.py` | 全部爬虫逻辑与命令行：令牌桶、出口池与自适应调速、出口配置、`classify`、Scrapling 请求与会话、快照、写回、`crawl()` 主循环 |
| `src/etl_task2/crawl/check.py` | 只读核对：状态之和、成功证据、尝试次数与日志一致、快照哈希（AC17） |
| `scripts/crawl_mihomo.py` | 宿主机运行，仅标准库：用 `.env` 的订阅链接生成 mihomo 配置，每个节点一个本地端口（7801 起），热加载并打印 `CRAWL_PROXY_URLS` |
| `compose.yaml` 的 `crawler` 服务 | profile `crawl`；挂载 `src`、`sql`（只读）与 `outputs`；`host.docker.internal` 指向宿主以连接 mihomo；`nofile` 上限 65536 |

为了便于快速复核，逻辑集中在一个文件里；请求函数可以注入（`fetch` 参数），测试用假请求，不访问外网（R31）。

### 数据流

1. **启动**：应用迁移 `102`（已应用则跳过）；默认取最近一次全量清单构建的抽取；取会话级 advisory lock `(etl_task2_crawl, extraction_id)`，拿不到就报错退出（R23）；插入 `crawl_run(status=running)`。
2. **生产者**：按 keyset 分批读取，每批 1000 行，条件为 `status = ANY(待处理状态) AND original_url IS NOT NULL`（R16）。默认 `--order shuffle`，按 `md5(asin)` 排序，打散 ASIN 前缀，避免连续请求同一类产品；放入有界队列（容量为并发数 × 4）。
3. **worker**（`--concurrency` 个）：从出口池取一个有令牌的出口（R25、R26）→ 随机抖动 → 请求，外层再套一层 `timeout + 10` 秒的 `asyncio.wait_for` → `classify` 判定（R18–R20）→ 把结果报告给出口池和会话 → 被拦截或出错且还有尝试次数时换出口重试（R24）。
4. **落盘与写回**：最后一次响应体 gzip 后原子写入快照，`content_hash` 用未压缩字节计算（R22）；一个事务内批量插入本 ASIN 的 `fetch_attempt` 行，并更新清单爬虫列：`attempt_count` 累加，`fetched_at` 取最后一次尝试时间（R17、R21、R23）。
5. **结束**：正常结束记 `succeeded`；收到停止信号或到达截止时间记 `interrupted`；异常记 `failed` 并保留原因。在 `crawl_run` 写入各判定计数，释放锁（R30）。

### 状态判定的取舍

判定只看最终 URL、HTTP 状态和少量正则，不解析 DOM，以保证速度（R18–R20）。按以下顺序判断：

1. `/checkpoint/` 跳转、验证码、403/429/5xx：先判，确保 HTTP 200 的拦截页不会因为页面里带导航就被当成产品页。
2. 404 或“找不到页面”的文字：判 `not_found`。
3. 产品标题选择器：有两种，旧模板 `#productTitle` 和新模板 `data-automation-id="title"`。
4. ASIN 对应：最终 URL 或页面中要出现目标 ASIN。

已知的代价：正则可能把罕见的新模板误判为 `parse_failed`。这类行会保留快照，可以复查，也可以用 `--retry-status parse_failed` 重爬。截至 2026-10-29 只有 62 行 `parse_failed`。

### 限速与自适应（R25、R26）

- **令牌桶**：每个出口一个桶（`TokenBucket`）。出口池选择“已过冷却、令牌最早可用”的出口；全部出口都在冷却时就等待，不让 worker 空耗 ASIN。
- **评估时机**：每个出口维护最近 `--window`（50）次响应的拦截窗口。窗口满了评估一次；样本已有 10 次且距上次评估超过 60 秒时也评估一次，这样低速出口不会几分钟才评估一次。
- **减速**：拦截率超过上限，或连续被拦截 `--ban-streak` 次，速率减半（下限 0.1/s），冷却 `30 s × 2^(连续超标次数-1)`，上限 600 s。
- **提速**：拦截率低于上限的 80% 时，速率乘 1.5（至少加 `--rate` 的 10%），最高恢复到 `--rate`。
- **超时**：网络超时不算拦截。同一出口连续 5 次超时则短暂冷却。

### 出口与会话（R27）

- **mihomo 多端口**：mihomo 跑在宿主机。每个机场节点建一个只含它自己的 select 组（按精确节点名过滤），再为每个组开一个 `127.0.0.1` 端口。容器通过 `http://host.docker.internal:780x` 连接这些端口。节点延迟要用提供者接口 `/providers/proxies/airport/<节点>/healthcheck` 测，全局 `/proxies/<名>/delay` 查不到提供者里的节点。
- **cookie 会话**：`SessionFetcher` 为每个出口维持一个 Scrapling `FetcherSession`（curl_cffi，模拟 Chrome）。新会话先访问首页拿 cookie。满 300 个请求，或连续被拦截 3 次，就换新会话。没有 cookie 的新访客更容易被送到 checkpoint，这是从实测得出的判断（见下）。
- **旧会话关闭**：换下来的会话没有在途请求时立即关闭，否则由它最后一个请求结束时关闭。预热被超时取消时也会关闭刚建好的会话。
- **安全**：日志和 `fetch_attempt.exit_label` 只记 `host:port`。订阅链接只在 `.env`，mihomo 配置写在被忽略的 `outputs/task2/mihomo/`。

### 异常与恢复

| 情况 | 处理 |
|---|---|
| 被拦截（checkpoint、验证码、403/429/5xx） | 记 `access_restricted` 并保留快照；出口按 R26 减速冷却；之后用 `--retry-status access_restricted` 补爬 |
| 网络异常、超时 | 记 `timeout`，原因保留 curl 错误；无快照；可用 `--retry-status timeout` 补爬 |
| `docker stop`（SIGTERM）、Ctrl+C、`--deadline` | 设置停止事件：生产者不再取行，排队的 ASIN 不请求，在途请求收尾后写回，运行记 `interrupted`。截止时间用 `loop.call_later` 触发，在冷却中等待的 worker 也能及时退出 |
| 进程被强制杀死 | 已提交的 ASIN 不受影响，在途 ASIN 未提交、仍为原状态；`crawl_run` 停在 `running`（不影响清单，计数以清单为准）；锁随连接断开释放 |
| 两个爬虫同时运行 | 第二个拿不到 advisory lock，报错退出 |

**2026-10-29 发现并修复：文件句柄泄漏。**
- **现象**：全量运行 66 分钟后卡住约 6 小时，进程仍在但没有任何请求。
- **原因**：旧会话换下后从未关闭，容器内打开的句柄达到默认上限 1024（其中 529 个连接处于 CLOSE_WAIT），curl 随即停住，既不报错也不退出。
- **修复**：按上一节的规则关闭旧会话，并把容器 `nofile` 上限提高到 65536。修复后运行 15 分钟，句柄数稳定在 40–60。
- **待办**：没有加“长时间无进展就报警或退出”的看门狗，长时间运行时要看日志的进度行。

### 已验证事实与默认参数

以下区分实测与默认值。实测都在正式库，2026-10-28 至 29：

| 场景 | 吞吐量 | 拦截率 | 结论 |
|---|---|---|---|
| 单出口、无会话 | — | 稳态约 30%；持续 2–3/s 会在几分钟内被验证码封锁，恢复以小时计 | 单 IP 不可行 |
| 10 出口、会话、1/s，5 分钟 | 约 510 个/分钟 | 2.2% | 会话有效 |
| 10 出口、1.5/s，10 分钟 | 前 2 分钟约 660 个/分钟，之后多数出口被压低 | — | 不可持续 |
| 15 出口、1/s，30 分钟 | 平均 227 个/分钟 | — | 被罚过的 IP 约 94% 被拦截；新 IP 跑 600–900 次后也转入高拦截 |
| 15 出口、0.4/s、并发 40（全量） | 起始约 290 个/分钟；修复泄漏后重启约 340 个/分钟 | 重启后约 2% | 采用此配置 |

- **默认参数**（代码默认，不是实测最优）：并发 4、每出口 1/s、burst 3、窗口 50、拦截上限 50%、冷却 30–600 s、会话 300 个请求、抖动 1 s。
- **全量实际参数**：`--concurrency 40 --rate 0.4 --timeout 15`。
- **按快照大小估算**（未直接测量）：商品页压缩后约 230 KB，checkpoint 页约 70 KB；全量第一遍约 35–50 GB 机场流量。
- **用户叫停时的结果**：处理 29,126 个 ASIN；success 22,321、access_restricted 4,152（验证码 3,310、checkpoint 730、5xx 112）、not_found 2,378、timeout 213、parse_failed 62；not_collected 223,933；快照 28,913 个，共 4.7 GB。

### 与作业要求的差距（如实记录）

- **Airflow**：爬虫还没有由 Airflow 调用（spec H07）。
- **重定向与模板变化**：没有保存完整跳转链（H08）。
- **robots.txt**：爬虫不读取 robots.txt（H09）。
- **覆盖率**：只采到约 11.5% 的 ASIN，其余如实保持 `not_collected`；成功率的分母仍为 253,059。
