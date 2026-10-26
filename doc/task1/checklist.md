# 任务 1 AC 完成情况

状态：2026-10-22 完成计划重整与一致性检查；业务实现尚未开始。全部 AC 的输入和预期定义见 [spec](spec.md)，设计见 [design](design.md)，步骤见 [tasks](tasks.md)。

下列测试路径是计划中的验证位置，文件尚未实现，不是已运行证据。实际执行后填写日期、命令、测试方法或运行 ID、结果及证据位置，再勾选；不在本文件重新定义 AC。

- [x] AC00 计划与已确认决定一致：2026-10-22 已核对 spec 五个板块、规则短条目、示范 AC 表头、目录链接、AC 全量对应及完成状态；仅文档检查通过。
- [x] AC01 输入版本可核验：2026-10-26 `tests/unit/test_source.py` 与流程测试通过。正确输入得到实测大小/哈希，哈希或大小不符、缺文件、缺必需元数据均在装载前失败；错误哈希经真实 DAG 运行后数据库评论行数为 0；下载时间与验证时间分开，本地哈希不冒充官方值。
- [x] AC02 边界与文本保留正确：2026-10-26 容器内 `pytest -q tests/unit` 共 110 项通过，覆盖 LF/CRLF、连续空行、EOF 无换行、字段重排、重复前缀/未知行隔离、块边界（chunk=1..64）与超过 32 MiB 单行旁路；20 MiB 记录的 tracemalloc 峰值低于 6 MiB。正式 RSS 实测见 AC12。
- [x] AC03 ID 稳定且保留内容重复的记录：2026-10-26 `tests/unit/test_identity.py`、`test_parser.py` 通过；同序号 ID 不变，内容相同的两条 ID 不同且都保留，样本/全量抽取 ID 不同。
- [x] AC04 基础转换保留原值：2026-10-26 `tests/unit/test_transform.py`、`test_quality.py` 通过；评分/有用票/时间的合法与非法输入、Unix 0、空与缺失的用户/正文均按 spec 验证。
- [x] AC05 原始证据可逐字节回放：2026-10-26 `tests/unit/test_evidence.py` 与 `tests/integration/test_evidence.py` 通过。10 条夹具按定位重取的字节与哈希一致，隔离表字节与源文件一致，非法编码/NUL 保留，问题可追到字段，旁路文件完整落盘后才被引用；模拟旁路写入不完整时批次回滚、无记录引用不完整文件，修复后重跑成功。
- [x] AC06 数据约束与环境隔离生效：2026-10-26 环境部分见 T1-01（三库互相拒绝访问、源与 src 只读、down/up 后数据保留）；`tests/integration/test_load.py` 通过：主键/外键/CHECK 约束拒绝非法数据，有效/隔离子表互斥（复合外键）与完备（提交时延迟触发器），同键不同内容抛 `LoadConflict`，etl_test 角色无法连接业务库与 Airflow 元数据库，完整性 SQL 全 0。
- [ ] AC07 重跑幂等：部分完成，不勾选。2026-10-26 `test_rerun_is_idempotent` 通过：同库两次装载固定样本，结果行、问题与批次均不变，仅新增一条 `reused` 执行审计；样本与全量互为独立抽取。正式全量完成后的重跑仍未执行。计划验证：`tests/integration/test_load.py`；保存正式两次运行 ID、行数、批次与 SQL 核对输出。
- [x] AC08 数量口径准确且守恒：2026-10-26 `tests/integration/test_counts.py`、`test_reports.py` 通过。固定样本 10＝6＋4，observed 产品 6/用户 8，valid 4/4，空用户 1，删除 0；记录 7 计入读取数且不生成虚构产品；问题行数（9）与有问题的记录数（7）分别统计；经真实 DAG 生成的 summary.json 与 SQL 汇总一致。
- [x] AC09 提交前后故障可恢复：2026-10-26 `tests/integration/test_resume.py` 通过。批次行数 3：第 2 批提交前终止保留前 3 条，提交后丢失确认保留前 6 条，恢复后快照与无中断结果完全一致；在每个批次的提交前/后注入故障（共 8 个点）均恢复一致；配置或输入变化不沿用检查点，篡改检查点被检出；gzip 尾部截断时已提交批次保留、抽取标记 failed、不报成功。gzip 重扫耗时未单独测量，见 AC12。
- [x] AC10 Airflow 实际执行完整流程：2026-10-26 `tests/integration/test_dag.py`（定义：六任务依赖、`all_done` 收尾、`max_active_runs=1`、`max_active_tasks=1`、`retries=2`、手动触发）通过；`scripts/run_dag_e2e.py` 经真实 scheduler 运行固定样本，六个任务成功，结果为 10/6/4，report 含 DAG、run、extraction 标识，XCom 载荷测试确认不含评论正文且小于 20 KB；真实文件前 50,000 条样本运行成功，extraction 显示 scope_complete=true、source_eof_reached=false、gzip_verified=false。
- [x] AC11 失败不能变成虚假成功：2026-10-26 `tests/integration/test_pipeline_failures.py` 与真实 scheduler 运行。错误哈希、缺文件、gzip 尾部缺失、守恒破坏（删除暂存行）、与官方基准差异（review_required 使门禁失败）、报告目录不可写、导出失败均不会得到完整交付；暂时错误按 retries=2 重试，真实运行 attempt=1 失败、attempt=2 成功，确定性错误只有 attempt=1；数据库不可用时 finalize 仍写出失败报告与失败文件；真实 DAG 中 finalize 成功但 run_gate 失败，DAG 总状态为 failed。守恒破坏与目录不可写仅在函数级验证，未经 scheduler 触发。
- [ ] AC12 资源预算有实测依据：未执行。计划验证：`tests/integration/test_resource_limits.py`、监测文件、数据库查询，记录容器/宿主限制。
- [ ] AC13 全量处理及差异核对：未执行。计划验证：实际 DAG run、`sql/task1_integrity.sql`、`sql/task1_counts.sql`、manifest、summary 和排查依据；差异未解释时保持 `review_required`，不得勾选。
- [ ] AC14 交付可复现且状态真实：部分完成，不勾选。镜像构建、锁定版本与配置示例已于 2026-10-26 验证；runbook、字段字典、样本复现与恢复尚未执行。计划验证：按 runbook 实际复现固定样本和恢复，核对已勾选项目的证据；未初始化 Git 或未运行步骤不能标完成。

当前汇总：计划 AC 完成 1/1；实施 AC 完成 10/14（AC01–AC06、AC08–AC11，均为样本/集成测试级）；AC07、AC14 部分完成，AC12、AC13 未执行。容器已启动，尚无建表或全量运行记录。本次初始 Git 提交仅归档计划与项目规范，不代表实施验收完成。
