# 任务 2 AC 完成情况（ASIN 采集清单与产品页面爬虫）

状态：第一部分 AC00–AC10 于 2026-10-28 由 AI 执行并通过，同日用户按本文命令人工复核，测试全部通过。第二部分 AC11–AC18 于 2026-10-29 由 AI 执行并通过，用户尚未复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都可在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库，可随时重跑；容器启动日志之后的最后一行就是结果。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：`268 passed`（2026-10-29；第一部分复核时为 244，第二部分新增爬虫测试 24 项）。

## AC00 计划文件规范

- [x] 2026-10-28 通过（AI 文档检查）；2026-10-29 补写 R16–R31、AC11–AC18 后复查通过。

```bash
grep -n "^## " doc/task2/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task2/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task2/checklist.md | tr -d '# ')
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（spec 与 checklist 的 AC 编号一一对应）。

## AC01 抽取前置检查

- [x] 2026-10-28 通过（集成测试＋真实 scheduler 运行 `t2_no_allow`）。局限：“complete 但未验证 gzip 的全量抽取”被任务 1 约束禁止，无法构造测试。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_manifest.py tests/integration/test_task2_failures.py -k "unknown_or_unfinished or sample_extraction or bad_extraction_id"
```

期望：`6 passed`。

## AC02 固定样本清单内容

- [x] 2026-10-28 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_manifest.py -k "one_row_per_observed or match_task1"
```

期望：`2 passed`。

## AC03 候选链接构造

- [x] 2026-10-28 通过（单元测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_urls.py
```

期望：`13 passed`。

## AC04 字段、状态与成功约束

- [x] 2026-10-28 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_manifest.py -k "not_collected or constraints_reject or complete_evidence"
```

期望：`5 passed`。

## AC05 评论时间与采集时间分开

- [x] 2026-10-28 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_manifest.py -k review_times
```

期望：`1 passed`。

## AC06 重跑复用、不覆盖爬虫状态

- [x] 2026-10-28 通过（集成测试；正式库重跑见 AC09）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_manifest.py -k "rebuild or missing_rows or conflicting"
```

期望：`5 passed`。

## AC07 事务失败、重试与 Airflow 运行

- [x] 2026-10-28 通过（集成测试＋真实 scheduler 在 `etl_test` 跑 6 次）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_manifest.py tests/integration/test_task2_failures.py tests/integration/test_task2_dag.py -k "before_commit or abandoned or transient or verification or unwritable or six_tasks or finalize_and_gate or manual_trigger or parse_time"
```

期望：`10 passed`。

真实 scheduler 复核（可选；会把 Airflow 临时切到测试库，步骤见 [runbook](runbook.md) 第 2 节，结束后切回）：

```bash
docker compose exec -T postgres psql -U postgres -d airflow_meta -c "SELECT run_id, task_id, state, try_number FROM task_instance WHERE dag_id = 'amazon_products_task2_manifest' AND task_id = 'build_manifest' AND run_id IN ('t2_transient', 't2_before_commit') ORDER BY run_id"
```

期望：`t2_before_commit | failed | 1`，`t2_transient | success | 2`。

## AC08 导出文件与摘要一致

- [x] 2026-10-28 通过（集成测试；正式导出见 AC09）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_failures.py -k "exports_csv or no_review_text or export_failure"
```

期望：`3 passed`。

## AC09 正式全量清单

- [x] 2026-10-28 通过（正式库 `etl_movies`，运行 `t2_full_1` 与重跑 `t2_full_2_rerun`）。只读复核，不改数据：

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT status, in_valid_records, count(*) FROM task2.product_manifest WHERE extraction_id = 'f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8' GROUP BY 1, 2 ORDER BY 1, 2"
```

期望：两行，`not_collected | f | 456` 与 `not_collected | t | 252603`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT airflow_run_id, status, inserted_rows FROM task2.manifest_run ORDER BY started_at"
```

期望：`t2_full_1 | succeeded | 253059`，`t2_full_2_rerun | reused | 0`。

```bash
python3 scripts/check_task2_manifest.py outputs/task2/f1e8b845*/63fca8ef*/t2_full_1/product_manifest.csv outputs/task1/f1e8b845*/full_run_2_rerun/distinct_products.csv
```

期望：退出码 0；`manifest_rows` 与 `reference_rows` 均为 253059，`asin_sequence_equal: true`，`column_mismatches`、`url_issue_rows`、`url_mismatches`、`crawler_fields_nonempty`、`attempt_nonzero` 均为 0，`in_valid_true/false` 为 252603/456，`review_time_null` 为 456。

```bash
shasum -a 256 outputs/task2/f1e8b845*/63fca8ef*/t2_full_*/product_manifest.csv
```

期望：两次运行的哈希相同，均为 `3318127c…f1e5df504`。

## AC10 独立 schema 与交付

- [x] 2026-10-28 通过。任务 1 的表只在 `schema_migration` 多一行 `101`（运行前后写入计数见 `outputs/task2-run/task1-table-writes-*.txt`）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT version FROM schema_migration ORDER BY 1"
git ls-files | grep -c '^outputs/'
git grep -nE '/Users/[a-z]|PASSWORD=[A-Za-z0-9]' -- . ':!*.example'
```

