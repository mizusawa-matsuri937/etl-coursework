# ETL 作业项目

当前状态：任务 1 计划已完成，T1-01（容器环境）与 T1-02（解析与转换）、T1-03（数据库约束、批次装载与恢复）、T1-04（统计、报告与 Airflow 流程）于 2026-10-26 完成，经单元、集成测试和真实 scheduler 样本运行验证；T1-05 于 2026-10-26 完成资源试跑、正式全量（7,911,696 条，经真实 Airflow 写入业务库）和同输入重跑，结果见 [任务 1 报告](doc/task1/task1-report.md)；T1-06（复现交付）于同日完成：独立项目、容器无外网下按 runbook 复现固定样本与一次恢复。任务 2 第一部分（ASIN 采集清单，T2-01 至 T2-03）于 2026-10-28 完成：正式库 `task2.product_manifest` 共 253,059 行、全部 `not_collected`，见 [任务 2 checklist](doc/task2/checklist.md)；第二部分爬虫（T2-04、T2-05）于 2026-10-29 完成并由用户叫停：已处理 29,126 个 ASIN，success 22,321，其余 223,933 保持 `not_collected`；2026-10-29 补完接入 Airflow（T2-06，DAG `amazon_products_task2_crawl`）与跳转链、页面模板证据（T2-07）；2026-10-29 用户按 checklist 人工复核通过。任务 3（电影页面筛选，T3-00 至 T3-05）于 2026-10-29 在分支 `task3-movie-filter` 实现并经 Airflow 全量运行：253,059 个产品各有分类，电影 8,808、待确认 239,274，见 [任务 3 checklist](doc/task3/checklist.md)；同日用户按 checklist 复核通过，并人工抽检抽查表（口头确认已分类的基本无问题，未逐条导入标注）；之后修复已知问题，规则升为 t3r2（T3-06：电影 8,730、待确认 239,361），2026-10-29 经 PR #3 合并到 `main`。任务 4（定义并识别同一电影，T4-01 至 T4-08）于 2026-10-29 实现，同日经 PR #4 合并到 `main`：IMDb 快照 `imdb_2026-10-29` 装载进正式库，经 Airflow 全量匹配（规则 t4r2）：8,730 个电影类商品 → 8,472 部作品，多商品作品 240 部，IMDb 关联商品 4,544 个，待复核 899 个；120 对评价样本与 20 簇由付林轩审核 AI 预标注后导入，自动合并 Precision 1.000、Recall 0.487、F1 0.655，簇抽查混入 0，见 [任务 4 checklist](doc/task4/checklist.md)。任务 5（人物姓名归一与消歧）于 2026-10-30 实现，T5-01 至 T5-07 于 2026-10-30 完成并在正式库经 Airflow 全量运行：56,809 条提及 → 29,635 个人物（外部 ID 13,233、待复核 6,031），迭代 4 轮，MoviePerson 50,027 行；T5-08 于同日导入标注（AI 预标注，付林轩核查后全部采纳）：120 对（same 85、different 33、unsure 2）与 20 个人物，自动合并 Precision 1.000、Recall 0.706、F1 0.828（TP 60、FP 0、FN 25），簇抽查混入 0，因正式标签来自 AI 预标注，指标可能偏乐观，见 [任务 5 checklist](doc/task5/checklist.md)；同日经 PR #5 合并到 `main`（`1625ad9`）。任务 6（补全上映信息）在分支 `task6-release-info` 实现，2026-10-30 完成 T6-01 至 T6-07 初版实现并经独立审查（[review](doc/task6/review.md)），同日完成 T6-08 至 T6-12 全部审查修复、回归测试（全量 792 项通过）、正式库清空重跑（先冻结开发池 1,732 部，再经 Airflow 运行 `rv0001`：8,659 部作品中 `verified` 4,473、`hint_matched` 293、`cooccur_matched` 641、`page_reported` 50、`missing` 3,202，推断上界 6,978 部，二次重跑 `reused`）与付林轩审核标注导入（120 部作品：年份准确率 1.000、`tconst` 正确率 0.9692、推断上界成立率 1.000、缺失可查到率 0.000），见 [任务 6 报告](doc/task6/report.md) 与 [任务 6 checklist](doc/task6/checklist.md)。

任务 1 的目标是全量流式抽取 Amazon 评论，分批装载 PostgreSQL，保留原始证据与异常，并通过 Airflow 完成数量核对和运行报告。

