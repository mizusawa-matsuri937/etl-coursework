# 任务 2 规格（第一部分：ASIN 采集清单）

状态：第一轮计划，2026-10-28 编写；尚未实施。本轮只覆盖任务 2 的第一部分——提取全部不同 ASIN、生成产品采集清单并构造候选链接。第二部分（爬虫）由用户自行完成，其规则与 AC 届时在本文件续接编号追加。设计见 [design](design.md)，执行顺序见 [tasks](tasks.md)，完成情况见 [checklist](checklist.md)。

## 目标

为任务 1 全量抽取中的全部不同 ASIN 建立一份可重复生成、可核对、可由爬虫回写状态的产品采集清单，每个 ASIN 一行并带确定的候选链接，初始状态均为未采集。

## 范围

- 包含：清单表与约束、从任务 1 抽取生成清单、候选链接构造、来源统计与评论时间、幂等与失败恢复、Airflow 编排、核对与导出、在正式库完成一次全量构建。
- 不包含（爬虫部分，由用户完成）：发起任何网络请求、robots.txt 处理、请求间隔与并发、缓存、重试、页面快照保存、成功/验证页/错误页判定、页面解析、重定向与模板变化证据。也不包含任务 3 及以后的电影分类、实体合并与补全。
- 约束：不修改任务 1 的 spec、表结构、数据与测试；沿用 Python + PostgreSQL + Airflow + Docker Compose 现有栈；只读任务 1 的数据库结果，不再读取 `movies.txt.gz`。
- 依据：[作业说明](../../../作业/数据存储与管理_ETL作业说明.docx) 的第三部分（为全部产品建立采集清单、每个产品均须有处理状态）、任务 2（清单字段与状态）、第十部分（互斥最终状态之和等于不同 ASIN 总数）；任务 1 的 [spec](../task1/spec.md) R22 observed 口径与 [交接文档](../handoffs/2026-10-26-task1-to-task2.md)。

## 规则

“作业要求”来自任务书，“用户选择”来自 2026-10-28 的对话，“工程默认”是本轮实现基线。爬虫阶段的规则从 R16 起续接。

- R01 [作业要求及工程默认]：清单输入限定为任务 1 的一个抽取，所有读取都带 `extraction_id` 条件。正式构建只接受 `scope=full`、`status=complete`、读到 EOF 且 gzip 已验证的抽取；样本抽取只能在测试库以显式参数构建，构建记录标明 scope。
- R02 [作业要求及用户选择]：清单纳入该抽取 observed 口径（任务 1 R22）下的全部不同 ASIN，含只出现在隔离记录中的 ASIN。每个 ASIN 恰好一行，行数等于同一抽取的 observed 产品数。
- R03 [用户选择]：每行记录 `in_valid_records`，表示该 ASIN 是否出现在有效记录中；为真的行数等于同一抽取的 valid 产品数。只见于隔离记录的 ASIN 同样列为待采集，不另行剔除。
- R04 [工程默认]：ASIN 原样保存，不改大小写、不去空白、不补位或截断。ASIN 是产品标识，不是电影 ID，不作为后续 movie_id。
- R05 [用户选择]：每个 ASIN 只构造一条候选链接，模板为 `https://www.amazon.com/dp/<ASIN>`。模板文本与模板版本随构建记录；同一 ASIN、同一模板版本的结果确定不变。
- R06 [工程默认]：只有完整匹配 `^[0-9A-Z]{10}$` 的 ASIN 才生成链接。不匹配者仍保留在清单中，`original_url` 为 NULL 并记录 `url_issue`，不猜测修正。
- R07 [作业要求]：清单至少包含 ASIN、`original_url`、`final_url`、`status`、`http_status`、`attempt_count`、`fetched_at`、`snapshot_path`、`content_hash`。本阶段只填写 ASIN、链接与来源字段；采集字段为 NULL，尝试次数为 0。
- R08 [作业要求及用户选择]：`status` 只取 `success`、`not_found`、`access_restricted`、`timeout`、`parse_failed`、`not_collected` 六个互斥值。新建行一律为 `not_collected`；各状态行数之和等于清单总行数。
- R09 [作业要求]：`status=success` 时 `final_url`、`http_status`、`fetched_at`、`snapshot_path`、`content_hash` 必须非空，由数据库约束保证，不能用空记录冒充成功。页面是否为真正的产品页由爬虫阶段的规则判定。
- R10 [作业要求]：采集时间与评论时间分开保存。`fetched_at` 只由爬虫写入；清单另存该 ASIN 有效记录中的最早、最晚评论时间（UTC），没有有效时间时为 NULL，两者不互相填补。
- R11 [作业要求及工程默认]：每行保留来源定位：记录总数、有效数、隔离数、首末记录序号，可回溯到任务 1 的源记录；这些值与任务 1 产品汇总的同名列一致。
- R12 [作业要求]：同一抽取、同一模板版本重跑不新增也不修改清单行，只新增一条 `reused` 审计。已有行的采集字段永不被构建重置或覆盖；同键来源字段不一致时报错停止，不改任何行。
- R13 [作业要求及工程默认]：一次构建在单个事务内提交，失败时清单不出现部分结果，并留下失败审计。Airflow 实际执行“检查抽取 → 构建 → 核对 → 导出 → 收尾与门禁”；确定性错误不重试，暂时错误最多重试 2 次，收尾成功不掩盖上游失败。
- R14 [作业要求及工程默认]：每次构建导出清单 CSV 与 `summary.json`，记录行数、状态分布、`in_valid_records` 分布、`url_issue` 数和文件哈希。导出不含评论正文，导出文件不提交到仓库。
- R15 [工程默认]：清单放在业务库 `etl_movies` 的独立 schema `task2`；测试与端到端运行只写 `etl_test`。不修改任务 1 的表、规则与数据，任务 1 测试保持通过。

