# 任务 1 报告：评论抽取与装载

状态：2026-10-26 完成资源试跑、正式全量运行和同输入重跑。样本通过、资源试跑和全量通过分别记录；本报告由 AI 辅助起草，不代表用户已逐行审阅。规则与验收定义见 [spec](spec.md)，设计见 [design](design.md)，逐项证据见 [checklist](checklist.md)，操作见 [runbook](runbook.md)。

## 1. 结论

- 输入 `movies.txt.gz`（3,321,791,660 字节）的大小与 SHA-256 与下载 manifest 一致（`4b6ee09e…cc715`，本地实测值，不是官方发布值），gzip 读到 EOF 并校验通过（`source_eof_reached=true`、`gzip_verified=true`）。
- 经真实 Airflow 全量装入业务库 `etl_movies`，抽取 ID `f1e8b845…d6d8`，792 个批次，记录 **7,911,696** 条：有效 7,849,565，隔离 62,131，删除 0。读取＝有效＋隔离；staging、quarantine、批次合计与明细逐项相等，完整性 SQL 10 项全 0。
- 与官方基准对照：产品 observed **253,059**（差 0），用户 observed **889,176**（差 0），评论 **7,911,696 对 7,911,684（多 12）**。差异已查明（见第 3 节），状态为 `explained`，实测数未被修改。
- 同输入重跑（`full_run_2_rerun`）结果行数逐表不变，仅新增一条 `reused` 的执行审计；`distinct_products.csv` 两次导出逐字节一致。

## 2. 运行证据

| 运行 | 位置（`outputs/task1/f1e8b845…/`） | 结果 |
|---|---|---|
| `full_run_1` | `full_run_1/` | 装载成功；`validate_counts` 经一次重试后成功；无基准说明，状态 `review_required`，`run_gate` 按设计失败 |
| `full_run_2_rerun` | `full_run_2_rerun/` | 复用既有抽取，附基准说明，状态 `succeeded`，`run_gate` 成功，DAG 总状态 success |

两次运行的 `report.md`、`summary.json`、`manifest.json`、`quality_issues_by_code.csv`、`distinct_products.csv`（253,059 行加表头）均在上述目录；资源采样 `full_run_1/resources.jsonl`（792 行，每批一行）与宿主侧 `outputs/full-run/host-stats-full1.csv`、重跑前后行数 `outputs/full-run/rowcounts-*-rerun.txt`。`outputs/` 不入库。

质量问题分布（全量）：`invalid_utf8` 63,501 行/62,085 条（占读取数 0.78%，与前 20 万条冒烟的 0.76% 一致，按已确认的严格 UTF-8 全部隔离）、`missing_field` 60/12、`unknown_line` 47/46、`invalid_helpfulness` 43/43、`missing_product_id` 12/12。

## 3. 与官方基准的差异

多出的 12 条来自源文件中 12 条评论的 `review/profileName` 内含空行。按空行分块时每条评论被拆成相邻两块：

- 前一块带产品与用户 ID，缺 helpfulness、score、time、summary、text 五个字段，按规则为有效并各带 5 条 `missing_field` 警告；
- 后一块以续行开头，缺产品和用户 ID，含未知行，整体隔离（`unknown_line`、`missing_product_id`）。

核对：缺产品 ID 的 12 条记录号都恰好等于某条缺 `review/text` 的有效记录的记录号加一（12 对全部对上）；observed 去重数与官方完全一致，因为 ID 只来自前一块。未删除、合并或补造任何记录，两块原文均可回放。这是对规则 R13（歧义整体隔离、不猜拆）的预期结果；若要在读取数上与官方一致，需要新的拆分规则和规则版本，本任务未做。

此外，有效口径的去重数（产品 252,603、用户 886,308）低于 observed，官方没有对应数字，只作为实测值报告。

## 4. 资源：预算、试跑、实测

单位：GiB/MiB 为二进制，MB/GB 为十进制；下表各格都能在对应文件里复算，来源见表后。时间一律指进程内单调时钟（`elapsed_s`），它在宿主休眠或虚拟机暂停时不走，所以不含暂停；墙钟时间另说明。

