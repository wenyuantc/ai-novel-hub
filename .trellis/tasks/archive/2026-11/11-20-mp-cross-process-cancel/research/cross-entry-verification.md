# 本人取消、管理员受理与接管的交叉验收

2026-11-23 使用两个独立 FastAPI 应用和两套连接池，显式安排数据库提交、负责执行的引擎收到通知和请求方最终读取的顺序。模型在可观测的分片处等待。B 的本人请求通过真实 HTTP 路由提交后，由 A 的独立会话读到原轮次、原 worker 和取消时间，随后才触发其他动作。测试位于 `backend/tests/test_c3_cross_entry.py`。

| 交错 | 实际观察 |
| --- | --- |
| B 受理本人取消，客户端断连，再由管理员在 B 取消 | 管理员收到 202 和操作号，A 中断一次模型调用，原章节回到 draft 且正文保留。一个生成日志、原统计都记录 cancelled，只有一条对应操作号的成功受理记录。 |
| B 等待本人结果，管理员已受理，原登录令牌被撤销 | A 完成同一轮清理；B 的原请求返回 401，响应没有任务内容。管理审计、模型中断、生成记录和章节释放仍各自只对应原轮次。 |
| 批量任务的暂停提交先于 A 的取消扫描，作者随后继续同一 UUID | 第二章已实际进入模型调用。B 的旧请求返回 409 `task_round_changed`；新轮次的取消时间为空，第二章能完成，两次模型流都正常退出。 |
| 旧任务正常完成后经 API 删除，整数 ID 随后分配给另一用户 | B 的旧请求返回 404，响应不包含新任务的私有标题或 UUID。新任务保持原状态，取消时间为空。双方数据库均显式重建相同整数 ID。 |
| C1 已成功认领旧 running 行并进入原回退，随后 B 收到本人取消和管理员受理 | 暂停回退时，由独立会话观察到管理操作号和仍在运行的原轮次。同一执行者收到管理扫描也不会再跑回退。放行后返回 interrupted，原业务处理器只被调用一次，管理审计一条，统计为 interrupted，管理和本人意图都已清除。 |

实际使用的每个测试库由 `isolated_worker_database` 分配。SQLite 使用独立临时文件，PostgreSQL 16 使用本机 `ainovel_test` 和每个用例新建的独立 schema。项目全量及真实 uvicorn 进程的接线验收见本任务综合记录。

```sh
# 在 backend/ 下执行
env -u AINOVEL_POSTGRES_TEST_URL LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 0 -q tests/test_c3_cross_entry.py
# 配置 AINOVEL_POSTGRES_TEST_URL 为本机专用库后执行相同模块
uv run pytest -n 0 -q tests/test_c3_cross_entry.py
```

SQLite 串行 5 项全部通过，耗时 24.25 秒。PostgreSQL 16 串行 5 项全部通过，耗时 58.82 秒。定向文件的 Ruff 检查和前端 TypeScript 编译通过。
