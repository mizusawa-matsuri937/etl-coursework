# 任务 2 规格（ASIN 采集清单与产品页面爬虫）

状态：第一部分（采集清单，R01–R15、AC00–AC10）2026-10-28 编写并完成。第二部分（爬虫，R16–R31、AC11–AC18）2026-10-29 补写：用户 2026-10-28 因时间紧改为由 Claude 实现、用户复核，代码先于本节规则完成，本节按实际实现与验证结果记录。同日按用户要求补充跳转链与页面模板证据、接入 Airflow（R32–R34、AC19–AC21）。设计见 [design](design.md)，执行顺序见 [tasks](tasks.md)，完成情况见 [checklist](checklist.md)。

## 目标

为任务 1 全量抽取中的全部不同 ASIN 建立一份可重复生成、可核对、可由爬虫回写状态的产品采集清单，每个 ASIN 一行并带确定的候选链接，初始状态均为未采集。

第二部分：用限速、多出口、可续跑的爬虫请求清单中的产品页面，为每个已处理的 ASIN 写回互斥状态和证据（快照、哈希、逐次请求日志）；在效率优先的前提下尽快产出可供后续任务使用的页面，未处理的保持未采集。

## 范围

- 包含：清单表与约束、从任务 1 抽取生成清单、候选链接构造、来源统计与评论时间、幂等与失败恢复、Airflow 编排、核对与导出、在正式库完成一次全量构建。
- 第二部分包含：从清单取待采集行、令牌桶限速与自适应调速、多出口（代理）轮换、cookie 会话、有上限的重试、状态判定（含 HTTP 200 验证页不算成功）、快照与哈希、逐次请求日志、跳转链与页面模板证据、运行审计、断点续跑与补爬、只读核对与模板回填、mihomo 多端口配置脚本、由 Airflow DAG 调用爬虫。
- 不包含：从页面解析电影字段（任务 3 起）；浏览器渲染（Chromium）；破解验证码或自动点击 checkpoint 表单；第三方或离线替代数据；robots.txt 处理（见假设 H09）。任务 3 及以后的电影分类、实体合并与补全不在本任务。
- 约束：不修改任务 1 的 spec、表结构、数据与测试；沿用 Python + PostgreSQL + Airflow + Docker Compose 现有栈；只读任务 1 的数据库结果，不再读取 `movies.txt.gz`。爬虫不改 R01–R15 定义的清单列语义与六种状态；订阅链接、代理口令只放 `.env`。
- 依据：[作业说明](../../../作业/数据存储与管理_ETL作业说明.docx) 的第三部分（为全部产品建立采集清单、每个产品均须有处理状态；受站点限制时应自行调整爬虫）、任务 2（清单字段与状态；断点续跑、缓存、限速、有上限的重试；HTTP 200 验证页不得记成功）、第十部分（互斥最终状态之和等于不同 ASIN 总数）；任务 1 的 [spec](../task1/spec.md) R22 observed 口径与 [交接文档](../handoffs/2026-10-26-task1-to-task2.md)。

## 规则

