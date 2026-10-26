# 任务 1 AC 完成情况

状态：2026-10-22 完成计划重整与一致性检查；业务实现尚未开始。全部 AC 的输入和预期定义见 [spec](spec.md)，设计见 [design](design.md)，步骤见 [tasks](tasks.md)。

下列测试路径是计划中的验证位置，文件尚未实现，不是已运行证据。实际执行后填写日期、命令、测试方法或运行 ID、结果及证据位置，再勾选；不在本文件重新定义 AC。

- [x] AC00 计划与已确认决定一致：2026-10-22 已核对 spec 五个板块、规则短条目、示范 AC 表头、目录链接、AC 全量对应及完成状态；仅文档检查通过。
- [ ] AC01 输入版本可核验：未执行。计划验证：`tests/unit/test_source.py`，并核对失败场景的数据库行数。
- [ ] AC02 边界与文本保留正确：未执行。计划验证：`tests/unit/test_parser.py`、`tests/unit/test_evidence.py`；内存测量见 AC12。
- [ ] AC03 ID 稳定且保留内容重复的记录：未执行。计划验证：`tests/unit/test_identity.py`、`tests/unit/test_parser.py`。
- [ ] AC04 基础转换保留原值：未执行。计划验证：`tests/unit/test_transform.py`、`tests/unit/test_quality.py`。
- [ ] AC05 原始证据可逐字节回放：未执行。计划验证：`tests/unit/test_evidence.py`、`tests/integration/test_evidence.py`；模拟旁路写入失败，不得提交指向不完整文件的记录。
- [ ] AC06 数据约束与环境隔离生效：未执行。计划验证：Compose 配置与实际连接/挂载检查；`tests/integration/test_load.py`、`sql/task1_integrity.sql`。
- [ ] AC07 重跑幂等：未执行。计划验证：`tests/integration/test_load.py`；保存正式两次运行 ID、行数、批次与 SQL 核对输出。
- [ ] AC08 数量口径准确且守恒：未执行。计划验证：`tests/integration/test_counts.py`；SQL 明细、问题码分布与 summary.json 一致。
- [ ] AC09 提交前后故障可恢复：未执行。计划验证：`tests/integration/test_resume.py`；比较有序记录哈希与规范值，记录 gzip 重扫耗时。
- [ ] AC10 Airflow 实际执行完整流程：未执行。计划验证：`tests/integration/test_dag.py`；真实 DAG run 状态、数据库与文件证据，不能只用导入检查代替。
- [ ] AC11 失败不能变成虚假成功：未执行。计划验证：`tests/integration/test_reports.py`、`tests/integration/test_pipeline_failures.py`；检查 run_gate 与 DAG 总状态。
- [ ] AC12 资源预算有实测依据：未执行。计划验证：`tests/integration/test_resource_limits.py`、监测文件、数据库查询，记录容器/宿主限制。
- [ ] AC13 全量处理及差异核对：未执行。计划验证：实际 DAG run、`sql/task1_integrity.sql`、`sql/task1_counts.sql`、manifest、summary 和排查依据；差异未解释时保持 `review_required`，不得勾选。
- [ ] AC14 交付可复现且状态真实：未执行。计划验证：按 runbook 实际复现固定样本和恢复，核对已勾选项目的证据；未初始化 Git 或未运行步骤不能标完成。

当前汇总：计划 AC 完成 1/1；实施 AC 完成 0/14。尚无业务测试、容器启动、建表或全量运行记录。本次初始 Git 提交仅归档计划与项目规范，不代表实施验收完成。