## 目录结构

```text
ETL作业模板/
├── CLAUDE.md                 # 协作约定与计划文件严格规范（原 AGENTS.md）
├── README.md                 # 项目入口与真实进度
├── .gitignore
├── compose.yaml              # PostgreSQL 18 + Airflow 3.3.2（LocalExecutor）+ 测试服务
├── Dockerfile / requirements.lock / pyproject.toml
├── .env.example              # 配置模板；复制为 .env 后填写
├── docker/init-databases.sh  # 创建 etl_movies / airflow_meta / etl_test 三个隔离库
├── src/etl_task1/            # Python 包：config、source、identity、parser、transform、quality、records、evidence、db、checkpoint、loader、validation、reporting、metrics、pipeline
├── src/etl_task2/            # 任务 2 采集清单：urls（候选链接）、manifest（表结构、前置检查、构建）、pipeline（DAG 步骤）
├── src/etl_task3/            # 任务 3 电影页面筛选：extract（证据区域字段抽取）、rules（分类规则）、classify（分组、分批装载、核对、版本差异）、report（统计、导出、人工抽查）、pipeline（DAG 步骤）、check（只读核对）
├── src/etl_task4/            # 任务 4 同一电影：imdb（快照校验与装载）、normalize、editions（断言）、match（特征与三档）、candidates、cluster（簇、movie_id、映射版本）、review（人工纠正）、run/runner（运行与分步骤）、pipeline（DAG 步骤）、report、check（只读核对）、evaluate（评价工具）
├── src/etl_task5/            # 任务 5 人物归一：names（规范化与比较键）、mentions（人物提及）、external（外部 ID 两条途径）、rules/resolve/cluster（跨作品判定、合作者迭代、person_id）、review（人工纠正与待复核队列）、movie_person、run/runner/pipeline（运行与 DAG 步骤）、check、report、query、evaluate
├── src/etl_task6/            # 任务 6 补全上映信息：rules、sources、candidates（三类候选）、cooccur（共现反查）、review_years（评论年份提及）、select（择优与版本）、product_dates、reviews、bounds（推断上界）、run/runner/pipeline、report、query、check、evaluate
├── tests/unit/ integration/  # 合计 792 项（2026-10-30）：任务 1 196、任务 2 89、任务 3 51、任务 4 169、任务 5 186、任务 6 101；集成测试连接 etl_test 库
├── tests/fixtures/           # 10 条固定样本、生成脚本与预期值
├── compose.e2e.yaml          # 端到端验证覆盖：Airflow 写入 etl_test，不污染业务库
├── compose.offline.yaml      # 离线复现覆盖：容器网络 internal，无外网
├── sql/                      # 表结构、完整性核对、数量核对、质量分布、产品汇总；101_task2_schema、task2_manifest_source/checks/export（任务 2 清单）；102/103（任务 2 爬虫）；201_task3_schema、task3_input_groups、task3_classification_export（任务 3）；301_imdb_schema、302_task4_schema（任务 4）；401_task5_schema（任务 5）；501_task6_schema（任务 6）
├── dags/                     # amazon_reviews_task1.py、amazon_products_task2_manifest.py、amazon_products_task2_crawl.py、amazon_products_task3_classify.py、imdb_reference_task4_load.py、amazon_movies_task4_match.py、amazon_movies_task5_person.py、amazon_movies_task6_release.py
├── scripts/                  # run_dag_e2e.py 经真实 scheduler 触发 DAG 并等待；sample_host_stats.sh 宿主侧资源采样；gen_schema_appendix.py 从数据库生成字段字典附录；check_task2_manifest.py 逐行核对任务 2 清单与任务 1 产品汇总；crawl_mihomo.py 生成爬虫用的 mihomo 多端口配置；fetch_imdb.py 下载 IMDb 五个文件（需 --confirm，文件放仓库外）；seed_task4_e2e.py、seed_task5_e2e.py、seed_task6_e2e.py 为端到端验证准备合成数据
└── doc/
    ├── request-template.md   # 公共请求模板
    ├── handoffs/             # 任务间交接文档（新对话先读最新一份，见 CLAUDE.md）
    ├── task6/                # 任务 6（补全上映信息）：spec（R01–R39、AC00–AC23）、design、tasks、checklist、runbook、review、report；T6-01 至 T6-12 已完成并在正式库重跑、导入标注（见 tasks）
    ├── task5/                # 任务 5（人物姓名归一与消歧）：spec（R01–R47、AC00–AC17）、design、tasks、checklist 已编写并经用户审批；T5-01 至 T5-08 已实现并在正式库全量运行、导入标注（见 tasks）
    ├── task4/                # 任务 4（定义并识别同一电影）：spec（R01–R42、AC00–AC17）、design、tasks、checklist、runbook；T4-01 至 T4-08 于 2026-10-29 完成；当前规则 t4r2
    ├── task3/                # 任务 3（电影页面筛选）：spec、design、tasks、checklist、runbook；T3-00 至 T3-06 于 2026-10-29 完成；当前规则 t3r2
    ├── task2/                # 任务 2（ASIN 采集清单与产品页面爬虫）：spec、design、tasks、checklist、runbook；T2-00 至 T2-07 已完成
    └── task1/                # 任务 1 第一轮计划
        ├── spec.md           # 目标、范围、规则、AC测试、假设
        ├── design.md         # 设计思路与实现取舍
        ├── tasks.md          # T1-00 至 T1-06，全部已完成
        ├── checklist.md      # AC00 至 AC14 的完成情况
        ├── data-dictionary.md # 字段字典与 ER 图（文末附录含列类型、约束、索引，由数据库生成）
        ├── runbook.md        # 运行手册（样本、全量、恢复、离线复现）
        └── task1-report.md   # 资源试跑、全量运行与差异核对报告
```