“作业要求”来自任务书，“用户选择”来自 2026-10-28 的对话，“工程默认”是本轮实现基线。R01–R15 为第一部分（清单），R16–R34 为第二部分（爬虫）。

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
- R16 [作业要求及用户选择]：爬虫只处理一个抽取的清单，默认取最近一次全量清单构建的抽取；只取 `not_collected` 行，以及用 `--retry-status` 显式指定的 `access_restricted`、`timeout`、`parse_failed` 行；`original_url` 为空的行不请求。
- R17 [作业要求]：爬虫只更新清单的爬虫列（R07 中的采集字段与 `status_reason`），不改构建列；最终跳转地址写 `final_url`，不覆盖 `original_url`。
- R18 [作业要求]：`success` 只给 HTTP 200、含产品标题元素（`id="productTitle"` 或 `data-automation-id="title"`），且最终地址或页面含目标 ASIN 的响应；有标题但 ASIN 对不上记 `parse_failed`，原因 `asin_mismatch`。
- R19 [作业要求]：跳转到 `/checkpoint/` 的页面、验证码页、HTTP 403/429/5xx 记 `access_restricted`，原因分别为 `checkpoint_redirect`、`captcha`、`http_<码>`；HTTP 200 的这类页面同样不算成功。
- R20 [作业要求]：HTTP 404，或 HTTP 200 含“Page Not Found”类文字且无产品标题，记 `not_found`；其他非 200 或无产品标题的 200 页面记 `parse_failed`；请求异常（超时、连接、TLS、代理错误）记 `timeout`，原因保留异常类型与消息。
- R21 [作业要求]：每次请求在 `task2.fetch_attempt` 记一行：出口标签、请求与最终 URL、HTTP 状态、判定与原因、响应字节数、哈希、开始时间与耗时；`attempt_no` 在同一 ASIN 内跨运行累计，清单 `attempt_count` 等于该 ASIN 的日志行数。
- R22 [作业要求]：每个已处理 ASIN 保存最后一次响应体的 gzip 快照（`outputs/task2/snapshots/<ASIN 末两位>/<ASIN>.html.gz`，先写临时文件再改名），`content_hash` 为未压缩原始字节的 SHA-256；被拦截的页面同样保存作证据，没有响应体时快照与哈希为空。
- R23 [作业要求]：一个 ASIN 的尝试日志与清单更新在同一事务提交；中断后未处理的 ASIN 保持原状态，重跑同一命令只处理剩余行，已处理行不再请求；同一抽取同一时间只允许一个爬虫运行。
- R24 [作业要求及用户选择]：重试有上限：每个 ASIN 每次运行最多 `--max-attempts` 次（默认 1，用户选择“checkpoint 直接算拦截”），被拦截或出错时换出口重试；补爬须显式指定 `--retry-status`。
- R25 [作业要求]：限速：每个出口一个令牌桶（`--rate` 每秒、`--burst` 突发），可另设全局桶 `--global-rate`；每次请求前随机等待 0–`--jitter` 秒。
- R26 [工程默认]：自适应调速：出口的窗口拦截率超过 `--max-block-ratio`，或连续被拦截 `--ban-streak` 次，即速率减半并冷却（`--cooldown` 起连续翻倍，上限 `--max-cooldown`）；拦截率低于上限的 80% 时逐步提速，不超过 `--rate`；全部出口冷却时等待，不消耗 ASIN。
- R27 [用户选择]：出口来自 `CRAWL_PROXY_URLS`（逗号分隔，用户选用 mihomo 每个端口一个机场节点），其次 `CRAWL_PROXY_FILE`、`CRAWL_PROXY_URL`，都没有则直连；日志与数据库只记 `host:port` 标签，不记账号口令；订阅链接只放 `.env`。
- R28 [作业要求及工程默认]：缓存：`success`、`not_found`、`parse_failed` 行默认不再请求；快照留在本地作为原始层，后续解析从快照读取。不另设 HTTP 缓存。
- R29 [用户选择]：效率优先：不点击 checkpoint 表单、不破解验证码，直接记 `access_restricted`；不要求爬完全部 ASIN，未处理的保持 `not_collected`，统计分母始终为清单全部行数。
- R30 [工程默认]：每次运行在 `task2.crawl_run` 记一行（参数、起止时间、状态、各判定计数）；收到 SIGTERM/SIGINT 或到达 `--deadline` 时收尾在途请求后退出，状态记 `interrupted`。
- R31 [工程默认]：爬虫表结构为迁移 `102`、`103`，放在 `task2` schema；测试不访问外网（注入假请求、本地 HTTP 服务），只写 `etl_test`；任务 1 与第一部分测试保持通过。
- R32 [作业要求]：每次有响应的请求在尝试日志中记录完整跳转链（按顺序的每一跳状态码、地址与 `Location`，最终页面不在其中），没有跳转记空数组；请求异常没有响应则为空。迁移 `103` 之前的尝试没有记录跳转链，保持为空，只能以最终地址与请求地址是否不同判断发生过跳转。
- R33 [作业要求]：每次有响应的请求记录页面模板：`dp_product_title`、`automation_title`（两种产品页标题）、`checkpoint`、`captcha`、`not_found`、`service_error`、`other`；按模板统计作为页面模板变化的证据。`103` 之前的尝试由快照回填，只回填有快照的尝试，回填可重复运行。
- R34 [作业要求]：爬虫由 Airflow DAG `amazon_products_task2_crawl` 调用：检查清单 → 爬取 → 核对 → 收尾报告 → 门禁。手动触发，每次以 `limit` 与 `deadline_minutes` 限定规模，重跑从未处理的行续接；确定性错误不重试，暂时错误（数据库断连、爬虫锁被占用）最多重试 2 次；离线假请求只允许在 `ETL_ENV=test` 下使用。

## AC测试

以下为验收定义，实际完成情况只记入 checklist。AC00 是计划文件检查，AC01–AC10 为第一部分实施验收，AC11–AC21 为第二部分（爬虫）验收。爬虫测试用注入的假请求或本地 HTTP 服务，不访问外网。

