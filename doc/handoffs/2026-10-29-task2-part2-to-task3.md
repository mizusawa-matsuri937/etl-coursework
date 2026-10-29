# 交接：任务 2 第二部分（爬虫）→ 任务 3（电影页面筛选）

日期：2026-10-29。写给接手后续任务的人（用户本人，或协助的 agent）。任务 2 第二部分（爬虫）的代码和文档都已完成，用户已叫停全量爬取；改动已提交到本地分支 `task2-manifest`（提交 `272eb11`，未推送、未合并到 `main`）。规则、设计与证据以 `doc/task2/` 为准，这里只写接手所需的事实。

## 1. 先做什么

1. 读 `CLAUDE.md`、`README.md`，再读本文。
2. 读 `doc/task2/spec.md` 的 R16–R31、AC11–AC18，`doc/task2/design.md` 的“爬虫”一节。
3. 用第 6 节的命令核对现状。数字是写作当时的快照，以实际为准。

## 2. 完成了什么

- **决定**：用户 2026-10-28 因时间紧，把爬虫改为由 Claude 实现、用户复核，并要求效率优先、不追求成功率。
- **爬虫做法**：请求清单里的 `https://www.amazon.com/dp/<ASIN>`。
  - 限速与调速：每个出口一个令牌桶，按拦截率自适应调速。
  - 出口与会话：mihomo 15 个端口（每端口一个机场节点），每个出口维持 cookie 会话。
  - 判定与证据：判定后在同一事务里写尝试日志并更新清单。每个已处理的 ASIN 都保存 gzip 快照和 SHA-256。
  - 中断后重跑同一命令即可续接。
- **测试与核对**：
  - 测试不访问外网，共 268 项，含爬虫 24 项。
  - 只读核对脚本是 `python -m etl_task2.crawl.check`。
- **用户叫停**：2026-10-29 07:58（北京时间），最后一次运行在库里记为 `interrupted`。
- **未做**：没有接入 Airflow（T2-06）。

## 3. 结果在哪里

**数据库**：`etl_movies`，schema `task2`，抽取 `f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`。

| 表 | 内容 |
|---|---|
| `product_manifest` | 爬虫列已写回：`status`、`final_url`、`http_status`、`attempt_count`、`fetched_at`、`snapshot_path`、`content_hash`、`status_reason` |
| `fetch_attempt` | 每次请求一行，共 29,144 行：出口、URL、状态码、判定、原因、字节数、哈希、耗时 |
| `crawl_run` | 每次运行一行，共 10 行 |

`crawl_run` 里有一行 `28d7bcd0…` 停在 `running`：那次运行因句柄泄漏卡住后被强制停止，实际结果已逐个 ASIN 写进清单。

**文件**（`outputs/` 被忽略，不在 GitHub 上）：

| 路径 | 内容 |
|---|---|
| `outputs/task2/snapshots/<ASIN 末两位>/<ASIN>.html.gz` | 快照，28,913 个，约 4.7 GB；被拦截的页面也有快照 |
| `outputs/task2/mihomo/` | mihomo 配置与订阅缓存，含订阅信息，**不要提交或分享** |

## 4. 关键数字（2026-10-29）

| 状态 | 数量 | 说明 |
|---|---|---|
| success | 22,321 | 产品页，`status_reason=product_page` |
| access_restricted | 4,152 | 验证码 3,310、checkpoint 730、HTTP 503 111、HTTP 500 1 |
| not_found | 2,378 | HTTP 404 |
| timeout | 213 | 多为 TLS 连接被断开（179） |
| parse_failed | 62 | 无产品标题 |
| not_collected | 223,933 | 未请求 |

- 状态之和为 253,059，等于清单行数。
- 已处理 29,126 个 ASIN，约占 11.5%。成功率分母按作业要求用 253,059，即 8.8%。

## 5. 给任务 3 的提示