期望：第一条为 `001`、`101` 两行；第二条输出 `0`；第三条无输出。再跑一次 `docker compose --profile test run --rm etl-test pytest -q tests`，期望 `244 passed`（任务 1 的 196 项仍全部通过）。2026-10-29 起第二部分已加入迁移 `102` 和 24 项测试，第一条现为三行、全部测试为 268 项，见 AC18。

## AC11 状态判定

- [x] 2026-10-29 通过（单元测试，8 个合成响应）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_crawl.py -k classify
```

期望：`8 passed`。

## AC12 令牌桶与自适应调速

- [x] 2026-10-29 通过（单元测试，假时钟）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_crawl.py -k "token_bucket or pool or ban_streak or slow_exit or scattered or window_over or acquire_stops"
```

期望：`7 passed`。

## AC13 写回、尝试日志与证据

- [x] 2026-10-29 通过（集成测试，固定样本 6 个 ASIN、假请求）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_crawl.py -k writes_statuses
```

期望：`1 passed`。

## AC14 续跑、补爬与截止

- [x] 2026-10-29 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_crawl.py -k "resume or retry_status or deadline or shuffled"
```

期望：`5 passed`。

## AC15 真实请求与 cookie 会话

- [x] 2026-10-29 通过（本地 HTTP 服务，真实 Scrapling 请求；含换会话后旧会话已关闭的断言）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_crawl.py -k "scrapling_fetch or session_fetcher"
```

期望：`2 passed`。

## AC16 出口配置与口令不入库

- [x] 2026-10-29 通过（单元测试＋仓库检查）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_crawl.py -k load_exits
```

期望：`1 passed`。

```bash
git check-ignore .env outputs/task2/mihomo/config.yaml
```

期望：输出 `.env` 和 `outputs/task2/mihomo/config.yaml` 两行（都被忽略）。

```bash
git grep --untracked -l -F "$(sed -n 's/^CRAWL_SUBSCRIPTION_URL=//p' .env)"
```

期望：无输出，退出码 1（订阅链接不在任何已跟踪或未跟踪的仓库文件里；只输出文件名，不显示链接）。

## AC17 正式库爬取结果

- [x] 2026-10-29 通过（只读核对脚本，用户要求停止后执行）。处理 29,126 个 ASIN：success 22,321、access_restricted 4,152、not_found 2,378、timeout 213、parse_failed 62；not_collected 223,933；28,913 个快照哈希全部一致。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task2.crawl.check
```

期望：退出码 0；`manifest_rows: 253059`，`status_sum_equals_rows: true`，`status` 为上面的六个数，`fetch_attempts: 29144`，`snapshots_checked: 28913`；`success_missing_evidence`、`processed_without_attempt`、`not_collected_with_attempt`、`attempt_count_mismatch`、`success_final_url_without_asin`、`snapshot_missing`、`snapshot_hash_mismatch` 均为 0。约 1 分钟。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT status, count(*) FROM task2.crawl_run GROUP BY 1 ORDER BY 1"
```

期望：`interrupted | 7`、`running | 1`、`succeeded | 2`。`running` 那行是 2026-10-28 因句柄泄漏卡住、被强制停止的运行（`28d7bcd0…`），其结果已逐 ASIN 写入清单，以清单为准。

## AC18 迁移、全部测试与交付

- [x] 2026-10-29 通过。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT version FROM schema_migration ORDER BY 1"
git grep --untracked -nE '/Users/[a-z]|PASSWORD=[A-Za-z0-9]' -- . ':!*.example'
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：第一条为 `001`、`101`、`102` 三行；第二条无输出；第三条 `268 passed`。

当前汇总：AC00–AC18 共 19 项。第一部分 AC00–AC10：AI 执行通过 11 项，用户人工复核通过 11 项（2026-10-28）。第二部分 AC11–AC18：AI 执行通过 8 项，用户尚未复核。
