# C7 验收总记录

状态：**C7 本地验收通过**。隔离 Linux 的标准 Docker Engine 已通过同机独立 bridge 的内部端口隔离复验；本机 OrbStack 的同项测试未通过，不能在该环境直接依赖 bridge 隔离。后端第一轮完整回归有失败，第二轮因源码继续修正而主动中止；最终源码的第三轮在隔离副本中完整通过。C8 旧版协作升级与回滚仍未验收。

## 父任务跨进程验收

真实进程脚本使用本机 SQLite、PostgreSQL 16.15 容器和受控本地模型/MCP。PostgreSQL 两实例验收在同一宿主机的独立应用进程进行；容器部署另做同机网络验收。没有以本机容器冒充跨物理主机文件锁验证。

| 验收 | 当前证据 |
| --- | --- |
| AC1 存活任务及在途工作不误伤 | 普通任务和模型请求见[任务与流](c7_task_acceptance/verification.md)；备份见[系统设置与排空](c7_operation_acceptance/verification.md)；MCP 外部调用与作品删除意图见[在途工作](c7_inflight_acceptance/verification.md)。各自在三种形态验证。 |
| AC2 失联收尾 | [任务与流](c7_task_acceptance/verification.md)：三种形态中单章任务中断、占用释放并成功重试，原终态及记录在一个扫描周期后不重复；无完成收据的分析任务保留待核对，释放名额和串行键，后继任务完成。 |
| AC3 全站、用户并发及同键顺序 | SQLite 和 PostgreSQL 多 worker 见[任务与流](c7_task_acceptance/verification.md)；PostgreSQL 两实例及独立限额对照见[调度、取消和删除](c7_scheduling_acceptance/verification.md)。 |
| AC4–AC5 跨进程取消和作品删除 | SQLite 见[任务与流](c7_task_acceptance/verification.md)，PostgreSQL 两种形态见[调度、取消和删除](c7_scheduling_acceptance/verification.md)。 |
| AC6 作者／协作者流、撤权及断线 | [任务与流](c7_task_acceptance/verification.md)，包括 Agent `tool_start`、`proposal`、`paused`、撤权和两种断线处理。 |
| AC7 设置跨进程生效 | [系统设置与排空](c7_operation_acceptance/verification.md)，含注册、MCP 和并发设置的两端观测。 |
| AC8 引擎外状态 | [系统设置与排空](c7_operation_acceptance/verification.md)，含备份竞争、灵感、跨端导入和智能体会话互斥。 |
| AC9 排空开关 | [系统设置与排空](c7_operation_acceptance/verification.md)，含拒绝新建／重试／继续、原任务完成及备份。 |
| AC10 并发启动迁移 | [容器验收](c7_container_acceptance/verification.md)：最终迁移代码的镜像复验中，PostgreSQL 与 SQLite 各对空库和 `0034` 旧库执行三容器竞争；持续故障时均不提供 HTTP，解除故障后均启动且迁移只执行一次。 |
| AC11 SQLite HTTP 与忙锁 | [任务与流](c7_task_acceptance/verification.md)、[系统设置与排空](c7_operation_acceptance/verification.md)和[在途工作](c7_inflight_acceptance/verification.md)分别记录各自场景的 HTTP 5xx 与忙锁日志；不能推断未执行的其他负载。 |
| AC12 单进程完整回归 | [前端回归](frontend-regression.md) 1,849 项通过；[后端最终完整回归](backend-regression.md) 7,519 项通过、76 项按环境跳过、19 项 xfailed，0 失败。前两轮失败或中止的记录均保留；PostgreSQL 迁移专项另在专属库 22 项通过。 |
| AC13 协作旧版升级与回滚 | C8 范围，尚未执行；C6→C7 代码升级演练不能替代它。 |

## C7 部署与运维