| 项目 | 初始预算/估算 | 100,000 条试跑 | 100 万条（经 Airflow，离线栈） | 全量实测 |
|---|---|---|---|---|
| 进程峰值 RSS | 目标 ≤ 1 GiB | 120 MB（单独运行该测试） | 235 MB（Airflow 任务进程） | 238 MB（Airflow 任务进程） |
| 速率（条/秒，累计平均） | 估算取 100k 试跑速率 | 8,152 | 7,682 | 约 2,220（含重扫；瞬时速率由约 10,200 降到约 1,500，见下） |
| 数据库增长（字节/条） | — | 3,009 | 3,031 | 总库大小 ÷ 条数 ≈ 2,890（含 2 万条样本抽取与基线）；批次 1 至 792 的增长约 2,878 |
| 数据库总大小 | 估算 22.2 GiB（由 100k 外推） | 0.29 GiB | 2.83 GiB | 21.3 GiB（22.87 GB） |
| `pg_wal` 目录 | — | 未采样 | 最大 1.06 GiB | 最大 1.25 GiB |
| 磁盘余量 | 预留 80 GiB，保留阈值 10 GiB | VM 66.6 GiB | VM 62.7 GiB | 装载期间最低：VM 磁盘 63.6 GiB，宿主 80.9 GiB，未触发保护 |
| 净装载耗时 | 估算约 970 s（16 分钟，3 次试跑的估算为 902–972 s） | 12.3 s | 130.2 s | **3,567 s ≈ 59.5 分钟**（见下），约为估算的 3.7–4 倍 |

来源：100k 试跑 `outputs/resource-trial/20261026T124416Z/trial-100k-summary.json`；100 万条 `outputs/repro/runs/trial_1m_sample/resources.json` 与 `outputs/repro/host-stats-1m-airflow.csv`；全量 `outputs/task1/f1e8b845…/full_run_1/resources.jsonl`（792 行）、`outputs/full-run/host-stats-full1.csv`。同一测试每次运行数字略有出入（100k：8,058–8,769 条/秒，估算 902–982 s），表中取一次干净的单独运行。第一次 100k 与 100 万条试跑（会话早期）的摘要文件曾被后续测试覆盖或在清理 e2e 目录时删除，已于同日重新运行并保留带时间戳的结果，早期的口头数字不再引用。

- **内存**：有界批次与流式读取使进程 RSS 稳定在约 235 MB（Airflow 任务进程自身占大头），与记录数无关，目标 1 GiB 满足。进程峰值取 `ru_maxrss` 与当前 RSS 的较大者。pytest 整套测试进程的 `ru_maxrss` 含同进程其他测试的高水位（曾达 668 MB），不能与 Airflow 任务进程比较，所以 100k 试跑的 RSS 取单独运行的值。160 MiB 超长记录的 Python 内存峰值受 `max_record_bytes`（32 MiB）约束而不随记录增大。scheduler 容器的 cgroup 峰值（约 3.0–3.1 GiB）是容器自启动以来的历史峰值，含页缓存和其他进程，不归因于单次运行。PostgreSQL 容器采样峰值约 4.2 GiB（含页缓存），Colima 为 4 CPU/8 GiB。
- **空间**：100k 试跑外推 22.2 GiB，实测 21.3 GiB，估算偏高约 4%（估算按增长口径，实测按总大小，口径略有差别）。构成：`review_staging` 堆 9.1 GiB，含 TOAST 与索引共 14 GiB；`review_record` 含索引共 6.9 GiB（三个以 SHA-256 为键的索引各约 1.65 GiB，`record_no` 唯一索引 0.78 GiB）。`pg_wal` 目录受默认 `max_wal_size`（1 GiB）约束，最大 1.25 GiB，只表示目录大小，不代表 WAL 产生总量。运行期 `pgdata` 因完整性 SQL 的临时文件曾达 27.7 GiB。
- **时间**：净装载耗时是两次尝试 `elapsed_s` 之和：2,554.6 s（第 1 次，到第 671 批）加 1,012.9 s（第 3 次，含重扫，追加 121 批）＝ 3,567.5 s。这不含宿主暂停；墙钟分别为 3,712 s 与 1,537 s（另有两次尝试之间的间隔）。重扫已提交的 671 万条记录，单调时钟约 105 s（约 6.4 万条/秒，含第一个新批），墙钟约 160 s 含暂停，这是 AC09 中"gzip 重扫耗时"的实测值。装载速率随表和索引增大持续下降（按每 50 批窗口、单调时钟计的瞬时速率：51 万条处约 10,200 条/秒，111 万条处 5,600，291 万条处 2,700，651 万条处 1,800，771 万条处 1,500）。原因推测为以 SHA-256 为主键的 B 树随机插入，索引超出 128 MiB `shared_buffers` 与缓存后读写放大；未做专项分析或调优（未改 PostgreSQL 参数）。线性外推不适用于这类随机键索引，再次全量应按约 60 分钟装载加约 2 分钟校验预留。
- **低磁盘保护**：`ETL_MIN_FREE_BYTES`（默认 10 GiB）。启动前和每批提交后检查 `output_dir` 与容器根文件系统的剩余空间，低于阈值时在已提交批次之后抛出 `LowDiskSpace`，抽取标记 failed，释放空间后可续跑；已用测试模拟（启动前不写任何批次；运行中第 1 批提交后停止，续跑后结果与不中断完全一致）。正式全量中未触发。

