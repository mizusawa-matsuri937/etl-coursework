# 交接：任务 2（产品页面采集）→ 任务 3（电影页面筛选）

日期：2026-10-29。写给接手任务 3 的人（用户本人，或协助的 agent）。

- **任务 2 状态**：全部完成，2026-10-29 经 [PR #2](https://github.com/mizusawa-matsuri937/etl-coursework/pull/2) 合并到 `main`（合并提交 `9594985`），用户已按 checklist 人工复核通过。
- **本文范围**：只写接手所需的事实；任务 2 的规则、设计与证据以 `doc/task2/` 为准。
- **补充时间**：第 2–5 节于 2026-10-29 任务 2 合并后补写。页面结构是抽样观察，不是全量统计。

## 1. 先做什么

1. 读 `CLAUDE.md`、`README.md`，再读本文。
2. 任务 3 的计划放在 `doc/task3/`。四份文件 `spec.md`、`design.md`、`tasks.md`、`checklist.md` 已建好但还是空的，按 `CLAUDE.md` 的“计划文件严格规范”编写。任务 3 不改任务 1、任务 2 的 spec 规则，需要新规则写在任务 3 的文档里。
3. 开工前从 `main` 新建任务 3 的分支，`main` 上不直接开发。
4. 用第 8 节的命令核对现状。本文的数字是写作当时的快照，以实际为准。

## 2. 任务 3 要做什么（作业原文要点）

作业原文是 `../作业/数据存储与管理_ETL作业说明.docx`，“任务3 电影页面筛选”与第十部分“数据质量与验收要求”。要点如下：

- **分类**：每个产品都要有分类，类别为电影、非电影、合集、剧集、演唱会或其他、待确认，并给出分类依据。
- **不能只看标题**：标题含 DVD 或 Blu-ray 不能就认定是电影。
- **证据不足**：保留“待确认”。
- **评论不能丢**：没有电影映射的评论仍要保留。
- **要提交的统计**：
  - 采集状态分布、电影识别数量、字段覆盖率。
  - 成功采集率的分母是全部产品数。
  - 离线快照、第三方补充、实时采集要分别统计，不能混成一个成功指标。
- **第十部分**：
  - 报告电影、非电影、待确认产品的数量。
  - 电影信息以已识别作品为分母，另报告全部产品的识别覆盖率。
  - 缺失、推断、已核实的结果分别统计。
  - 每个非空字段都要能追溯到原始来源。

后续任务（字段抽取、实体合并等）的字段定义见作业第五部分，不在任务 3 范围内，规划时注意别提前做。

## 3. 任务 3 的输入与口径

**产品总体**：清单 `task2.product_manifest` 共 253,059 个 ASIN，与任务 1 的不同产品数一致，抽取为 `f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`。按任务 2 结果和用户决定分成以下几组，2026-10-29 正式库查询核对，合计 253,059：

| 组 | 数量 | 有没有可用页面 | 用户决定或说明 |
|---|---|---|---|
| amazon.com 的经典 `#productTitle` 产品页 | **20,661** | 有 | **任务 3 只解析这一组** |
| amazon.com 的另一种模板（`automation_title`） | 1,493 | 有 | 第三轮不单独处理；在任务 3 里怎么计数，在任务 3 spec 里定 |
| 被跳到 `amazon.co.jp`（160）或 `amazon.sg`（7）的成功页 | 167 | 有，但语言不同 | 保持 `success`，只计入成功采集数，不进入后续数据样本（任务 2 spec H11） |
| `access_restricted` | 4,152 | 只有拦截页 | 没有产品信息 |
| `not_found` | 2,378 | 404 页 | 页面不存在，不能据此判为非电影 |
| `timeout` | 213 | 没有 | — |
| `parse_failed` | 62 | 页面无产品标题 | — |
| `not_collected` | 223,933 | 没有 | 未请求 |

- **没有可用页面的组**（最后五组，以及后两组被排除的页面）不能当作“非电影”。按作业要求，证据不足的保留“待确认”，具体口径在任务 3 spec 里定。
- **统计来源**：任务 2 的页面都是实时采集、保存为快照；任务 3 如果引入第三方数据，要和它分开统计。

**取输入的 SQL**（只读，返回 20,661 行）：

```sql
SELECT m.asin, m.snapshot_path, m.content_hash, m.final_url, m.fetched_at
FROM task2.product_manifest m
JOIN task2.fetch_attempt a
  ON a.extraction_id = m.extraction_id AND a.asin = m.asin AND a.attempt_no = m.attempt_count
WHERE m.extraction_id = 'f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8'
  AND m.status = 'success'
  AND m.final_url LIKE 'https://www.amazon.com/%'
  AND a.page_template = 'dp_product_title';
```

- **读快照**：快照是 gzip 压缩的原始 HTML，路径相对 `/opt/etl`，要用 `etl_task2.crawl.crawler.resolve_snapshot` 还原，读取后用 `content_hash`（未压缩字节的 SHA-256）核对。不要重新请求页面。
- **采集时间**：`fetched_at` 是采集时间，评论时间在清单的 `earliest_review_time`、`latest_review_time` 和任务 1 的表里，两者分开。

## 4. 页面里有什么（抽样观察，不是全量统计）

2026-10-29 按 `md5(asin)` 顺序，从上面 20,661 个里取 300 个快照做只读观察。

- **体积**：原始 HTML 的中位数约 0.9 MB。
- **全站导航干扰**：每个页面的导航菜单里都有 “Prime Video”“Audio CD”“DVD”“Blu-ray” 等字样，在整页里搜关键词没有区分度，要限定在面包屑、商品详情等区域里找。这一点也正是作业说不能凭 DVD、Blu-ray 判电影的原因。
- **面包屑**（`#wayfinding-breadcrumbs_feature_div`）：300 个里有 298 个有面包屑。

  | 第一级 | 数量 |
  |---|---|
  | Movies & TV | 285 |
  | CDs & Vinyl | 9 |
  | Electronics | 2 |
  | Books | 1 |
  | Grocery & Gourmet Food | 1 |
  | 无面包屑 | 2 |

  第二级大多是 “Movies & TV › Featured Categories”（279 个），区分不出电影和剧集。
- **商品详情**（`#detailBullets_feature_div`）：299 个有这一块（旧版的 `#productDetails` 表格只有 1 个）。各字段出现的次数如下，可作为分类依据的候选：

  | 字段 | 数量 |
  |---|---|
  | ASIN | 298 |
  | Studio | 230 |
  | Actors | 201 |
  | Release date | 197 |
  | Media Format | 192 |
  | Run time | 183 |
  | Number of discs | 169 |
  | MPAA rating | 159 |
  | Director | 127 |
  | Language | 77 |
  | Writers | 33 |
  | Producers | 30 |

- **代表性**：这只是 300 个的观察，规则要在任务 3 用全量和固定样本验证。

## 5. 可以用的其他数据

- **任务 1 的评论数据**（`etl_movies` 的 `public` schema）：
  - `review_record`：所有源记录，一条一行，记录定位和分类（valid / quarantined），观察到的 ASIN 在 `product_id_observed`。
  - `review_staging`：有效记录，与 `review_record` 一对一，存解析后的字段：`product_id`（即 ASIN）、`score`、`review_time_utc`、`summary_raw`、`review_text_raw` 等。
  - `quarantine_record`：隔离记录，与 `review_record` 一对一，存原始字节和隔离原因。
  - `extraction`：抽取版本，所有查询都要带 `extraction_id`。
  - 口径见 `doc/handoffs/2026-10-26-task1-to-task2.md` 和 `doc/task1/data-dictionary.md`。
- **清单里的来源字段**：`records_total`、`records_valid`、`in_valid_records`、首末记录序号、评论时间范围，可回溯到任务 1 的源记录。
- **逐次请求证据**（`task2.fetch_attempt`）：页面模板、最终地址、`redirect_chain`（迁移 `103` 之前的请求为空）、出口、耗时。

## 6. 已做的决定（截至 2026-10-29）

- **爬虫由谁实现**：Claude 实现、用户复核；效率优先，不追求成功率。被拦截的页面记 `access_restricted`。
- **不再爬取**：用户叫停全量爬取（已处理 29,126 个 ASIN，约 11.5%），也不在正式库经 DAG 再真实爬取，现有结果就是任务 2 的交付。
- **任务 3 的输入范围**：任务 3 只解析 `dp_product_title` 页面，`automation_title` 页面不单独处理。
- **其他站点的 167 个页面**：保持 `success`，只计入成功采集数，不进入后续数据样本。
- **robots.txt**：用户暂不处理（任务 2 spec H09）。

## 7. 任务 2 的结果在哪里

**数据库**：`etl_movies` 的 schema `task2`。

| 表 | 内容 |
|---|---|
| `product_manifest` | 每个 ASIN 一行：候选链接、来源统计、评论时间，以及爬虫写回的状态与证据（`status`、`final_url`、`http_status`、`attempt_count`、`fetched_at`、`snapshot_path`、`content_hash`、`status_reason`） |
| `fetch_attempt` | 每次请求一行，共 29,144 行，含 `page_template`（已回填）和 `redirect_chain` |
| `crawl_run` | 每次运行一行，共 10 行。`28d7bcd0…` 停在 `running`：那次运行因句柄泄漏卡住后被强制停止，结果已逐个 ASIN 写进清单，以清单为准 |
| `manifest_build`、`manifest_run` | 清单的构建定义与审计 |

**文件**（`outputs/` 被忽略，不在 GitHub 上）：

| 路径 | 内容 |
|---|---|
| `outputs/task2/snapshots/<ASIN 末两位>/<ASIN>.html.gz` | 快照，28,913 个，约 4.7 GB；被拦截的页面也有快照 |
| `outputs/task2/f1e8b845…/63fca8ef…/t2_full_1/` | 清单导出 `product_manifest.csv`、`summary.json`、`report.md` |
| `outputs/task2/mihomo/` | mihomo 配置与订阅缓存，含订阅信息，**不要提交或分享** |

## 8. 环境与核对命令

- **当前配置**：栈与任务 1、任务 2 相同，现在是正常配置（`ETL_ENV=prod`，写 `etl_movies`）。
- **mihomo**：已停，任务 3 不需要网络请求。
- **任务 2 只读核对**（约 1 分钟；期望退出码 0，`status` 与第 3 节一致，`snapshot_hash_mismatch: 0`）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task2.crawl.check
```

- **全部测试**（期望 `285 passed`）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

- **爬虫的运行、补爬、回填与 Airflow 用法**：见 `doc/task2/runbook.md` 第 6 节。补爬要先启动 mihomo（6.1 节），会消耗机场流量。

## 9. 踩过的坑

- **快照路径是相对路径**：快照记的是相对 `/opt/etl` 的路径，Airflow 任务的工作目录不是 `/opt/etl`，读取时一律用 `resolve_snapshot`，不要直接 `Path(snapshot_path)`。
- **快照很大**：单个页面中位数约 0.9 MB，按中位数估算，全量 20,661 个解压后约 18 GB。解析时要逐个流式处理，不要一次全读进内存。
- **测试会清空 etl_test**：集成测试会话开始时会清空 `etl_test`，测试库端到端要在测试之后从头准备（见任务 2 runbook 第 2 节、6.2 节）。
- **爬取限制**：单 IP 拦截严重；被罚过的 IP 恢复以小时计；全量用的是 15 个出口、每个出口 0.4/s。
- **句柄泄漏**：换会话时旧会话没有关闭，导致爬虫卡住约 6 小时，已修复，`nofile` 上限为 65536。没有看门狗。
- **Scrapling 的两个坑**：
  - `FetcherSession.__aenter__` 返回的客户端才能发请求。
  - 首次导入 `scrapling.core.utils._utils` 时会把 logger 重设为 INFO，要先导入再静音。
- **mihomo 测延迟**：要用 `/providers/proxies/airport/<节点>/healthcheck`。

## 10. 遗留事项

- **任务 3 的计划**：`doc/task3/` 下的四份计划都是空文件，待编写。
- **任务 2 的已知差距**（任务 2 spec H07–H09，用户已知悉）：
  - 正式库没有经 DAG 发过真实请求。
  - `103` 之前的请求没有跳转链。
  - 不读取 robots.txt。
- **可选的后续工作**：补爬被拦截的行；用浏览器渲染处理 checkpoint 页。这两项都要先改任务 2 的 spec。

## 11. 文件索引

| 需要什么 | 看哪里 |
|---|---|
| 任务 3 计划（待编写） | `doc/task3/spec.md`、`design.md`、`tasks.md`、`checklist.md` |
| 任务 2 规则与验收 | `doc/task2/spec.md`（R01–R34、AC00–AC21）、`doc/task2/checklist.md` |
| 任务 2 设计、实测参数 | `doc/task2/design.md`（文末“爬虫”一节） |
| 任务 2 运行与恢复 | `doc/task2/runbook.md` |
| 爬虫与快照读取 | `src/etl_task2/crawl/crawler.py`（`resolve_snapshot`、`detect_template`） |
| 只读核对、模板回填 | `src/etl_task2/crawl/check.py`、`backfill.py` |
| 表结构 | `sql/101_task2_schema.sql`、`sql/102_task2_crawl.sql`、`sql/103_task2_crawl_evidence.sql` |
| 任务 1 数据口径 | `doc/handoffs/2026-10-26-task1-to-task2.md`、`doc/task1/data-dictionary.md` |
| 上一份交接（清单 → 爬虫） | `doc/handoffs/2026-10-28-task2-part1-to-part2.md` |
