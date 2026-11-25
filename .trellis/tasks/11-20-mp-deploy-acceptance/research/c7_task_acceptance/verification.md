# C7 任务与实时流的真实进程验收

2026-11-25 在本机隔离环境执行。`process_tasks.py` 用临时数据库、临时 Uvicorn 进程、本机脚本化模型和一次性密钥；PostgreSQL 使用本机专属 `postgres:16-alpine` 容器，结束时清理。应用从真实 HTTP、任务调度、数据库和 SSE 路由运行。PostgreSQL 的两个独立实例在本机各启动一个 Uvicorn 进程；这一组不代表两台物理主机或两个应用镜像容器的验证。

临时 ASGI 包装器只添加接入 PID 响应头、在进入应用前拒绝落到错误 PID 的请求，以及提供带一次性密钥的本机测试入口调用真实 `peer.stop()`。它没有替换租约、权限、调度、取消、删除或 SSE 实现。跨端发起取消、撤权、删除与订阅时，脚本按数据库的原执行者 PID，确认接入 PID 确实不同。共享公开端口的多 worker 通过短连接和 PID 头识别具体 worker。

## 已观察结果

历史运行只留下摘要，不能从摘要恢复完整 PID 和端口。下表是重新运行后保存的结构化证据；每份 JSON 记录 UTC 时间、数据库查得的任务执行 PID、实际接入 PID、对应公开端口、所通过断言及必要状态。多 worker 的不同 PID 共用同一公开端口，`services` 中的 supervisor PID 不当作执行者。报告只在整次运行成功并清理临时资源后写入。

| 场景 | SQLite 同机多进程 | PostgreSQL 同一服务多 worker | PostgreSQL 两独立应用进程 |
| --- | --- | --- | --- |
| 运行任务期间启动或重启另一进程 | [通过](evidence/sqlite-live.json) | [通过](evidence/pg-workers-live.json) | [通过](evidence/pg-instances-live.json) |
| 作者跨端实时流 | [通过](evidence/sqlite-live.json) | [通过](evidence/pg-workers-forward.json) | [通过](evidence/pg-instances-live.json) |
| 协作者撤权及降为查看者 | [通过](evidence/sqlite-live.json) | [通过](evidence/pg-workers-live.json) | [通过](evidence/pg-instances-live.json) |
| Agent 工具及提案结构化事件 | [通过](evidence/sqlite-live.json) | [通过](evidence/pg-workers-live.json) | [通过](evidence/pg-instances-live.json) |
| 内部通道断开：作者 | [通过](evidence/sqlite-author-off.json) | [通过](evidence/pg-workers-author-off.json) | [通过](evidence/pg-instances-author-off.json) |
| 内部通道断开：协作者 | [通过](evidence/sqlite-collab-off.json) | [通过](evidence/pg-workers-collab-off.json) | [通过](evidence/pg-instances-collab-off.json) |
| 强杀单章：占用清理、终态稳定及重试 | [通过：33.603 秒](evidence/sqlite-recovery.json) | [通过：28.601 秒](evidence/pg-workers-recovery.json) | [通过：33.771 秒](evidence/pg-instances-recovery.json) |
| 强杀分析：待核对并放行同键后继 | [通过：29.128 秒](evidence/sqlite-recovery.json) | [通过：30.187 秒](evidence/pg-workers-recovery.json) | [通过：30.425 秒](evidence/pg-instances-recovery.json) |
| 全站／每人上限及同键 FIFO | [通过](evidence/sqlite-scheduling-cancellation.json) | [独立 AC3–AC5 验收](../c7_scheduling_acceptance/verification.md) | [独立 AC3–AC5 验收](../c7_scheduling_acceptance/verification.md) |
| 本人取消、管理员取消、作品删除 | [通过](evidence/sqlite-scheduling-cancellation.json) | [独立 AC3–AC5 验收](../c7_scheduling_acceptance/verification.md) | [独立 AC3–AC5 验收](../c7_scheduling_acceptance/verification.md) |

