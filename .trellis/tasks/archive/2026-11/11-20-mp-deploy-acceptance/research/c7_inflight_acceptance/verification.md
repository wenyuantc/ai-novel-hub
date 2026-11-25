# C7 AC1：外部调用和删除意图期间启动新进程

2026-11-25 在隔离环境执行，SQLite 3.47.1，PostgreSQL 16.15（本次临时容器使用 `postgres:16-alpine`）。所有结果都来自真实 Uvicorn 进程、公开 HTTP 接口和数据库；没有读取项目 `.env`，临时数据库、进程与专属容器均已清理。

## 测试方法

脚本复用同一任务的 [`c7_task_acceptance/process_tasks.py`](../c7_task_acceptance/process_tasks.py)：测试 ASGI 包装器只为响应追加 PID，以及在请求 PID 不匹配时返回 421 让客户端重新连接；任务、权限、租约、迁移、工具调用与删除逻辑均为真实实现。模型和 MCP 是仅监听本机的受控 HTTP 替身。智能体通过真实模型工具调用产生 `external` 提案，用户通过真实确认接口发出 MCP JSON-RPC `tools/call`；替身发送响应头后等待，确保网络请求确实已经到达且原调用仍在途。

- **外部调用**：按数据库中 `AgentChange.external_worker_key`、`TaskWorkRecord` 和执行者 PID 确认原调用仍是 `executing` / `active`。另起一个实例；同端口多 worker 形态向本次 Uvicorn 父进程发 `SIGTTIN` 添加 worker。等待新进程完成应用启动并实际处理健康请求后，核对原状态、归属未变，确认请求仍等待且工具只被调用一次。释放替身后，提案为 `executed`、Agent 任务为 `succeeded`，原活动记录已清理。
- **删除意图**：先保持真实 MCP 调用在途，再由另一进程发起该作品的公开删除请求；数据库确认删除 token、请求 worker 和未删除状态。新进程启动并处理健康请求后，同一 token、删除 worker 和外部调用的原状态、原归属均未变化，两条原请求仍在等待。释放 MCP 后，按既有冲突语义，删除返回 204，外部确认返回 409、提案标记 `failed`，任务为 `cancelled`，删除意图清空、作品进入回收站；MCP 服务只收到一次 `tools/call`。

同端口多 worker 的 PID 仅作观测，通过禁用测试客户端的连接复用和每次响应头确认目标 PID；产品部署不依赖会话粘连。早期尝试用 `SIGSTOP` 挂住生成任务时，Uvicorn supervisor 的健康检测替换了被冻结的 worker，故没有把该轮作为产品故障或通过证据；最终改用真实 MCP 等待响应控制删除时序，不再暂停应用进程。

## 结果

| 形态 | 外部调用请求进程 → 新进程 | 删除时外部调用 / 删除请求 / 新进程 | 结果 |
| --- | --- | --- | --- |
| SQLite 同机多进程 | 4899 → 5144 | 5331 / 5278 / 5446 | 两项通过；各收到一次 MCP 调用，HTTP 5xx 0，应用日志 SQLite 忙锁命中 0。 |
| PostgreSQL 同实例多 worker | 95040 → 95335 | 256 / 257 / 2658 | 两项通过；各收到一次 MCP 调用，HTTP 5xx 0。 |
| PostgreSQL 两独立实例 | 3489 → 3664 | 3921 / 3985 / 4179 | 两项通过；各收到一次 MCP 调用，HTTP 5xx 0。 |

PID 只标识本次对应的隔离运行。SQLite 应用包装器记录本场景所有实际应用 HTTP 5xx；脚本另搜索应用日志中的 `database is locked`、`database table is locked`、`SQLITE_BUSY` 和 `SQLITE_LOCKED`，最终运行均为 0。此结论仅覆盖本页两种场景，不能替代父任务 AC11 的完整矩阵。

## 复跑

