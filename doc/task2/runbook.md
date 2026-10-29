# 任务 2 运行手册（ASIN 采集清单与产品页面爬虫）

环境准备、口令与 Compose 用法同 [任务 1 runbook](../task1/runbook.md) 第 1 节。所有命令的工作目录为项目根目录。第 1–5 节是清单（第一部分）：只读任务 1 的数据库结果，不读 `movies.txt.gz`，不发网络请求。第 6 节是爬虫（第二部分）：新增 Compose 服务 `crawler`（profile `crawl`）与依赖 `scrapling[fetchers]==0.5`。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

只跑任务 2：

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_urls.py tests/integration/test_task2_manifest.py tests/integration/test_task2_failures.py tests/integration/test_task2_dag.py
```

集成测试连接独立的 `etl_test` 库；会话开始时重建 `public` 与 `task2` 两个 schema。

## 2. 端到端验证（真实 scheduler，写 etl_test）

先切到端到端配置，再用任务 1 的固定样本 DAG 在 `etl_test` 里产生一个样本抽取：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
export COMPOSE_FILE=compose.yaml:compose.e2e.yaml
FX='"source_gzip_path":"/data/fixtures/task1_sample.txt.gz","source_manifest_path":"/data/fixtures/task1_sample.manifest.json"'
python3 scripts/run_dag_e2e.py --run-id t2pre_task1_sample --conf "{\"scope\":\"sample\",\"sample_max_records\":100,\"batch_rows\":3,$FX}"
docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT extraction_id, scope FROM extraction WHERE status = 'complete'"
```

把上一步得到的样本抽取 ID 记为 `S`，然后触发清单 DAG：

```bash
S=<样本抽取 ID>
D=amazon_products_task2_manifest
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_ok --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true}"
# 同输入重跑：构建状态 reused，清单不变
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_rerun --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true}"
# 未允许样本：check_extraction 失败且不重试，下游 upstream_failed，run_gate 失败
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_no_allow --conf "{\"extraction_id\":\"$S\"}"
# 暂时错误：build_manifest 第 1 次连接失败，第 2 次成功
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_transient --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true,\"fault_injection\":\"transient_once:/opt/etl/outputs/e2e/t2_marker\"}"
# 提交前故障：build_manifest 失败且不重试，审计为 failed
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_before_commit --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true,\"fault_injection\":\"before_commit\"}"
```

`transient_once` 的标记文件存在时不再注入；重复演示前先删除 `outputs/e2e/t2_marker`。故障注入只在 `ETL_ENV=test` 下有效。演示结束回到正常配置：

```bash
unset COMPOSE_FILE
docker compose up -d
```

## 3. 正式构建

2026-10-28 已按此执行（`t2_full_1` 成功，约 45 s；`t2_full_2_rerun` 复用），结果见 [checklist](checklist.md) AC09。先确认 Airflow 已回到正常配置（写 `etl_movies`），且没有任务 1 的 DAG 正在运行：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_products_task2_manifest --run-id t2_full_1 --conf '{"extraction_id":"f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8"}' --timeout 3600
```

同参数重新触发只复用，不改清单；运行期间防止宿主休眠（如 `caffeinate -s`）。

## 4. 结果位置

- 数据库：`task2.manifest_build`（构建定义）、`task2.manifest_run`（每次执行的审计）、`task2.product_manifest`（清单）。查询必须带 `extraction_id`，例如：

  ```bash
  docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT status, count(*) FROM task2.product_manifest WHERE extraction_id = '<抽取 ID>' GROUP BY status"
  ```

- 文件（`outputs/` 被忽略，不入库）：`outputs/task2/<extraction_id>/<build_id>/<run_key>/` 下的 `product_manifest.csv`、`summary.json`、`report.md`；抽取或构建未知时报告落在 `outputs/task2/_failures/<run_key>/`。每次失败另写 `outputs/task2/_failures/<run_key>/<任务>-try<N>.json`。端到端运行的输出在 `outputs/e2e/task2/`。

## 5. 失败与恢复

- **确定性错误**（抽取不存在/未完成/样本未允许、来源冲突 `ManifestConflict`、数量核对不符、注入的提交前故障）：任务失败且不重试；查明原因后重新触发。
- **暂时错误**（数据库断连、同一构建正被占用 `ManifestBusy`）：自动重试，最多 2 次。
- **中途失败**：构建在单个事务内提交，失败后清单没有部分行；重新触发即可。进程被杀时审计停在 `running`，下一次构建取得锁后把它登记为 `failed`。
- **重跑**：同一抽取、同一模板版本只复用（`reused`），不新增也不修改清单行，爬虫写入的状态不受影响。
- **导出失败**：清单数据已提交且有效；修复输出目录后重新触发，会走复用路径并重新导出。

## 6. 爬虫（第二部分）

代码 `src/etl_task2/crawl/crawler.py`，表结构 `sql/102_task2_crawl.sql`（首次运行自动应用；正式库 2026-10-28 已应用）。规则见 [spec](spec.md) R16–R31，设计见 [design](design.md)“爬虫”一节。出口在 `.env` 配置：`CRAWL_PROXY_URLS`（逗号分隔，多出口）优先，其次 `CRAWL_PROXY_FILE`、`CRAWL_PROXY_URL`，都没有则直连。

测试（不访问外网，写 `etl_test`）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_crawl.py tests/integration/test_task2_crawl.py
```

