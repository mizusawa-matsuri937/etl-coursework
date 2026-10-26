# 任务 1 开发任务

状态：T1-00 至 T1-06 全部完成。步骤按依赖顺序执行，后续实施需按用户明确的执行范围推进。

下面源码文件相对 `src/etl_task1/`，测试文件相对 `tests/`；其他路径相对项目根目录。测试入口为 `docker compose --profile test run --rm etl-test pytest -q <测试路径>`，各任务的实际执行结果登记在对应条目下。

- [x] T1-00：整理第一轮规格、设计、任务和验收计划。
  - 文件：`doc/task1/spec.md`、`doc/task1/design.md`、`doc/task1/tasks.md`、`doc/task1/checklist.md`、`CLAUDE.md`、`README.md`、`doc/request-template.md`。
  - 检查：AC00；文档规范、已确认选型、输入/预期、目录链接和状态一致。未写业务代码或运行全量。
- [x] T1-01：准备容器环境与项目骨架。2026-10-26 完成，证据见下方检查。
  - 文件：`pyproject.toml`、`requirements.lock`、`Dockerfile`、`compose.yaml`、`.env.example`、`.dockerignore`、`.gitignore`、`docker/init-databases.sh`；`__init__.py`、`config.py`；`conftest.py`、`unit/test_config.py`。
  - 检查：`docker compose config --quiet` 和配置测试；验证 arm64 镜像、依赖版本、源只读、持久卷及数据库隔离。覆盖 AC06/AC14 的环境部分，完整验收留待后续步骤。
  - 实测（2026-10-26，Colima 0.10.3 / Docker 29.5.2 / Compose 5.6.1，4 CPU、8 GiB）：`docker compose config --quiet` 与 `--profile test` 通过；镜像在 arm64 构建成功（Python 3.12.14、Airflow 3.3.2、psycopg 3.3.5、pytest 9.1.1）；`etl-test` 运行 `tests/unit` 17 项通过；跨库访问均被拒绝；源文件只读，`src` 只读，`outputs` 可写；down/up 后业务表数据保留；api-server 健康检查返回 3.3.2；scheduler 与 dag-processor 运行。
- [x] T1-02：实现来源校验、流式解析、标识和基础转换。2026-10-26 完成。
  - 文件：`source.py`、`identity.py`、`parser.py`、`records.py`、`evidence.py`、`transform.py`、`quality.py`；对应 `unit/test_*.py`；`fixtures/README.md`、`fixtures/build_fixtures.py`、`fixtures/task1_cases.json`。
  - 检查：运行 `tests/unit`；覆盖 AC01–AC05 的核心行为及固定样本 10/6/4 分类。边界、空值、非法编码、原文回放和稳定 ID 均验证；数据库相关 AC 不提前勾选。
  - 实测（2026-10-26，工作目录：项目根目录）：`docker compose --profile test run --rm etl-test pytest -q tests/unit` 共 110 项通过；固定样本得到读取 10、有效 6、隔离 4，observed 产品 6/用户 8，valid 产品 4/用户 4，空用户 1。另用真实文件前 200,000 条做只读冒烟（非正式试跑）：约 45,000 条/秒，进程峰值 RSS 约 23 MiB；发现 1,519 条隔离，见 design 的"真实数据初步观察"。
- [x] T1-03：实现数据库约束、批次装载与恢复。2026-10-26 完成。
  - 文件：`sql/001_task1_schema.sql`、`sql/task1_integrity.sql`；`db.py`、`loader.py`、`checkpoint.py`；`integration/test_load.py`、`integration/test_resume.py`、`integration/test_evidence.py`；`doc/task1/data-dictionary.md`。
  - 检查：运行上述集成测试及完整性 SQL，覆盖 AC05、AC06、AC07、AC09；同事务检查点、同键内容冲突、重复运行、提交前后中断均在真实 PostgreSQL 验证。
  - 实测（2026-10-26，工作目录：项目根目录）：`docker compose --profile test run --rm etl-test pytest -q tests` 共 148 项通过（单元 110，集成 38，连接独立的 `etl_test` 库）。真实文件前 50,000 条装入测试库的冒烟（非正式试跑）：约 13,000 条/秒，完整性 SQL 10 项全 0，数据库表与索引合计约 143 MB。