在 `backend/` 下，设 `C7_INFLIGHT` 为本页同目录下 `run_inflight.py` 的实际路径（任务归档后改用归档目录）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python "$C7_INFLIGHT" --topology sqlite
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python "$C7_INFLIGHT" --topology pg-workers
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python "$C7_INFLIGHT" --topology pg-instances
```

PostgreSQL 形态要求本机 Docker 已有 `postgres:16-alpine`；脚本使用 `--pull=never`、随机凭据和随机本机端口，不使用现有数据库。支持 `--case external` 或 `--case deletion` 单独复跑。两种删除与调用分别在独立临时环境中执行，避免前一场景的进程登记或资源状态影响后一场景。

脚本检查：`uv run --no-sync ruff check ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_inflight_acceptance/run_inflight.py`、`uv run --no-sync python -m py_compile .../run_inflight.py` 均通过。

## 失败留痕与脱敏验证

2026-11-25 补充失败处理：脚本在隔离临时目录删除前写入 `failure_reports/`，以退出码 1 和 JSON 输出所保存报告的实际路径。报告只包含场景、形态、阶段、UTC 时间、已知的受控进程 PID、固定异常类别、固定白名单测试代码文件及行号、HTTP 5xx 状态计数、无效记录数和 SQLite 忙锁命中数。它不复制请求路径、正文、模型内容、凭据、原始异常、栈文本或应用日志。应用还没有返回进程句柄、日志无法读取或数据库无法取到 worker 时，对应字段标记 `null`，不当成零；报告本身写入失败时只输出固定的 `write_failed`，不打印写入异常。

报告中的代码位置限于测试脚本和辅助脚本，不能定位被测应用内部的每个错误。留痕要求测试进程仍能执行且报告目录可写；被 `SIGKILL`、主机中断或写入失败时不能保证报告或资源清理完成。

以下是**人工注入故障**的结果，并非实际产品 503 或数据库忙锁。测试分别验证运行中、诊断阶段、应用启动和依赖加载阶段的失败：前两项启动真实 SQLite 应用进程；前三项向临时诊断记录和日志注入虚构密钥、请求正文、模型内容、503、损坏记录和一条 `SQLITE_BUSY`；依赖加载阶段在创建临时环境前注入含这些敏感标记的异常。创建了资源的场景检查临时数据库、应用进程、peer socket 均已清理；测试进程结束后报告仍可读取，运行时文件权限为 `0600`。报告及标准输出／错误输出都不包含这三个虚构敏感标记。执行阶段的注入在一次真实 MCP 场景成功后发生；诊断阶段的注入用于验证 5xx 检查本身，不能作为 AC1 行为证据。

| 受控阶段 | 持久报告 | 验证结果 |
| --- | --- | --- |
| 场景执行 | [scenario](failure_reports/sqlite-external-8inlw77k.json) | 原执行完成后注入异常；保留真实应用 PID、异常类别和代码位置。 |
| 诊断 | [diagnostics](failure_reports/sqlite-external-fl6zrv47.json) | 注入的 503 被计数且使运行失败。 |
| 应用启动 | [setup](failure_reports/sqlite-external-6fnrqk5z.json) | 应用句柄不可用时保留测试进程 PID，其余进程信息标记未知。 |
| 依赖加载 | [dependencies](failure_reports/sqlite-external-ex7ooudo.json) | 临时环境尚未创建时，HTTP 和忙锁计数标记未知。 |

本次记录时同目录的其他报告来自实现过程中更早的同类人工注入，其代码行号对应当时版本；上表是最终脚本的验证证据。以后运行新产生的报告应按其实际运行结果判断。

复跑受控失败验证（它会故意产生上述失败报告，但自身应返回成功）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_inflight_acceptance/verify_failure_reporting.py
```

最终代码复跑 SQLite 两个真实场景均通过：外部调用原进程 55080、新进程 55680；删除时外部调用进程 56281、删除进程 56583、新进程 56909。两项各仅一次 MCP 调用，HTTP 5xx 和 SQLite 忙锁日志均为 0。上述受控注入日志不属于这两项成功运行。

PostgreSQL 双独立实例复跑也通过：外部调用原进程 58381、新进程 58815；删除时外部调用进程 60321、删除进程 60543、新进程 61049。两项各仅一次 MCP 调用，HTTP 5xx 均为 0。

共享夹具禁用 PID 探针的连接复用后，PostgreSQL 同 Uvicorn 多 worker 形态再次通过：外部调用原进程 66659、新进程 66975；删除时外部调用进程 68208、删除进程 68207、新进程 68708。两项各仅一次 MCP 调用，HTTP 5xx 均为 0。