以上为文档；源码在 `src/etl_task1/`、`src/etl_task2/`（爬虫在 `src/etl_task2/crawl/`）、`src/etl_task3/`，Airflow DAG 在 `dags/`，SQL 在 `sql/`，测试在 `tests/`，Docker 配置为 `compose*.yaml` 与 `Dockerfile`，脚本在 `scripts/`。项目沿用“星期三项目开发”的协作结构。

## 已确认选择

| 项目 | 选择 |
|---|---|
| 实现语言 | Python |
| 数据存储 | PostgreSQL，从任务 1 开始使用并延续到后续任务 |
| ETL 编排 | 课件中的 Airflow，任务 1 就接通校验、解析装载、核对与报告 |
| 部署方式 | Docker Compose，本机容器环境 |

初始版本设计为 Python 3.12、Airflow 3.3.2、PostgreSQL 18、Psycopg 3；具体小版本和镜像摘要在环境任务中验证锁定。采用单机 LocalExecutor，数据库元数据与业务数据分开。

## 阅读与执行顺序

1. 阅读 [spec](doc/task1/spec.md)，了解目标、范围、规则、AC 测试与假设。
2. 阅读 [design](doc/task1/design.md)，了解架构、模型、批次恢复和资源预算。
3. 按 [tasks](doc/task1/tasks.md) 执行；T1-00 至 T1-06 均已完成，任务 1 的剩余事项是用户人工复核（见 [任务 1 报告](doc/task1/task1-report.md) 第 8 节）。
4. 在 [checklist](doc/task1/checklist.md) 登记每项 AC 的实际结果；未执行不能勾选。
5. 后续请求见 [请求模板](doc/request-template.md)。文档写法遵守 CLAUDE.md，后续任务独立放在 doc/taskN/。

本阶段不实现产品采集、实体合并或评论逻辑去重。重复内容的原始评论仍分别保留；完整作业的其他任务后续单独规划。

## 输入与现有材料

- [作业说明](../作业/数据存储与管理_ETL作业说明.docx)
- [ETL 课件](../ETL.pdf)
- [原始压缩数据](../movies.txt.gz)
- [下载元数据](../movies.txt.gz.download.json) 与 [SHA-256 记录](../movies.txt.gz.sha256)

计划复用原文件并只读挂载，不把原始数据复制进项目或镜像。下载记录中的哈希是下载时的本地计算值；实施阶段的实测哈希与全量统计见任务 1 报告，二者分别记录。

以上材料位于仓库外，链接用于本地查阅；原始作业、课件和全量数据不随 GitHub 仓库同步。

## 运行与验证

环境准备（macOS arm64，已用 Colima 验证；`requirements.lock` 只锁定 aarch64，其他架构需重新生成；`docker compose build` 需联网一次）：

