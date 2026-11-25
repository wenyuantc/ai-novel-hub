# C7 PostgreSQL 调度、取消与删除的真实进程验收

2026-11-25；PostgreSQL 16.15（`postgres:16-alpine`），后端使用 Python 3.12。每次运行独立创建临时数据库容器、随机密钥、假模型服务和应用进程；未读取项目 `.env` 或既有数据库。任务结束后脚手架只回收本次创建的资源。

两种形态：`pg-workers` 使用一个 Uvicorn 父进程及两个实际 worker；`pg-instances` 使用两个独立 Uvicorn 应用进程。后者位于同一宿主机，不能证明跨主机网络或共享卷行为。测试专用 ASGI 包装只用于按 PID 定位接入进程、记录实际 PID，以及本任务其他场景的内部通道测试入口；没有替换或修改生产租约、权限、调度、取消或删除逻辑。模型服务是可控的本地替身，业务经真实 HTTP、调度器和数据库执行。

## 父任务 AC3：并发上限与同键 FIFO

分别设置全局上限 `2`、每用户上限 `6`，以及全局上限 `6`、每用户上限 `2`。每次把第一个执行进程短暂暂停，由另一进程受理和执行第二个任务，确认两个进程确实各自领取，再恢复原执行者；不修改业务处理逻辑。保持模型返回等待屏障，观察额外任务在完整轮询周期内不被领取。

| 形态 | 全局上限场景 | 每用户上限场景 | 同键 FIFO |
| --- | --- | --- | --- |
| 同一实例双 worker | `X1` / `Y1` 分别由 PID 87449 / 87450 领取，峰值 2 | `A1` / `A2` 分别由 PID 87449 / 87450 领取；另一用户 `B1` 并行；A 峰值 2、B 峰值 1 | `X1 → X2 → X3`，模型开始及完成顺序一致 |
| 两个独立实例 | `X1` / `Y1` 分别由 PID 90341 / 90557 领取，峰值 2 | `A1` / `A2` 分别由 PID 90341 / 90557 领取；`B1` 并行；A 峰值 2、B 峰值 1 | `X1 → X2 → X3`，模型开始及完成顺序一致 |

两个进程中的 PID 为本次临时运行的事实，不是固定配置。首次双实例尝试中两个进程均启动但全部任务被同一个进程领取，脚手架未能证明跨进程并发而失败；加入上述暂停屏障后重新运行，两种形态均通过且退出码为 0。最终版在暂停前固定另一个进程的 keep-alive 连接，整个屏障有 5 秒截止时间；双 worker 的两次实际暂停分别为 0.795、0.278 秒，双实例为 0.658、0.289 秒，后者未出现 HTTP 5xx。

## 父任务 AC4–AC5：非执行进程取消及作品删除

令模型发送一段正文并等数据库刷盘后，先用 keep-alive 连接固定另一 PID，再暂停真正持有任务的进程，分别发起本人取消、管理员取消和作品删除。暂停最长 4 秒并在 `finally` 恢复，避免共享端口把新连接分配给已暂停进程或使租约过期。暂停期间核对任务仍为 `running`、章节仍为 `generating` 并持有占用、执行记录及名额仍在，模型连接未关闭、作品未删除；本人取消和作品删除没有提前返回。恢复执行进程后，核对模型连接确实关闭、任务 `cancelled`、章节恢复 `draft`，章节占用、执行记录和调度名额均归零。生成记录仅一条且为 `cancelled`，每次只调用一次假模型。管理员在执行者暂停期间连续请求两次，均返回 202 且操作 ID 相同，仅产生一条成功审计。

| 形态 | 执行 PID / 请求 PID | 本人取消 | 管理员重复取消 | 删除作品 |
| --- | --- | --- | --- | --- |
| 同一实例双 worker | 64532 / 64531 | 200，0.895 秒 | 202 / 202，同一操作，0.918 秒 | 204，1.009 秒 |
| 两个独立实例 | 64679 / 64787 | 200，1.064 秒 | 202 / 202，同一操作，0.724 秒 | 204，1.026 秒 |