- [x] T1-04：完成统计、报告和 Airflow 流程。2026-10-26 完成。
  - 文件：`sql/task1_counts.sql`、`dags/amazon_reviews_task1.py`、`compose.yaml`；`validation.py`、`reporting.py`、`pipeline.py`（收尾逻辑并入 pipeline，未单设 finalize.py）；`compose.e2e.yaml`、`scripts/run_dag_e2e.py`；`sql/task1_counts.sql` 之外另有质量分布与产品汇总 SQL；`integration/test_counts.py`、`integration/test_reports.py`、`integration/test_dag.py`、`integration/test_pipeline_failures.py`；`doc/task1/runbook.md`。
  - 检查：覆盖 AC08、AC10、AC11；运行对应集成测试并由实际 scheduler 触发成功/失败样本；observed 产品/用户为 6/8、valid 为 4/4；收尾不得掩盖失败。
  - 实测（2026-10-26，工作目录：项目根目录）：`docker compose --profile test run --rm etl-test pytest -q tests` 共 188 项通过（单元 111，集成 77）。经真实 scheduler（`compose.e2e.yaml`，写入 `etl_test`）触发 6 次 DAG run：固定样本成功并得到 10/6/4；同输入再跑复用；真实文件前 50,000 条样本成功且 `source_eof_reached=false`、`gzip_verified=false`；错误哈希使 `verify_source` 失败且不重试、下游 `upstream_failed`、`run_gate` 失败；gzip 截断使 `extract_load` 失败、抽取标 failed；暂时错误第 1 次失败、第 2 次重试成功（`attempt=1/2` 日志）。
- [x] T1-05：试跑资源并执行正式全量验收。2026-10-26 完成，证据见下方实测与 [任务 1 报告](task1-report.md)。
  - 文件：`metrics.py`、`integration/test_resource_limits.py`、`doc/task1/task1-report.md`、`scripts/sample_host_stats.sh`；另修改 `config.py`、`loader.py`、`checkpoint.py`、`pipeline.py`、`reporting.py`、`sql/task1_integrity.sql`、`compose.yaml`；生成 `outputs/task1/<extraction_id>/<run_id>/` 运行证据。
  - 检查：前置样本与恢复测试通过后，隔离试跑至少 100,000 条以修正预算，再经 Airflow 全量至 EOF；完成 AC07、AC12、AC13 的正式证据。数量差异未解释不标通过，保留同输入重跑核对结果。
  - 实测（2026-10-26，工作目录：项目根目录）：195 项测试通过（含真实数据 100,000 条试跑、低磁盘模拟、160 MiB 超长记录；其后新增 1 项续跑记录数测试，现为 196 项）；经真实 scheduler 完成 100 万条样本运行（写 `etl_test`）；`full_run_1` 全量写入 `etl_movies`：792 批、7,911,696 条（有效 7,849,565，隔离 62,131），产品 253,059、用户 889,176 与官方一致，评论多 12 条已查明；运行中遇到约 15 分钟停顿和任务重试，装载从检查点恢复；完整性 SQL 在全量时超时，改写后 89 秒完成（正式运行中途修正）；`full_run_2_rerun` 复用既有抽取并附基准说明，状态 `succeeded`，结果表行数不变。
- [x] T1-06：核对全部 AC 并整理复现交付。2026-10-26 完成，证据见下方实测、[任务 1 报告](task1-report.md) 第 7、8 节与 checklist AC14。
  - 文件：`README.md`、`.env.example`、`doc/task1/` 下的计划与实际字段字典、runbook、报告，以及 `doc/request-template.md`。
  - 检查：AC14；预先准备镜像后，在独立测试库按 runbook 离线复现固定样本和一次恢复；核查 AC01–AC14 的实际证据、命令与版本。纯文档修改不无理由重跑全量，不声称未创建的提交或标签。
  - 实测（2026-10-26，工作目录：项目根目录）：新增 `compose.offline.yaml`，在独立项目 `etl-repro`、容器无外网下，`docker compose --profile test run --rm etl-test pytest -q tests` 196 项通过；固定样本 10/6/4；第 2 批提交前故障后同参数续跑，结果一致；复现后 `down -v` 仅清理该项目。核对 checklist 引用的文件路径均存在，Git 跟踪文件无个人路径或口令；更正 README、CLAUDE.md、spec、design、tasks 中的过时"计划中"表述。

全部任务已完成并留有证据；用户的人工复核另行记录。设计细节维护于 design，规则和验收定义维护于 spec，实际证据维护于 checklist。