- **输入**：用 `status='success'` 的行，从 `snapshot_path` 读取快照（gzip 解压后是 HTML）。不要重新请求页面。读取时用 `content_hash` 核对快照。
- **页面模板**：有两种标题选择器，`#productTitle` 和 `data-automation-id="title"`，说明至少有两种页面模板，解析时两种都要处理。
- **非 success 的行**：`access_restricted`、`timeout`、`not_collected` 没有可用页面，任务 3 应把它们归为“待确认/无证据”，不能当作非电影。
- **补爬**：想补更多页面，命令见 `doc/task2/runbook.md` 第 6 节（`--retry-status`）。补爬需要先启动 mihomo（6.1 节），还会消耗机场流量。

## 6. 环境与核对命令

- 爬虫运行需要宿主机上的 mihomo（`mihomo -d outputs/task2/mihomo`）。用户已停掉 mihomo 和防休眠的 `caffeinate`。
- 核对（只读，约 1 分钟；期望退出码 0，各计数与第 4 节一致）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task2.crawl.check
```

- 全部测试（期望 `268 passed`）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

## 7. 踩过的坑

- **单 IP 不可行**：单 IP 稳态拦截约 30%。持续 2–3/s 几分钟内会被验证码封锁，恢复要几个小时。每个出口超过 1/s 不可持续。被罚过的 IP 30–40 分钟后仍约 94% 被拦截。全量最终用的是 15 个出口、每出口 0.4/s。
- **句柄泄漏**：换会话时旧会话没有关闭，容器句柄耗尽（默认上限 1024）后爬虫卡住，不报错也不退出，表现为 6 小时没有新请求。已修复：关闭旧会话，`nofile` 上限提高到 65536。没有看门狗，长时间运行要看日志进度行。
- **重启后的空白期**：修复后重启时，约 5 分钟没有请求才开始，原因未查明。
- **mihomo 节点延迟**：要用提供者接口 `/providers/proxies/airport/<节点>/healthcheck` 测，全局 `/proxies/<名>/delay` 查不到提供者里的节点。
- **Scrapling 的 `FetcherSession`**：`__aenter__` 返回的客户端才能发请求，会话对象本身没有 `get`。
- **Scrapling 日志**：首次导入 `scrapling.core.utils._utils` 时会把 logger 重设为 INFO，要先导入再静音。
- **流量估算**：按快照大小推算（未直接测量），跑一晚约用 25–30 GB 机场流量。Shadowrocket 开着时可能计两次（未验证）。

## 8. 遗留事项

- **提交状态**：本阶段改动已于 2026-10-29 提交到本地分支 `task2-manifest`（`272eb11`），未推送、未开 PR、未合并到 `main`。
  - 新增：`src/etl_task2/crawl/`（`crawler.py`、`check.py`）、`sql/102_task2_crawl.sql`、`scripts/crawl_mihomo.py`、两个测试文件、本交接文档。
  - 修改：`compose.yaml`、`.env.example`、`pyproject.toml`、`requirements.lock`、`README.md`、`CLAUDE.md`、`doc/task2/` 下的五份文档。
  - 推送与合并须由用户要求。
- **用户复核**：AC11–AC18 由 AI 执行通过，用户尚未按 `doc/task2/checklist.md` 复核。
- **与作业要求的差距**（spec 假设 H07–H09）：
  - 爬虫没有由 Airflow 调用（T2-06）。
  - 没有保存完整的重定向链。
  - 不读取 robots.txt。
- **可选的后续工作**：补爬被拦截的行；用浏览器渲染（Scrapling 的 Chromium 会话）处理 checkpoint 页。这类改动要先改 spec。

## 9. 文件索引

| 需要什么 | 看哪里 |
|---|---|
| 爬虫规则与验收 | `doc/task2/spec.md` R16–R31、AC11–AC18；`doc/task2/checklist.md` |
| 设计、实测参数、异常处理 | `doc/task2/design.md`“爬虫”一节 |
| 运行、补爬、核对、mihomo | `doc/task2/runbook.md` 第 6 节 |
| 爬虫代码 | `src/etl_task2/crawl/crawler.py` |
| 只读核对 | `src/etl_task2/crawl/check.py` |
| 表结构 | `sql/102_task2_crawl.sql` |
| 上一份交接（清单） | `doc/handoffs/2026-10-28-task2-part1-to-part2.md` |