## AC测试

以下为验收定义，实际完成情况只记入 checklist。AC00 是计划文件检查，AC01–AC10 为实施验收。

固定样本复用任务 1 的 10 条夹具（`tests/fixtures/`，预期见任务 1 spec 的“固定样本”）：observed 产品 6 个（`AAAAAAAAAA`–`FFFFFFFFFF`），valid 产品 4 个；`CCCCCCCCCC`、`DDDDDDDDDD` 只出现在隔离记录中；记录 7 缺少产品字段。每个案例独立初始化测试库；正式全量核对不使用样本数值。

| AC | Rule | Input | Expected result |
|---|---|---|---|
| AC00 | 计划文件规范 | 作业任务 2 原文、2026-10-28 用户选择、CLAUDE.md 规范。 | spec 五个板块顺序正确；R01–R15 均有任务与 AC；爬虫内容明确排除；未提前勾选实施验收。 |
| AC01 | R01 | 不存在的抽取 ID；`status=failed` 的抽取；未读到 EOF 或未通过 gzip 验证的全量抽取；未显式允许的样本抽取。 | 均在构建前失败并给出原因；清单不新增任何行；失败审计可查询。 |
| AC02 | R02, R03, R04, R11 | 固定样本（整份夹具按 `scope=full` 装载）装入测试库后构建清单。 | 清单 6 行；`in_valid_records` 为真 4 行、为假 2 行（`CCCCCCCCCC`、`DDDDDDDDDD`）；记录 7 不产生行；ASIN 原样；来源字段与 `sql/task1_distinct_products.sql` 的结果逐列相等。 |
| AC03 | R05, R06 | `B000000743`、`0001489305`，以及人工构造的小写值、含空格值、9 位值、含 `/` 的值，各处理两次。 | 前两者得到 `https://www.amazon.com/dp/<ASIN>`；其余 `original_url` 为 NULL、`url_issue` 非空、ASIN 不变；两次结果完全相同。 |
| AC04 | R07, R08, R09 | 新建清单后，用 SQL 分别写入 `status='done'`、`attempt_count=-1`、`status='success'` 但 `snapshot_path` 为 NULL。 | 新行全部为 `not_collected`、采集字段为 NULL、尝试次数 0；三种非法写入均被约束拒绝。 |
| AC05 | R10 | 固定样本构建的清单。 | `AAAAAAAAAA` 最早与最晚评论时间均为 `1970-01-01T00:00:00Z`；`BBBBBBBBBB`（唯一有效记录时间非法）、`CCCCCCCCCC`、`DDDDDDDDDD` 为 NULL；所有行 `fetched_at` 为 NULL。 |
| AC06 | R12 | 连续构建两次；用 SQL 模拟爬虫把一行改为 `timeout`、尝试 3 次后再构建；篡改一行 `records_total` 后再构建。 | 第二次行数不变并新增 `reused` 审计；被模拟回写的行仍为 `timeout`/3；来源冲突时构建报错，任何行均未被修改。 |
| AC07 | R13 | 写入清单中途注入故障；构建时模拟暂时数据库断连；在 `compose.e2e.yaml` 下由真实 scheduler 触发成功与失败运行。 | 故障后清单无部分行、审计为 failed；暂时错误在第 2 次尝试成功、确定性错误只尝试 1 次；成功运行各任务按依赖完成，失败运行的门禁失败、DAG 状态为 failed。 |
| AC08 | R14 | 固定样本构建后导出。 | CSV 6 行，含 R07 全部字段与来源字段；`summary.json` 的行数、状态分布、`in_valid_records` 分布、`url_issue` 数和文件哈希与库中一致；导出不含评论正文。 |
| AC09 | R01, R02, R03, R08, R11, R14 | 正式库 `etl_movies`、任务 1 全量抽取 `f1e8b845…`，经 Airflow 正式构建。 | 清单 253,059 行；`in_valid_records` 为真 252,603、为假 456；全部 `not_collected`，状态分布之和为 253,059；ASIN 集合与来源字段和 `distinct_products.csv` 逐行一致；`url_issue` 数如实报告。 |
| AC10 | R15 | 实现、SQL、测试、runbook 与 README。 | task2 schema 独立；任务 1 表与测试未改且全部通过；文档中的命令可执行；无导出数据、口令或个人路径进入仓库。 |

## 假设

| 编号 | 当前选择或假设 | 状态与理由 | 相关规则 |
|---|---|---|---|
| H01 | 纳入全部 observed ASIN；每个 ASIN 一条 `/dp/` 链接；清单存 PostgreSQL 并由 Airflow 生成；状态固定六种 | 用户 2026-10-28 已确认 | R02、R03、R05、R08、R13、R15 |
| H02 | 爬虫（robots、限速、缓存、重试、快照、成功判定）由用户自行完成，规则在本文件续接 R16 起追加 | 用户已确认；本轮不规划其细节 | 范围、R09 |
| H03 | 目标域名为 `www.amazon.com`（作业与 SNAP 页面的示范形式） | 工程默认；页面能否访问、是否重定向待爬虫阶段验证 | R05 |
| H04 | 清单主键为 `(extraction_id, asin)`，复用任务 1 的栈、配置与工具函数 | 工程默认；理由见 design | R01、R12、R15 |
| H05 | 全部 ASIN 符合 R06 格式；全量构建耗时为分钟级 | 规划时对 `distinct_products.csv` 只读检查无不合规值；2026-10-28 正式构建实测 `url_issue` 0、整次约 45 s，见 checklist AC09 | R06、R13 |