SQLite 的新调度证据逐项保存请求实际接入 PID、数据库查得的执行 PID 与端口；本人取消、管理员取消、作品删除分别从另一 PID 发起，原模型连接退出。PostgreSQL 对应场景另见[独立 AC3–AC5 验收](../c7_scheduling_acceptance/verification.md)。

调度场景在每次取得首任务后，先建立到另一 PID 的 keep-alive 连接，再短暂 `SIGSTOP` 首执行进程并从另一端送入第二任务；整个暂停有 4.5 秒绝对截止，`finally` 恢复。本次 SQLite 两次暂停分别为 0.583 秒、0.161 秒，全站峰值 2、A 用户峰值 2、B 用户峰值 1，X1、X2、X3 同键按序。两轮首波任务由两个执行 PID 领取。强制中断／暂停前均检查进程属于本次脚手架，worker 另核验父进程为本次 supervisor。首次未加屏障的 SQLite 调度尝试中所有任务恰由一个 PID 领取，因此没有作为跨进程证据。

先前强杀单章生成后，SQLite、PostgreSQL 多 worker、PostgreSQL 两实例的摘要分别记录了 31.174、27.184、28.302 秒；分析任务保留“结果待核对”并放行同键后继的摘要分别记录了 30.776、33.595、28.326 秒。上述历史运行的完整 PID 和端口未持久保存，不能用这些数字补造当前证据。本次三种形态都在后端全量 pytest 并发运行期间重新采集，计时均包含启动接管进程；已保留完整执行者、请求端和接管 PID 与端口。

分析强杀探针首轮错误地要求任务必须变成 `interrupted`，三种形态均等待超时。核对 `engine.py` 和 C1 已验收契约后，将断言改为验证持久待核对状态、拒绝重试及同键后继放行；未因此修改产品代码。另一次连续运行两个强杀场景时，脚手架误用被第一轮杀掉进程的旧端口，导致第二轮注册请求超时；脚本改为选择仍存活的验收进程，三种形态均已复跑成功。

## SQLite 忙锁和 HTTP 500

脚手架记录每个应用 HTTP 5xx 的进程、状态及时间，并检索应用日志中的 `database is locked` / `SQLITE_BUSY`；请求路径和正文不进入持久报告。本轮 [SQLite 运行中及实时流](evidence/sqlite-live.json)、[作者断通道](evidence/sqlite-author-off.json)、[协作者断通道](evidence/sqlite-collab-off.json)、[强杀接管](evidence/sqlite-recovery.json)及[调度、取消和删除](evidence/sqlite-scheduling-cancellation.json)的结构化记录均为 HTTP 5xx 0、忙锁日志命中 0。只覆盖对应脚本和场景，不能代替其他脚本的日志检查。

## 失败留痕与脱敏复验

脚本失败时在同目录 `diagnostics/` 新建权限为 `0600` 的 JSON 报告；也可通过 `--diagnostics-dir` 指定持久目录。报告只保留拓扑、场景、阶段、开始与捕获时间、脚手架及已观察到的执行者 PID、进程启动／退出时间、HTTP 5xx 状态与进程、日志分类计数、异常类别和调用位置。捕获时间可能包含退出应用的清理耗时。异常原文、环境变量、原始日志、请求路径／正文和模型内容均不保存；命令行失败也不打印原始异常。报告写入失败时输出安全错误类别，不能声称已留痕。

2026-11-25 运行 `verify_failure_diagnostics.py`，使用实际 SQLite 多进程分别制造[场景故障](diagnostics/c7-failure-20261125T024418-643fb09a.json)、[第二个实例启动故障](diagnostics/c7-failure-20261125T024425-becf6837.json)和[应用清理故障](diagnostics/c7-failure-20261125T024436-28b3f09d.json)，另以替身模拟[临时目录创建失败](diagnostics/c7-failure-20261125T024425-7d165eb6.json)。场景故障中把实际一次性 JWT、JWT 密钥、加密密钥、管理员密码、测试令牌及模拟请求／模型正文写入临时异常和日志，再逐值检查所有这些内容均未进入持久报告；还检查报告记录了两个应用进程与调用位置。场景报告内的 503、忙锁和损坏采集记录是**故意注入的测试数据**，不代表真实业务故障。损坏的 5xx 或进程采集记录会被计数并使正式验收失败，不能按零错误通过。