时间从暂停执行者并发起请求开始，包含恢复及完成状态核对，只是本地健康环境的实测值，不构成故障或持续争用时的上界。脚本在发信号前确认进程归属；它以一次模型调用、唯一生成记录、最终章节与占用状态及管理员审计去重证明持久副作用没有重复；没有向生产回调插桩，因此不单独断言回调函数的调用次数。两种形态的最终成功复验均未发现 HTTP 5xx。

与全量回归并行期间曾出现一次关联验收超时，停在脚手架寻找非执行 PID 的步骤，报告记录 HTTP 5xx 为 0。原共享夹具在 421 后可能重复使用同一连接；本脚本改为先从隔离库选择存活 PID，再逐次新建连接并固定到该 PID。另一次失败发生在“章节已恢复且执行退出记录已清除”的联合断言，原报告没有保存业务状态，无法确认当时哪一项未满足。经核对代码，执行退出确认由名额归还后的异步回调完成；验收现在等待退出记录实际归零，仍保留章节状态、占用及记录的最终断言。

## 失败留痕与脱敏验证

验收失败时，脚本在运行临时目录之外创建私有诊断目录和 JSON 报告，输出 `failure_report` 路径。默认保存在系统临时目录；如需指定持久保存的位置，可用 `--diagnostic-dir` 指向一个已存在的目录。报告只包含场景、拓扑、UTC 时间、耗时、进程 ID、固定错误类别、受信代码文件名和行号、HTTP 5xx 数量及无法解析的计数记录数量。它不复制异常原文、请求路径或正文、模型输出、日志内容、密钥和认证信息；无法读取计数记录时用 `null` 表示未知。成功时不创建失败报告，并清理运行临时目录、应用进程和脚手架创建的容器。

`verify_failure_redaction.py` 在 PostgreSQL 双实例验收中、执行者暂停期间注入受控失败，把虚构密钥和正文同时放入异常、临时应用日志和模拟 HTTP 5xx 记录。验证退出码为 1、结构化报告留存、5xx 计数为 1、场景／进程／时间／类别／文件／行号齐全，并检查报告及标准输出和标准错误均不包含虚构密钥和正文。报告文件和目录的权限分别为 `0600` 和 `0700`；执行者已恢复并退出，临时运行目录已清空。随后该脚本再次运行正常的本人取消场景，确认未新建失败报告、原报告仍存在、应用进程退出且临时运行目录为空。验证器通过只记录容器 ID 的本地包装器检查两次创建的 PostgreSQL 容器均已删除，不保存 Docker 参数或数据库密码。验证脚本输出保留的报告路径。两种正常拓扑的三个场景均完整复跑，退出码均为 0，HTTP 5xx 计数均为 0。

## 复跑

需要 Docker、已拉取的 `postgres:16-alpine` 镜像以及后端锁定依赖。脚本使用 `--pull=never`，不会自动下载镜像。从仓库根目录设置 `C7_TASK_DIR` 为本任务的绝对路径；归档后将其指向归档后的任务目录。然后在 `backend/` 执行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python \
  "$C7_TASK_DIR/research/c7_task_acceptance/process_tasks.py" --topology pg-workers --cases scheduling
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python \
  "$C7_TASK_DIR/research/c7_task_acceptance/process_tasks.py" --topology pg-instances --cases scheduling
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python \
  "$C7_TASK_DIR/research/c7_scheduling_acceptance/run_pg_cancellation.py" --topology pg-workers
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python \
  "$C7_TASK_DIR/research/c7_scheduling_acceptance/run_pg_cancellation.py" --topology pg-instances
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python \
  "$C7_TASK_DIR/research/c7_scheduling_acceptance/verify_failure_redaction.py"
```

四项业务验收及脱敏验证均退出码 0。脱敏验证内部的受控失败进程按预期退出码 1。`uv run --no-sync ruff check ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_scheduling_acceptance` 通过。