## 5. 运行中发现并处理的问题

时间均为 UTC（用户机器为 UTC+8）。

1. **真实的中断与恢复**（计划外的实测）。`full_run_1` 运行中宿主多次暂停：`resources.jsonl` 的墙钟与单调时钟之差显示至少 7 处 ≥55 秒的暂停，最长一次 916 s（09:09:21 到 09:24:36，约 15 分钟），另有 3 处 3.6–4.3 分钟；宿主采样显示第 2 次失败与第 3 次启动之间也有约 4 分钟空档。用户确认其间让电脑休眠过一次；其余几次暂停的原因未确认。最长一次之后，Airflow 开始第 2 次尝试（推断为判定旧任务失联），而旧任务进程仍在提交批次（09:25:31 与 09:25:36 仍有批次 670、671 提交）：第 2 次尝试因 `ExtractionBusy`（暂时错误）失败，抽取咨询锁防止了两个装载器并行写入；旧进程终止后（无日志记录终止方式）第 3 次尝试于 09:30:24 从第 671 批之后续跑（重扫后追加 121 批），最终 792 批，无重复、无缺口，完整性 SQL 为 0。
2. **完整性 SQL 在全量时二次方变慢**。`batch_counts_differ_from_records` 的相关子查询对每个批次反复按记录序号索引扫描，100 万条时无感，全量时运行约 27 分钟（09:58:41 到 10:25:53）仍未完成。已改成一次分组汇总再连接，全量 88 秒内完成；同时一并检查批次缺记录的情形。
3. **共享内存不足与那次重试的原因**：并行 Hash Join 在 Docker 默认 64 MiB `/dev/shm` 下报过 `DiskFull`（该错误只出现在当时的测试输出，日志未留存），`compose.yaml` 给 postgres 加 `shm_size: 1gb`。为了应用它，我重建了 postgres 容器，旧查询随连接断开以 `AdminShutdown` 失败（`_failures/full_run_1/validate_counts-try1.json`），按暂时错误重试，用新 SQL 在 10:26:53 到 10:28:22 完成。这是我在正式运行中途的人工处置，不是计划内流程。
4. **孤儿运行记录**：被杀进程留下的 `etl_run` 状态一直是 running。现在取得抽取锁后，同抽取的 running 运行被登记为 failed 并带原因；`full_run_1-t1` 已据此被后面的重跑收尾（671 批、6,710,000 条）。
5. 业务库 `etl_movies` 同时有用户先前按 runbook 运行的 20,000 条样本抽取（`39019c6f…`，`my_run_1`）；查询均限定抽取 ID，不影响全量数字，数据库总大小含它（可忽略）。

## 6. 局限

- 资源试跑与估算基于前缀样本，不是随机样本；时间估算已被全量证伪（实测约为估算的 3.7–4 倍），表述以实测为准。
- 报告中的程序版本"Git 提交"为空：容器内没有提供提交号。**全量装载是在未提交的工作树上运行的**，源码树哈希 `e883f405…` 没有对应的 Git 提交，也无法重建；它只覆盖 `src/etl_task1/*.py`，不含 `sql/`、`dags/`、`compose.yaml`。装载之后到最终提交之间的差异：`checkpoint.py`（新增登记遗留运行为 failed）、`loader.py`（新增一行调用）、`reporting.py`（复用时的渲染）、`sql/task1_integrity.sql`（一项核对改写）、`compose.yaml`（`shm_size`）。依据是文件修改时间与 `git diff`，没有触及解析、规则、标识或数据写入路径（`parser.py`、`transform.py`、`quality.py`、`records.py`、`identity.py`、`source.py`、`evidence.py`、`db.py`、`validation.py`，以及 `loader.py` 的 `build_batch`/`commit_batch`/`flush`）。
- 重跑 `full_run_2_rerun` 的 `report.md` 资源节是旧版渲染（"约 None 字节/条、采样峰值 0.0 MiB"）；渲染已改好，该证据文件未重新生成。
- `full_run_1` 的 `resources.json` 只覆盖最后一次尝试，其 `records_before_run` 当时恒为 0（代码缺陷，已修并有测试，旧文件未重生成），1,186 条/秒也含重扫时间；完整数据在 `resources.jsonl`，本报告的跨尝试汇总由采样文件计算。
- 全量只运行了一次装载；"同输入重跑"验证的是复用路径，不是从空库再装一次。
- 未试验更小的 `shared_buffers` 或其他主键方案，速率下降的原因仍是推测。