这四次受控故障后临时目录与已启动的应用进程均已清理。随后使用相同诊断目录重新运行正常 `forward` 场景，SQLite 执行者 PID 53318、接入 PID 53482，两个 PID 收到完整事件序列，HTTP 5xx、损坏的 HTTP 采集记录和忙锁命中均为 0；没有产生新的失败报告，也清理了临时资源。独立复跑 PostgreSQL 多 worker `forward`，执行者 PID 53648、接入 PID 53647，同样通过且上述统计均为 0。

复跑命令（在 `backend/` 下，变量指向本记录同目录的脚本）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. .venv/bin/python "$C7_TASK_DIAGNOSTIC_CHECK"
```

其中 `C7_TASK_DIAGNOSTIC_CHECK` 为 `verify_failure_diagnostics.py` 的绝对路径。脚本每次运行都会留下新的受控故障报告；它只读取并清理自己创建的临时资源，不删除以前的报告。

## 复跑

在 `backend/` 下，将 `C7_TASK_SCRIPT` 指向本记录同目录的 `process_tasks.py`。该脚本通过向上查找 `backend/alembic.ini` 定位仓库根，任务归档后仍可运行。三种形态均使用同一命令，只需把 topology 替换为 `sqlite`、`pg-workers`、`pg-instances`。PostgreSQL 需要本地已有 `postgres:16-alpine` 镜像和 Docker。

需要持久保存本次补采的结构化成功证据时，将 `C7_TASK_RECORDER` 指向同目录的 `record_success.py`，并将 `C7_RESULT_FILE` 指向**尚不存在**的输出文件；脚本不会覆盖已有证据。每种形态各使用新的输出路径：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. .venv/bin/python "$C7_TASK_RECORDER" \
  --topology sqlite --cases restart forward revoke downgrade agent-structured --output "$C7_RESULT_FILE"
```

将 `--cases` 分别换成 `author-off`、`collab-off` 可复跑断通道分支；两者需要分别启动隔离环境。强杀组使用 `--cases kill kill-serial`，计时断言保持原来的 35 秒阈值；SQLite 的调度与取消／删除组使用 `--cases scheduling cancel admin-cancel delete`。每次都选择新的输出文件。记录器仅采用业务断言已通过后的字段白名单，不保存 SSE 载荷、模型文本、请求体或凭据。`verify_success_redaction.py` 已验证临时随机敏感值不会进入成功证据、未知事件被拒绝、已有证据不会覆盖，且文件不授予组或其他用户权限。

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. .venv/bin/python "$C7_TASK_SCRIPT" \
  --topology sqlite --cases scheduling restart forward revoke downgrade cancel admin-cancel delete agent-structured
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. .venv/bin/python "$C7_TASK_SCRIPT" \
  --topology sqlite --cases kill kill-serial
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. .venv/bin/python "$C7_TASK_SCRIPT" \
  --topology sqlite --cases author-off
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. .venv/bin/python "$C7_TASK_SCRIPT" \
  --topology sqlite --cases collab-off
```

开始时所有库、端口和目录均随机新建；脚本仅结束自己创建的进程及容器。输出包含 PID、案例、事件类型、计时及 5xx/忙锁统计，不输出一次性密钥或测试正文。`ruff check` 单独检查脚本。

## 范围边界

本组的 AC1 是普通任务及其模型调用期间另一进程启动；备份期间另一进程启动见[独立备份验收](../c7_operation_acceptance/verification.md)，外部 MCP 调用和作品删除意图期间的进程启动见[独立在途工作验收](../c7_inflight_acceptance/verification.md)。短暂的同机两个 Uvicorn 进程和本机 Docker PostgreSQL 不能证实真实跨主机网络或共享文件系统锁。没有据本组结果修改 README 的生产部署限制。