```bash
brew install colima docker docker-compose
colima start --cpu 4 --memory 8 --disk 100
cp .env.example .env   # 然后按文件内说明填写口令、密钥与 SOURCE_HOST_DIR
docker compose build
docker compose up -d   # 工作目录：项目根目录
docker compose --profile test run --rm etl-test pytest -q tests   # 单元 + 集成
```

任务 2 爬虫（2026-10-28 至 29）：依赖 `scrapling[fetchers]==0.5`（锁定于 `requirements.lock`，未装浏览器）；Compose 服务 `crawler`（profile `crawl`），出口代理 `CRAWL_PROXY_URLS` 与机场订阅 `CRAWL_SUBSCRIPTION_URL` 只写在 `.env`。运行、补爬、核对与 mihomo 多出口见 [任务 2 runbook](doc/task2/runbook.md) 第 6 节。

任务 4（2026-10-29）：IMDb 非商业数据集（五个 `.tsv.gz`，约 1.9 GB）用 `python3 scripts/fetch_imdb.py --confirm` 下载到仓库外的 `../imdb/<日期>/`（联网前需用户许可，文件不入仓库），在 `.env` 里用 `IMDB_HOST_DIR` 指向其父目录并只读挂载到容器 `/data/imdb`；装载与匹配经 Airflow DAG `imdb_reference_task4_load`、`amazon_movies_task4_match` 运行，核对用 `python -m etl_task4.check`，评价与标注见 [任务 4 runbook](doc/task4/runbook.md)。

`docker compose` 需在 `~/.docker/config.json` 中加入 `"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]`。Airflow 界面为 `http://127.0.0.1:8080`（仅本机、免登录，不得对外暴露）。测试覆盖配置、来源校验、解析、转换、证据回放、数据库约束、批次装载与恢复、统计报告与 DAG 定义。经 Airflow 运行、端到端验证与故障演示的命令见 [runbook](doc/task1/runbook.md)。

已验证范围：计划文件检查、T1-01 的容器环境，T1-02 的单元测试（固定样本 10/6/4），以及 T1-03/T1-04 的集成测试与真实 scheduler 样本运行；T1-05 的资源试跑与全量运行证据见 [任务 1 报告](doc/task1/task1-report.md)；AC14（离线复现）的证据见报告第 7 节与 `outputs/repro/`（不入库）。未来运行结果保存于 `outputs/task1/<extraction_id>/<run_id>/`，不得用规划数字冒充实测结果。

## 阶段记录

