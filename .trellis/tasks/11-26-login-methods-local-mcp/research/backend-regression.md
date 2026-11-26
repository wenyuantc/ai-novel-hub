# 0079 后端完整回归

2026-11-26 在 backend 目录使用 Python 3.12.10，对冻结后的后端代码运行 SQLite 默认门禁。命令为 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest -n 4 --dist loadfile --durations=20 -v --tb=short -r fE`。四个 worker 共收集 7817 项，7687 通过、111 条件跳过、19 预期失败、21 条 pytest 警告，用时 42 分 39 秒，退出码 0。`.venv/bin/ruff check .` 和 `git diff --check` 均通过。PostgreSQL 的独立验收见[联合验收](integration.md)。

首轮全量有三项失败。登记账号的公开字段断言已增加 `has_local_password` 并校验密码账号为真。0067 迁移用例改用数据库反射读取 0066 真实 MCP 表，仍逐列比较迁移前后以及回滚后数据。实际 stdio 人工确认用例以最终提交候选的提案格式验收。修复后三个失败位置及其相关参数化分支定向重跑 7 通过、1 条件跳过；上述完整门禁再次运行，三项全部通过。

单书导出格式仍为 v18，数据库元信息更新为 0079。历史迁移专项 6 通过、2 条件跳过。最终 MCP 管理、真实 stdio、智能体发送、远程传输回归组 106 通过、6 预期失败。