固定样本复用任务 1 的 10 条夹具（`tests/fixtures/`，预期见任务 1 spec 的“固定样本”）：observed 产品 6 个（`AAAAAAAAAA`–`FFFFFFFFFF`），valid 产品 4 个；`CCCCCCCCCC`、`DDDDDDDDDD` 只出现在隔离记录中；记录 7 缺少产品字段。每个案例独立初始化测试库；正式全量核对不使用样本数值。

| AC | Rule | Input | Expected result |
|---|---|---|---|
| AC00 | 计划文件规范 | 作业任务 2 原文、2026-10-28 用户选择、CLAUDE.md 规范。 | spec 五个板块顺序正确；R01–R34 均有任务与 AC；spec 与 checklist 的 AC 编号一一对应；未提前勾选实施验收。 |
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
| AC11 | R18, R19, R20 | 8 个合成响应：含目标 ASIN 的产品页；跳到 `/checkpoint/` 的 200 页；含 `validateCaptcha` 的 200 页；503；429；404 “Page Not Found”；无标题的 200 页；标题存在但 ASIN 为另一值的页。 | 依次判为 `success`/`product_page`、`access_restricted`/`checkpoint_redirect`、`access_restricted`/`captcha`、`access_restricted`/`http_503`、`access_restricted`/`http_429`、`not_found`/`http_404`、`parse_failed`/`no_product_title`、`parse_failed`/`asin_mismatch`。 |
| AC12 | R25, R26 | 假时钟下：令牌桶连取与补充；单出口窗口 70% 拦截两次后再全成功；30% 分散拦截；低速出口 60 s 内只有 11 次响应；连续 4 次拦截；全部出口冷却时取出口；停止信号已设时取出口。 | 桶空时需等 0.5 s 且不超过 burst；速率 1→0.5→0.25、冷却 60→120 s，恢复后 0.375；30% 不罚；按时间评估并提速到 0.2；立即冷却并轮换到另一出口；等到冷却结束才返回；抛出停止异常。 |
| AC13 | R16, R17, R21, R22, R24 | 固定样本清单 6 个 ASIN，假请求依次返回：产品页；先 checkpoint 后产品页；两次 checkpoint；404；两次超时；首页；`max_attempts=2`。 | 状态/尝试次数为 A success/1、B success/2、C access_restricted/2、D not_found/1、E timeout/2、F parse_failed/1；B 两次用不同出口；共 9 行尝试日志；成功行快照解压后的 SHA-256 等于 `content_hash`；C 的 200 checkpoint 页保留快照、原因 `checkpoint_redirect`；E 无快照。 |
| AC14 | R23, R24, R30 | 同一样本：先 `limit=2` 再无限制重跑，再跑一次；用 `--retry-status access_restricted` 补爬 C；0.3 s 截止；出口冷却 1 小时时 0.5 s 截止；乱序加抖动分两次跑。 | 续跑分别处理 2、4、0 个，总尝试仍为 9；C 变为 success、尝试 3 次；截止时运行为 `interrupted`，未处理行保持 `not_collected`，冷却等待不阻塞退出；乱序两次合计覆盖 6 行各一次。 |
| AC15 | R22, R27 | 本地 HTTP 服务：真实 Scrapling 请求产品页与 404；带会话请求两次回显 cookie，连续 2 次拦截后再请求。 | 产品页 200、判为 success，404 页状态 404；前两次 cookie 相同，换会话后 cookie 不同，共建 2 个会话，旧会话已关闭。 |
| AC16 | R27 | `CRAWL_PROXY_URLS` 含账号口令和重复地址；只设 `CRAWL_PROXY_URL`；都不设。检查仓库是否含订阅链接或 `outputs/`。 | 标签为 `host:port`、重复加 `#2`、不含口令；依次退回单代理与 `direct`；仓库没有 `.env`、订阅链接与 `outputs/` 文件。 |
| AC17 | R16–R24, R28, R29 | 正式库 `etl_movies`、抽取 `f1e8b845…`，在用户要求停止时的实际爬取结果，用只读核对脚本检查。 | 状态之和等于 253,059；`success` 行证据齐全；已处理行尝试次数大于 0、未采集行为 0，且与日志行数一致；成功行最终地址都含目标 ASIN；全部快照存在且哈希一致；实际数量如实报告。 |
| AC18 | R31 | `schema_migration` 版本、全部测试、`git status`。 | 版本为 `001`、`101`、`102`、`103`；全部测试通过（任务 1 与第一部分不减少）；爬虫代码与文档不含口令或订阅链接。 |
| AC19 | R32, R33 | 本地 HTTP 服务两跳跳转（301 → 302 → 产品页）；7 种合成响应判模板；固定样本假请求（A 带一次 301）写回后，清空模板再从快照回填两次。 | 跳转链依次为两跳的状态、地址与 `Location`，无跳转为空；模板依次判对；A 的日志记下该跳、模板 `dp_product_title`，checkpoint、404、首页分别为 `checkpoint`、`not_found`、`other`，超时为空；回填只处理 5 个有快照的尝试且结果相同，第二次候选为 0。 |
| AC20 | R34 | DAG 定义；在测试库按 DAG 顺序以离线假请求运行成功、重跑、补爬、`limit=2`；在正式环境配置下请求离线假请求；篡改一个快照后核对；真实 scheduler 经 `compose.e2e.yaml` 触发成功、重跑与不存在的抽取。 | 5 个任务依次相连，收尾与门禁 `all_done`，每个任务重试 2 次；成功运行写回 6 行、报告含模板、跳转与状态前后分布且合计相等，重跑处理 0 个、补爬 1 个、限定时处理 2 个；正式配置下检查步骤确定性失败、报告在 `_failures`；篡改后核对失败、门禁失败；真实运行前两次 success，第三次检查失败只尝试 1 次、下游 upstream_failed、DAG failed。 |
| AC21 | R32, R33 | 正式库应用迁移 `103` 后运行模板回填与只读核对，统计成功页面的模板与最终域名。 | 回填全部有快照的尝试、无缺失快照；核对退出码 0 且模板计数之和等于已处理 ASIN 数；`103` 之前的跳转链如实为空；模板分布与跨站点跳转数如实报告。 |