| 日期 | 状态 |
|---|---|
| 2026-10-21 | 协作模板已准备；规格、方案与实现均未开始 |
| 2026-10-21 | 两轮讨论确认技术栈；任务 1 规格、任务、验收计划已形成；实现未开始 |
| 2026-10-22 | 计划移至 doc/task1/，设计独立为 design，spec 统一五个板块，tasks 精简为 6 个待实施阶段；实现未开始 |
| 2026-10-26 | 已初始化本地 Git 仓库，默认分支为 main；尚无提交或远程仓库，业务实现未开始 |
| 2026-10-26 | 初始提交归档任务 1 计划、项目规范、请求模板与忽略规则；业务实现未开始 |
| 2026-10-26 | 创建 GitHub 公开仓库并推送 main，关联 origin/main；业务实现未开始 |
| 2026-10-26 | AGENTS.md 并入 CLAUDE.md；完成 T1-01：容器环境、依赖锁定、三库隔离与配置模块，已在 Colima 上验证；解析与装载未开始 |
| 2026-10-26 | 完成 T1-02：来源校验、流式解析、标识、转换与固定样本，110 项单元测试通过；前 200,000 条真实记录冒烟发现约 0.76% 非 UTF-8 隔离，规则未改 |
| 2026-10-26 | 完成 T1-03：表结构、约束、批次装载、检查点恢复与证据旁路；148 项测试通过；前 50,000 条真实记录装入测试库的冒烟完整性全 0 |
| 2026-10-26 | 完成 T1-04：数量核对、报告、Airflow DAG 与门禁；188 项测试通过；真实 scheduler 完成成功、错误哈希、gzip 截断、暂时错误重试等 6 次运行 |
| 2026-10-26 | 完成 T1-05：100k/100 万条试跑、正式全量 7,911,696 条（产品 253,059、用户 889,176 与官方一致，评论多 12 条已查明）、同输入重跑复用；195 项测试通过 |
| 2026-10-26 | 完成 T1-06：独立 Compose 项目、无外网复现固定样本（10/6/4）与提交前故障恢复，196 项测试通过；AC01–AC14 均有实际证据 |
| 2026-10-28 | 编写任务 2 第一部分（提取全部 ASIN、生成采集清单与候选链接）的 spec、design、tasks、checklist；实现未开始。爬虫部分由用户自行完成 |
| 2026-10-28 | 完成 T2-01：task2 清单表与约束、候选链接构造、单事务构建（复用、冲突检测、不覆盖爬虫状态）；226 项测试通过（新增 30 项）；尚未接 Airflow，未在正式库构建 |
| 2026-10-28 | 完成 T2-02：清单 DAG（检查→构建→核对→导出→收尾→门禁）、失败记录与导出；244 项测试通过；真实 scheduler 在 etl_test 完成成功、复用、样本未允许、暂时错误重试、提交前故障等 6 次运行；未在正式库构建 |
| 2026-10-28 | 完成 T2-03：经 Airflow 在正式库构建清单 253,059 行（in_valid 252,603/456，全部 not_collected，url_issue 0），与任务 1 产品汇总逐行一致；同参数重跑复用；修正审计结束时间；任务 2 第一部分结束 |
| 2026-10-28 | 任务 2 第二部分：装好爬虫环境（Scrapling 0.5、镜像重建、Compose 服务 `crawler`）；用户随后因时间紧改为由 Claude 实现、用户复核 |
| 2026-10-29 | 完成 T2-04、T2-05：爬虫（令牌桶、mihomo 15 个出口、cookie 会话、状态判定、快照与尝试日志、续跑）；修复会话句柄泄漏；用户叫停时已处理 29,126 个 ASIN（success 22,321，其余 223,933 未采集），只读核对通过；268 项测试通过；补写 spec R16–R31、AC11–AC18；尚未接入 Airflow；提交到本地分支 `task2-manifest`（`272eb11`），未推送 |
| 2026-10-29 | 完成 T2-06、T2-07：爬虫 DAG `amazon_products_task2_crawl`（真实 scheduler 在 etl_test 跑通成功、重跑与失败分支）；迁移 103 记录跳转链与页面模板，正式库从快照回填 28,913 个请求的模板（成功页两种模板 20,828 / 1,493）；285 项测试通过；提交到本地分支 `task2-manifest`，未推送 |
| 2026-10-29 | 用户按 checklist 人工复核任务 2 第二部分（AC11–AC21）全部通过；推送分支 `task2-manifest` 并开 PR #2 |
| 2026-10-29 | PR #2 合并到 `main`（`9594985`），任务 2 完成 |
| 2026-10-29 | 在分支 `task3-movie-filter` 编写任务 3（电影页面筛选）的 spec（R01–R30、AC00–AC13）、design、tasks、checklist；规划时只读探查全部 20,661 个快照 |
| 2026-10-29 | 用户同意假设按建议执行；完成 T3-01 至 T3-05：字段抽取、规则分类与证据、分批装载、DAG `amazon_products_task3_classify`（真实 scheduler 在 etl_test 跑通成功、重跑、暂时错误、失败分支）；正式库全量 `t3_full_1` 约 5.5 分钟，分类 253,059 行（电影 8,808、演唱会或其他 3,256、合集 861、剧集 835、非电影 25、待确认 239,274），重跑 reused；333 项测试通过；抽查表已导出；用户复核 checklist 全部通过、人工抽检口头确认已分类的基本无问题；提交 `939562d` |
| 2026-10-29 | T3-06：规则升为 t3r2，修复载体列表误判合集、占位署名算作演员、非视频首级影片判为非电影；正式库 `t3_full_r2_1` 只重算分类，212 个产品变化（电影 8,730、待确认 239,361）；336 项测试通过 |
| 2026-10-29 | 推送分支 `task3-movie-filter` 并开 PR #3；PR #3 合并到 `main`（`c547c27`），任务 3 完成（AC12 无逐条标注，未勾选） |
| 2026-10-29 | 在分支 `task4-movie-identity` 编写任务 4（定义并识别同一电影）的 spec（R01–R42、AC00–AC17）、design、tasks、checklist；用户确认引入 IMDb、合并范围（电影 + 合集拆分，剧集只建商品层）、人物只做姓名比较、评价样本 120 对，其余假设同意按建议执行；实现未开始 |
| 2026-10-29 | 完成 T4-01 至 T4-04：IMDb 快照下载脚本与流式装载、迁移 301/302、标准化与断言、候选与三档判定、聚类与稳定 movie_id、映射版本与人工纠正；475 项测试通过 |
| 2026-10-29 | 完成 T4-05 至 T4-07：分步骤运行与锁、报告与核对、匹配 DAG、评价工具；用户授权后下载 IMDb（1.93 GB，仓库外），经 Airflow 装载（287 秒）与全量匹配（346 秒）；开发池上把导演严格重叠的权重 3 → 4，规则升为 t4r2：8,730 个电影类商品 → 8,472 部作品、多商品作品 240 部、IMDb 关联率 52.05%、待复核率 10.30%；只读核对通过，同参数重跑为 reused；505 项测试通过；评价表与簇抽查表已导出 |
| 2026-10-29 | T4-08：AI 先出预标注草稿，标注人付林轩审核（改判 4 对）后导入 120 对与 20 簇；自动合并 P 1.000、R 0.487、F1 0.655，误合并 0，簇抽查混入 0；AC00–AC17 全部有证据 |
| 2026-10-29 | 推送分支 `task4-movie-identity` 并开 PR #4；PR #4 合并到 `main`（`df0d7af`），任务 4 完成；完善任务 5 交接文档，新建 `doc/task5/` 计划空文件 |
| 2026-10-29 | 在分支 `task5-person-identity` 编写任务 5（人物姓名归一与消歧）的 spec（R01–R41、AC00–AC15）、design、tasks、checklist；用户确认角色范围（导演、主演、演员）、主演依据（页面署名行）、无外部 ID 人物的归并方式与 IMDb 演职员只作证据（H01–H04），其余假设待审批；实现未开始 |
| 2026-10-30 | 按用户要求加强任务 5 计划中没有 IMDb 关联作品的跨作品人物匹配：作品年表反查（规划估算可为 311 个商品的 411 条署名确认外部 ID）、合作者佐证迭代、只记录的名字稀有度与发行商特征、按影响排序的待复核队列，评价增加作品年表层；规则与 AC 重新编号为 R01–R47、AC00–AC17；仍待用户审批，实现未开始 |
| 2026-10-30 | 实现任务 5 的 T5-01 至 T5-06：姓名规范化、提及、外部 ID 两条途径、归并与迭代、人物映射版本、MoviePerson、分步骤运行与 DAG `amazon_movies_task5_person`、报告与查询、只读核对、评价工具；真实 scheduler 端到端（成功、重跑、缺映射版本、暂时错误）在 etl_test 通过并已回到正常配置；全量 689 项测试通过；正式库运行（T5-07）未执行 |
| 2026-10-30 | 完成 T5-07：正式库经 Airflow 全量运行（规则 `t5r1`，人物映射版本 `pmap0001`，8 分 47 秒；前两次尝试暴露合集成员标题键重复与导演兼演员的候选矛盾两个缺陷，修复并加测试后清空 `task5` 重跑）：56,809 条提及 → 29,635 个人物（外部 ID 13,233、页面人物 10,371、待复核 6,031），迭代 4 轮，MoviePerson 50,027 行，任务 4 升级线索 335；核对通过，任务 4 表行数不变，重跑 `reused`；开发池观察后规则不调整；已导出评价表、簇抽查表、待复核队列与 AI 预标注草稿；691 项测试通过；本地提交，未推送 |
| 2026-10-30 | 完成 T5-08：导入标注（AI 预标注草稿，付林轩核查后全部采纳作为正式标注，未改判；含 2 对 unsure、8 对 AI 存疑）120 对与 20 个人物；自动合并 Precision 1.000、Recall 0.706、F1 0.828（TP 60、FP 0、FN 25、TN 33，计入 118 对），误合并 0，漏合并 25（S4 18、S5 5、S6 2，均为系统不按名字合并的同名或近似名），簇抽查混入 0，人工复核覆盖率 0%、待复核率 20.4%；`check` 通过，任务 4 表行数不变；局限：正式标签来自 AI 预标注，AI 与系统用相似证据，指标可能偏乐观；本地提交，未推送 |
| 2026-10-30 | 任务 5 收尾：新增交接文档 `doc/handoffs/2026-10-30-task5-to-task6.md`（作业有任务 6“补全上映信息”）；导入标注后用 `report export --run-key t5_final_labeled` 重新导出实例（5 组别名合并标 `same`、5 组不合并标 `different`），修正 checklist AC16 与 tasks 中导入标注前的过时表述；691 项测试通过；任务 5 待经 PR 合并到 `main` |
| 2026-10-30 | 任务 5 经 PR #5 合并到 `main`（`1625ad9`）；从 `main` 新建分支 `task6-release-info`，编写任务 6（补全上映信息）的 spec（R01–R35、AC00–AC18）、design、tasks、checklist；用户确认只做 IMDb 首次公开发行年份、最早评论时间做条件性上界、任务 5 升级线索纳入作候选、120 部作品分层 + AI 预标注（H01–H04），其余假设待审批；规划时只读探查正式库（8,659 部作品，估算有年份 4,786、无候选 3,873）；实现未开始 |
| 2026-10-30 | 按用户要求为无候选作品补充来源：只读探查评估了人物共现反查、评论正文年份、商品描述年份、标题末尾年份、任务 4 复核档候选与商品发售日期上界；计划改为加入人物共现反查（模拟：已关联商品上唯一命中 4,287 个全部正确）、被占用 `tconst` 的线索、评论年份只作消歧、商品发售日期作第二种上界，无候选估算 3,873 → 约 3,165；spec 改为 R01–R39、AC00–AC20，评价改为七层；待用户审批，实现未开始 |
| 2026-10-30 | 完成任务 6 T6-01 至 T6-07 初版实现并经独立审查（`doc/task6/review.md`）；针对审查发现的 12 项问题完成 T6-08 至 T6-11 修复（共现标题键优先序与续集编号回退、单实词别名保护、共现阶段键参数、报告 `title_types` 与 `candidate_shared_count` 统计、`FULL OUTER JOIN` 版本差异、评价七层键名修复、商品原始标题导出、全局 MD5 打乱盲测编号、非循环 AI 草稿生成）；正式库清空 `task6` 后先冻结开发池 1,732 部，再经 Airflow 全量运行 `t6_full_1`（`2a6b07ff00e54bb39fcf86234a366cbf`，77.74s）与复用验证 `t6_full_2`（`fb16afb798c046e599502af3a169dbdf`，`reused`）：8,659 部作品中 `verified` 4,473、`hint_matched` 293、`cooccur_matched` 641、`page_reported` 50、`missing` 3,202，推断上界 6,978 部；只读核对通过，任务 1–5 全部 9 张主表行数不变；全量 792 项测试通过 |
| 2026-10-30 | 完成 T6-12：付林轩审核 120 部盲测样本的非循环 AI 预标注草稿后全部采纳导入（`known` 75、`unsure` 45，标注人 `付林轩`）；年份准确率 1.000（75/75，完全一致与相差 ≤1 年均 100.0%）、`tconst` 正确率 0.9692（63/65，`imdb_t5_hint` 发现 2 例异片错配）、推断上界成立率 1.000（53/53，`media_release` 40/40、`review` 13/13）、缺失可查到率 0.000（0/40）；编写交接文档 `doc/handoffs/2026-10-30-task6-to-task7.md`；任务 6 待经 PR 合并到 `main` |

本目录已初始化独立 Git 仓库，当前分支为 `main`，跟踪 `origin/main`。GitHub 公开仓库为 [mizusawa-matsuri937/etl-coursework](https://github.com/mizusawa-matsuri937/etl-coursework)。任务 1 经 PR #1 合并到 `main`；任务 2 经 [PR #2](https://github.com/mizusawa-matsuri937/etl-coursework/pull/2) 于 2026-10-29 合并到 `main`（合并提交 `9594985`）；任务 3 经 [PR #3](https://github.com/mizusawa-matsuri937/etl-coursework/pull/3) 于 2026-10-29 合并到 `main`（合并提交 `c547c27`）。任务 4 经 [PR #4](https://github.com/mizusawa-matsuri937/etl-coursework/pull/4) 于 2026-10-29 合并到 `main`（合并提交 `df0d7af`）。任务 5 经 [PR #5](https://github.com/mizusawa-matsuri937/etl-coursework/pull/5) 于 2026-10-30 合并到 `main`（合并提交 `1625ad9`）。没有阶段标签。