期望：`24 passed`。

小测试（50 个 ASIN，前台运行，结束打印 JSON 汇总）：

```bash
docker compose --profile crawl run --rm crawler python -m etl_task2.crawl.crawler --limit 50
```

全量（后台运行；2026-10-28 至 29 实际使用的参数，15 个出口；中断后重跑同一命令即从未处理的 ASIN 续接）：

```bash
docker compose --profile crawl run -d --name etl-task2-crawl crawler python -m etl_task2.crawl.crawler --concurrency 40 --rate 0.4 --timeout 15 --log-every 2000
```

```bash
docker logs -f etl-task2-crawl
```

```bash
docker stop -t 60 etl-task2-crawl && docker rm etl-task2-crawl
```

`docker stop` 发送 SIGTERM：收尾在途请求后退出，运行记为 `interrupted`，未开始的 ASIN 保持 `not_collected`。长时间运行时防止宿主休眠（如另开终端运行 `caffeinate -is`，并接电源、不合盖）。

补爬被拦截或超时的行（建议隔几个小时，等出口 IP 的惩罚恢复）：

```bash
docker compose --profile crawl run -d --name etl-task2-crawl crawler python -m etl_task2.crawl.crawler --concurrency 40 --rate 0.4 --timeout 15 --log-every 2000 --retry-status access_restricted --retry-status timeout
```

只读核对（状态之和、成功证据、尝试次数、全部快照哈希；约 1 分钟；`--sample 500` 只抽查 500 个快照）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task2.crawl.check
```

期望：退出码 0，`status_sum_equals_rows: true`，其余计数项均为 0。

状态与原因分布（只读）：

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT status, left(status_reason, 40) AS reason, count(*) FROM task2.product_manifest WHERE extraction_id = 'f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8' GROUP BY 1, 2 ORDER BY 3 DESC"
```

常用参数：

| 参数 | 默认 | 作用 |
|---|---|---|
| `--concurrency` | 4 | worker 数；建议约为出口数 × 3 |
| `--rate` / `--burst` | 1 / 3 | 每个出口每秒请求数与突发；全量用 0.4 |
| `--global-rate` | 无 | 所有出口合计上限 |
| `--max-attempts` | 1 | 每个 ASIN 每次运行的最多尝试次数 |
| `--max-block-ratio` / `--window` | 0.5 / 50 | 出口窗口拦截率上限与窗口大小 |
| `--ban-streak` | 10 | 连续被拦截多少次立即减速 |
| `--cooldown` / `--max-cooldown` | 30 / 600 | 减速时的冷却秒数，连续超标翻倍 |
| `--retry-status` | 无 | 重新采集 `access_restricted`、`timeout` 或 `parse_failed` 行，可重复 |
| `--order` / `--jitter` | shuffle / 1.0 | 取行顺序与请求前随机等待秒数 |
| `--no-session` / `--session-max-requests` / `--session-reset-blocks` | 关 / 300 / 3 | cookie 会话开关与换新条件 |
| `--deadline` / `--limit` | 无 | 运行分钟数上限、本次最多处理的 ASIN 数 |
| `--log-every` | 500 | 每完成多少个 ASIN 打印一行进度 |

快照在 `outputs/task2/snapshots/<ASIN 末两位>/<ASIN>.html.gz`（不入库）。

### 6.1 多出口：mihomo 多端口

每个本地端口固定走机场的一个节点（一个出口 IP），爬虫按端口分别限速、统计拦截率。mihomo 用 Homebrew 装在宿主机（`brew install mihomo`，2026-10-28 安装的是 1.19.33）；配置脚本 `scripts/crawl_mihomo.py` 只用标准库，配置与订阅缓存写在 `outputs/task2/mihomo/`（不入库）。

1. 在 `.env` 填写 `CRAWL_SUBSCRIPTION_URL`（机场订阅链接），生成基础配置：

```bash
python3 scripts/crawl_mihomo.py init
```

2. 另开一个终端启动 mihomo，爬取期间保持运行：

```bash
mihomo -d outputs/task2/mihomo
```

3. 读取节点、测延迟（美国节点优先，排除“剩余流量”“订阅”等假节点），每个节点开一个端口（7801 起）并热加载；最后一行打印 `CRAWL_PROXY_URLS=...`，粘贴到 `.env`：

```bash
python3 scripts/crawl_mihomo.py ports --max 15
```

4. 逐个端口查询出口 IP：

```bash
python3 scripts/crawl_mihomo.py check
```

期望（需 mihomo 在运行；2026-10-28 核对）：每个端口一个不同的 IP，最后一行 `共 15 个端口，15 个不同的出口 IP`。mihomo 未运行时每个端口显示 `失败：URLError`。

注意：
- Shadowrocket 开启时，mihomo 连接节点的流量可能再被它转发一次，机场流量按两倍计（未验证）。
- 流量（按快照大小推算，未直接测量）：商品页约 230 KB/个，checkpoint 页约 70 KB/个，全量第一遍约 35–50 GB；跑一晚约 25–30 GB。出口数量只影响速度，不影响总流量。
- 实测参数、拦截规律与已修复的句柄泄漏见 [design](design.md)“爬虫”一节；进度行停止更新而容器仍在运行时，先查句柄数：

```bash
docker exec etl-task2-crawl sh -c 'ls /proc/1/fd | wc -l'
```

期望：远小于 65536（2026-10-29 修复后运行时为 40–60）。
