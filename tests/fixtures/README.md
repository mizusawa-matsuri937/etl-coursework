# 任务 1 固定样本

`build_fixtures.py` 生成 10 条人工构造记录（非真实评论）的 gzip 与 manifest，gzip 头时间固定，结果可重复。
`task1_cases.json` 是按 spec 固定样本表手写的预期值，不由实现生成。`data/` 中的文件为已生成并提交的副本，
`tests/unit/test_fixtures.py` 会核对它与生成脚本的输出逐字节一致。

重新生成（工作目录：项目根目录）：

```bash
python tests/fixtures/build_fixtures.py
```

manifest 的 `download_completed_at` 为 null，表示来源时间未知；`source_url` 是占位地址，不是真实来源。
额外换行、超长行等夹具由各单元测试在临时目录中现场构造，不计入这 10 条的预期数字。