- 最终迁移代码的真实镜像已完成 PostgreSQL／SQLite 四组三容器迁移故障及恢复复验。两个 Compose 健康检查已改为通过 TCP 执行真实 SQL，独立初始化测试证明它们不会把仅有临时本地 socket 的状态误判为就绪。
- 2026-11-25 在不含 `.env` 的隔离源码副本的 `backend/`，以 `python -m pytest -n 0 -q tests/test_c7_migration_connection_contract.py tests/test_c7_container_startup.py tests/test_c7_container_diagnostics.py` 复验最终连接契约及启动／诊断用例：63 项通过。清空业务环境并将默认数据库指向副本内临时 SQLite。后端及本任务研究脚本 Ruff 通过，两份 Compose 的 `config --quiet` 和入口 `sh -n` 通过；`alembic heads` 为 `0070 (head)`。
- PostgreSQL 迁移专项 `tests/test_c7_migration_postgresql.py` 在专属 PostgreSQL 16.15 库串行复验：22 项通过，无跳过或失败；实际命令及逐例结果见[后端回归](backend-regression.md)。
- 按[部署说明草稿](deployment-guide-draft.md)在独立 Linux 机器从空镜像缓存复制 `.env.example` 并填写随机配置，从零执行 Compose 配置检查、构建、启动、状态及日志检查；两个实例的健康和数据库设置接口四次请求均返回 200。浏览器中管理页面显示的两个存活进程与同一数据库查询一致；两个 peer 均完成受信 TCP 连接、外部探针正向对照及独立网络不可达检查。此检查本身不验证 peer 协议消息；业务流另见真实进程验收。脱敏[步骤结果](c7_container_acceptance/from-scratch-result-c7b24h6y.json)已留存；正式 README 在完整门禁通过后更新。
- 最终构建规则与恢复提示修正后，另在新建隔离 Linux 的标准 Docker Engine 上执行候选 README 的 1／4 worker 网络自检及从零双实例部署；接口、管理页面进程、受信 peer 和独立网络对照均通过。验收脚本修复归档路径后又在模拟归档深度重跑通过。[最终容器验收](c7_container_acceptance/verification.md)记录了实际命令差异、清理和未覆盖范围。
- C6→C7 停服升级在 SQLite 和 PostgreSQL 上演练，数据库迁移版本两侧均为 `0070`。备份校验及隔离副本数据检查见[系统设置与排空](c7_operation_acceptance/verification.md)；SQLite 副本未启动恢复后的应用。
- 当前代码的单独恢复演练对 SQLite 和 PostgreSQL 均完成排空、手动备份、校验、停服、恢复及重启两个真实应用；恢复后账号、作品、章节、原密钥下的模型配置与任务重新受理均通过。[恢复演练](c7_operation_acceptance/verification.md)未覆盖 Compose 编排、生产代理维护模式、GPG 加密快照、跨主机与失败后的人工回滚，也不能替代 C8 的旧版演练。
- 跨主机共享目录和其文件锁未在两台真实主机上验证，部署前必须自行验证。
- 当前 OrbStack 环境中，不同 Docker bridge 网络的容器可以直连未发布的 peer TCP 私网地址；独立实验中即便目标网络标记为 `internal` 仍可直连。[网络隔离验收](c7_container_acceptance/verification.md)在此环境失败，不能依靠未发布端口或 `internal` 网络声称已隔离。隔离 Linux 机器的标准 Docker Engine 复验中，独立 bridge 无法直连全部 2／8 个 peer 端口，探针正向对照和受信实例的重新连接均成功；该 Linux 机器自身可直连。OrbStack 隔离机器共享 Linux 内核，本次结果只适用于该配置，不能证明物理外网或其他主机的网络策略。内部协议会鉴权但不加密，`TASK_PEER_NETWORKS` 不检查入站来源。OrbStack 的观察与其[官方问题讨论](https://github.com/orbstack/orbstack/issues/1944)中的开发环境行为一致。
- 本轮独立 Linux 测试机器已删除，原有机器与 Docker context 保持不变；确认没有容器使用本轮镜像标签后，已删除该标签和镜像。
- 最终 Docker 构建排除规则在全是假数据的上下文中验证：默认 BuildKit 排除了 39 个敏感文件并保留 7 个示例／源码；最终应用镜像另行构建并通过隔离启动。[构建上下文验收](c7_container_acceptance/build-context-verification.md)
- PostgreSQL 在隔离库中验证了新库恢复、已有目标拒绝和恢复中途失败不改原库；该演练使用虚构最小表，未验证完整应用回退。[新库恢复验证](c7_container_acceptance/postgres-fresh-restore-verification.md) README 候选的 Bash 步骤另用假 Docker 验证了 7 个失败阶段和 4 种错误目标均不会继续启动，并用真实 Compose 的虚构配置检查单／双实例的目标校验。
- 失败诊断以白名单摘要留存；README 与规范已按本次验证结果和限制核对，正式 README 的恢复代码块另经故障注入及 Compose 解析检查。