## 假设

| 编号 | 当前选择或假设 | 状态与理由 | 相关规则 |
|---|---|---|---|
| H01 | 纳入全部 observed ASIN；每个 ASIN 一条 `/dp/` 链接；清单存 PostgreSQL 并由 Airflow 生成；状态固定六种 | 用户 2026-10-28 已确认 | R02、R03、R05、R08、R13、R15 |
| H02 | 爬虫原定由用户自行完成；2026-10-28 用户因时间紧改为由 Claude（Claude Code）实现、用户复核，并要求效率优先、不追求成功率 | 用户已确认；AI 辅助的用途与验证方式见 design“爬虫”一节 | R16–R31 |
| H03 | 目标域名为 `www.amazon.com`（作业与 SNAP 页面的示范形式） | 工程默认；页面能否访问、是否重定向待爬虫阶段验证 | R05 |
| H04 | 清单主键为 `(extraction_id, asin)`，复用任务 1 的栈、配置与工具函数 | 工程默认；理由见 design | R01、R12、R15 |
| H05 | 全部 ASIN 符合 R06 格式；全量构建耗时为分钟级 | 规划时对 `distinct_products.csv` 只读检查无不合规值；2026-10-28 正式构建实测 `url_issue` 0、整次约 45 s，见 checklist AC09 | R06、R13 |
| H06 | 出口用 mihomo 多端口（每端口一个机场节点，15 个）、cookie 会话、每出口 0.4/s；checkpoint 直接记拦截，被拦截行留待补爬 | 用户 2026-10-28 已确认；参数来自实测，见 design | R24–R27、R29 |
| H07 | 爬虫既可在 `crawler` 容器以命令行运行（长时间全量），也可由 Airflow DAG 按批调用（每次 `limit`/`deadline_minutes`）；2026-10-28 至 29 的全量爬取是命令行运行的，DAG 在正式库尚未真实请求过 | 2026-10-29 已接入 DAG（R34）；真实网络请求的 DAG 运行需 mihomo 在运行，待用户决定是否执行 | R30、R34 |
| H08 | 103 之后的请求记录完整跳转链与页面模板；已有 29,144 次请求没有跳转链（只能比较最终地址与请求地址），模板已从快照回填 | 2026-10-29 已补（R32、R33）；旧请求的跳转链无法补回 | R32、R33 |
| H09 | 爬虫不读取 robots.txt；是否满足作业“在允许访问的范围内采集” | 待用户确认 | R16 |
| H10 | Shadowrocket 开启时机场流量可能被计两次；跑一晚约用 25–30 GB 流量 | 未验证，按快照大小推算 | R27 |
| H11 | 日本、新加坡出口有 167 个成功页面被跳转到 `amazon.co.jp`（160）或 `amazon.sg`（7），另有 1 个 `amazon.sg` 的 404；ASIN 一致，按 R18 仍记 `success`，未改状态 | 用户 2026-10-29 决定：保持 `success`，只计入成功采集数；因语言不同，不进入后续任务的数据样本 | R18、R32 |
