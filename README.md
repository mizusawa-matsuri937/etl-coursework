# ETL 作业项目

当前状态：已完成任务 1 第一轮 spec、design、tasks、checklist，并于 2026-10-22 按严格规范重整至 doc/task1/，尚未开始业务实现。本轮范围是计划文件；容器环境、数据库、解析程序和全量运行都未完成。

任务 1 的目标是全量流式抽取 Amazon 评论，分批装载 PostgreSQL，保留原始证据与异常，并通过 Airflow 完成数量核对和运行报告。

## 目录结构

```text
ETL作业模板/
├── AGENTS.md                 # 协作约定与计划文件严格规范
├── README.md                 # 项目入口与真实进度
├── .gitignore
└── doc/
    ├── request-template.md   # 公共请求模板
    └── task1/                # 任务 1 第一轮计划
        ├── spec.md           # 目标、范围、规则、AC测试、假设
        ├── design.md         # 设计思路与实现取舍
        ├── tasks.md          # T1-00 至 T1-06，实施步骤未执行
        └── checklist.md      # AC00 至 AC14 的完成情况
```

这些是当前已有文件。源码、Docker 配置、SQL、DAG 和测试在计划中列出，尚未创建。项目沿用“星期三项目开发”的协作结构。

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
3. 按 [tasks](doc/task1/tasks.md) 执行；下一项仍为 **T1-01 环境与项目骨架**。
4. 在 [checklist](doc/task1/checklist.md) 登记每项 AC 的实际结果；未执行不能勾选。
5. 后续请求见 [请求模板](doc/request-template.md)。文档写法遵守 AGENTS.md，后续任务独立放在 doc/taskN/。

本阶段不实现产品采集、实体合并或评论逻辑去重。重复内容的原始评论仍分别保留；完整作业的其他任务后续单独规划。

## 输入与现有材料

- [作业说明](../作业/数据存储与管理_ETL作业说明.docx)
- [ETL 课件](../ETL.pdf)
- [原始压缩数据](../movies.txt.gz)
- [下载元数据](../movies.txt.gz.download.json) 与 [SHA-256 记录](../movies.txt.gz.sha256)

计划复用原文件并只读挂载，不把原始数据复制进项目或镜像。本轮没有重新运行全量哈希、解压或评论数量统计。已有下载记录与实施阶段实测结果须分别记录。

以上材料位于仓库外，链接用于本地查阅；原始作业、课件和全量数据不随 GitHub 仓库同步。

## 运行与验证

尚无可运行程序、Compose 配置或测试。tasks 中的命令是待实现的检查约定，不是现在可直接运行的完成品。环境完成后补充真实安装与运行命令。

当前检查仅针对计划文件的编号、引用、状态和固定样本预期；不代表功能测试、容器启动或全量验收已通过。未来运行结果保存于 `outputs/task1/<extraction_id>/<run_id>/`，不得用规划数字冒充实测结果。

## 阶段记录

| 日期 | 状态 |
|---|---|
| 2026-10-21 | 协作模板已准备；规格、方案与实现均未开始 |
| 2026-10-21 | 两轮讨论确认技术栈；任务 1 规格、任务、验收计划已形成；实现未开始 |
| 2026-10-22 | 计划移至 doc/task1/，设计独立为 design，spec 统一五个板块，tasks 精简为 6 个待实施阶段；实现未开始 |
| 2026-10-26 | 已初始化本地 Git 仓库，默认分支为 main；尚无提交或远程仓库，业务实现未开始 |
| 2026-10-26 | 初始提交归档任务 1 计划、项目规范、请求模板与忽略规则；业务实现未开始 |
| 2026-10-26 | 创建 GitHub 公开仓库并推送 main，关联 origin/main；业务实现未开始 |

本目录已初始化独立 Git 仓库，当前分支为 `main`，跟踪 `origin/main`。GitHub 公开仓库为 [mizusawa-matsuri937/etl-coursework](https://github.com/mizusawa-matsuri937/etl-coursework)。当前仅归档计划与项目规范，没有阶段标签；业务实现尚未开始。
