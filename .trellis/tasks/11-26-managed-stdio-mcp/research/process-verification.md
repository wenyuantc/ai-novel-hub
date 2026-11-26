# stdio 进程层验收（2026-11-26）

| 环境 | 操作 | 观测结果 |
| --- | --- | --- |
| macOS，本地后端虚拟环境 | 运行 `uv run pytest -q tests/test_mcp_stdio_process.py` | 6 项通过。真实子进程完成协议握手、工具发现和工具调用；撤销发送租约后调用没有开始。 |
| macOS，独立 worker | 工具启动后强制结束 worker，检查工具和其子进程的 PID。另测调用中取消、初始化超时、并发槽耗尽、缺少可执行文件。 | 监督进程通过存活管道发现 worker 已退出；同一进程组中的工具及后代退出。新会话能在回收后取得并发槽。 |
| macOS，部署环境值 | 插件进程读取环境，使用普通短值 `DEBUG=1`、`LANG=C` 和测试密钥引用。 | 初始化/工具列表可用，worker 私有环境缺失，工具返回文本中的引用密钥已遮盖。 |
| Linux Docker，`ainovel-backup-acceptance:local`，`--init --network none` | UID 0 的真实 worker 通过 stdio 连接器列出并调用离线示例工具 `record` 一次，在宿主侧核对进程 UID、内核状态及退出后的 PID。 | 插件 UID 65534、`NoNewPrivs=1`，无法读取测试用 worker 环境变量；关闭会话后插件与其子进程均从 /proc 消失。 |
| Linux Docker，同一镜像与运行设置 | 登记的测试程序创建独立会话的后台子进程，再关闭 MCP 会话。 | 托管监督进程作为 Linux 子收割器回收了脱离原进程组的后代，/proc 中已无其 PID。 |

Linux 部署可以用容器 init 回收异常消失的 worker 留下的监督进程。非 root 服务启动的插件使用当前服务账户，部署者应对其可读文件、挂载和网络配置最小权限。本机进程组功能依赖 POSIX；macOS 的回收验收覆盖标准 MCP 进程组，Linux 额外覆盖独立会话后代。