## 7. 离线复现（T1-06）

2026-10-26 在独立的 Compose 项目 `etl-repro`（全新 PostgreSQL 卷、不同端口、全新口令、`compose.offline.yaml` 使容器无外网）里按 [runbook](runbook.md) 第 7 节复现。首次复现时没有保留日志，被审核指出后同日重做并保存到 `outputs/repro/`（`01-up.log` 至 `09-down.log`、`runs/` 下各运行的摘要，不入库）：

- 容器内连接 `pypi.org` 解析失败（`02-offline-check.log`），确认无外网；镜像用已有的 `etl-task1:local`（`--no-build --pull never`）。
- 全量测试 196 项通过（`03-pytest.log`；单元＋集成，连接新库的 `etl_test`，包含 100,000 条真实记录试跑，数据只读挂载）。
- 固定样本经真实 scheduler：`repro_ok` 成功，读取 10＝有效 6＋隔离 4，产品/用户 observed 6/8、valid 4/4、空用户 1。
- 恢复：`repro_fault` 在第 2 批提交前故障，`extract_load` 失败、`run_gate` 失败，已提交 1 批（4 条）；同参数去掉故障的 `repro_resume` 续跑 2 批（6 条），结果与不中断一致（10/6/4，6/8，4/4，1），`etl_run` 依次为 failed、succeeded（`08-db-state.log`）。
- 100 万条样本经 Airflow 成功（`04-trial-1m.log`）。
- 复现完成后只用 `docker compose down -v` 删除 `etl-repro` 的容器、网络和卷。复现栈与日常栈共用宿主的 `./outputs` 目录，所以对数据库是隔离的，对 `outputs/` 并非完全隔离（它写入 `outputs/resource-trial/` 与 `outputs/e2e/`，后者已按流程删除）。日常栈和业务库未被改动。

无个人路径或口令入库：已扫描受 Git 跟踪的文件，没有本机用户目录路径、`.env` 或密钥；口令、Fernet 密钥和 JWT 密钥只来自未入库的 `.env`，复现用的是现场生成的随机值。`compose.offline.yaml` 是本轮新增文件，随提交入库。限制：`requirements.lock` 只锁定 aarch64（macOS arm64 + Colima）；其他架构需重新生成锁文件。镜像构建（`docker compose build`）需联网一次，离线复现的前提是镜像已准备好。

## 8. AI 辅助与人工核验

计划、代码与文档由 Claude（Claude Code）辅助完成。数据事实来自可回放的原始证据、SQL 核对和运行产物，不来自 AI 输出。

**用户已人工确认**（来自用户在会话中的明确表态）：全量数字及与官方基准的对照与守恒（第 1、2 节）；12 条差异的解释及保持现状（规则 R13，不为凑官方数量合并，不重跑全量）；`full_run_1` 最长一次停顿期间电脑休眠过一次；严格 UTF-8 隔离的决定（约 0.78% 记录）；接受全量装载在未提交工作树上运行的现状（第 6 节），不在已提交代码上重跑；失败报告措辞与 `review_required` 使 `run_gate` 失败的设计。用户按 runbook 在本机自行运行过样本和端到端演示（据其会话中的说明），并在 Airflow 界面看到包含 AI 触发的故意失败演示在内的多次运行，询问并确认了失败含义。故意失败的演示运行（错误哈希、gzip 截断等）是 AI 触发的。

**AI 独立审核**（非人工）：2026-10-26 另派一个 AI 子代理只读审核了字段字典与本报告，复算了约 55 项（采样文件、日志、SQL 对照、源码树时序）。它发现并已修正的错误：净装载耗时重复扣除停顿（原 44 分钟、约 3 倍，改为 59.5 分钟、3.7–4 倍）、重扫记录数和耗时口径、停顿不止一次、WAL 等单位混用、100k 与 100 万条试跑证据被覆盖或删除、`records_before_run` 恒为 0、tasks.md 与 README 的过时内容。这属于 AI 检查，不等同于用户已审阅。

**尚未人工复核**：本报告第 4 节资源数字与推断（速率下降的原因）、第 5–7 节叙述、字段字典的逐条含义（类型、约束、索引附录由数据库自动生成，不属于人工撰写内容）。
