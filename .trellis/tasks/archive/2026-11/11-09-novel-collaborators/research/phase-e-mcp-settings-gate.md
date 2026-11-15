# MCP 系统开关与物理发送门禁

## 目标和实现边界

手动确认的 MCP `tools/call` 必须在真实网络发送前检查当前持久系统开关。PostgreSQL 不同行锁不能阻止跨实例管理端关闭设置；使用固定双整数 advisory namespace 为无行、首次插入和后续修改提供同一提交顺序。

- 管理设置写：进程 manager 锁 → 管理授权写屏障及管理员 User → MCP 独占锁 → 设置 revision/审计 → commit → 无 await 发布。
- 真实发送：原 actor User → MCP 共享锁和持久设置读取 → 项目可见性 → 项目/任务/提案/插件/模型。共享事务只保留到真实 HTTP 响应头或传输确认关闭；不以其他节点尚未刷新的 `Settings.mcp_enabled` 决定发送。
- 读取发布内存快照：PG 共享锁内重读并无 await 发布，避免与其他节点的提交穿越读取和发布窗口；SQLite 短事务沿用现有 User/授权 no-op 更新写屏障。
- sessionmaker 固定启动时的 deployment 默认。无设置行或该字段未覆盖时使用此值；runtime 未确认、坏值、坏 actor 或不支持的事务隔离失败关闭。PG REPEATABLE READ 会在 advisory 锁前固定旧快照，因此拒绝用于实际设置门禁的此类事务。
- 提交确认未知时，本地 runtime 暂停准入并重新读取数据库；其他节点即使内存仍为旧值也以数据库持久值为准。两个应用实例的 deployment 默认必须在部署时一致，现有表并未存储全局 deployment 默认。

`agent/mcp_tools.py` 的真实 SDK 发送租约由 log_review 接入 `settings_gate.acquire_send`，并移除 Worker 基于过期内存的提前拒绝。工具发现阶段仍可能因为跨实例未刷新设置而错误拒绝重新启用的工具，已单独报告主代理；编辑者及自动外部调用未闭环前继续关闭。

## 验证

- 红灯：最初门禁专项因 `settings_gate` 尚不存在而在测试收集时失败。
- SQLite：`uv run pytest -q tests/test_mcp_settings_gate.py tests/test_system_settings.py --disable-warnings`，37 passed in 53.96s。包含持久开关优先于陈旧进程设置、首次无行的固定默认、未确认/损坏失败关闭、发送和设置两种先后顺序。
- SQLite 真 SDK/TCP + 运行时：`uv run pytest -q tests/test_agent_mcp_send_lease.py tests/test_system_settings_runtime.py --disable-warnings`，31 passed in 157.25s。覆盖关闭先零发送、发送先等待响应头、正文未结束时设置可提交、取消和断连。
- PG 两独立应用配置的 `tests/test_mcp_settings_gate_postgresql.py` 收集 8 项，涵盖真实 TCP、头前断开、取消、无行/并发首次写、提交确认丢失恢复和 REPEATABLE READ 失败关闭。首轮 5 failed、1 passed；5 个失败均在真实 Agent HTTP 入队时报模型绑定 400，未触及 MCP 门禁。其余两项在首轮启动后添加，尚未实跑。
- 根因：PG 的 `agent.transactions.session` 使用 `execution_options(isolation_level="REPEATABLE READ")` 创建新的 AsyncEngine wrapper；模型受理原先以对象 identity 对比原 AsyncEngine，因此同一 app 的受理会错误拒绝。该共享受理问题已交 agent_transactions 修复；不能为了测试绕过预检。等待修复后由主代理错峰重跑全部 PG 8 项。
- Ruff：本切片的生产文件及测试通过 `uv run ruff check`。
