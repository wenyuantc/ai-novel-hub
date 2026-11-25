# C7 实施与验收清单

需求：[prd.md](prd.md)；设计：[design.md](design.md)；预研：[migration-research.md](research/migration-research.md)。开始前先由用户确认本方案，C1–C6 已收口，C8 保持独立。

## 1. 迁移互斥

- [x] 核查所有历史迁移的事务边界、SQLite 外键重建和 `0066`–`0070` 侧库／停机守卫；补真实竞争与中途失败用例，先确认现有路径不能满足要求。
- [x] 按设计改造 `backend/alembic/env.py` 的在线迁移，PostgreSQL 用同连接会话锁，SQLite 用显式写事务；兼容现有程序调用、升级、降级、离线 SQL 和测试配置。只在迁移专用连接关闭 SQLite FK，并在提交前核验外键。
- [x] 对空库和有业务数据的旧版本库分别用三个独立进程竞争，统计实际迁移执行次数；覆盖 PostgreSQL 16 和 SQLite、等待超过 5 秒、迁移中途异常、持锁者被杀、事务回滚与干净重试。验证失败实例不启动 HTTP 服务，持续故障下所有实例均不服务。
- [x] 运行既有迁移往返、拒绝降级、SQLite 主库／租约侧库一致性测试及本任务新增测试；检查正常单进程升级和唯一 head。全量回归发现的 0036 外键状态测试已按 C7 契约修正并在 SQLite 复验；0068 写锁观察测试已在隔离 SQLite 和 PostgreSQL 中复验。完整回归另见第 4 节。

## 2. 镜像与部署

- [x] 在 Dockerfile、Compose 和 `.env.example` 接入 `UVICORN_WORKERS`，默认 1，验证正整数、进程信号、迁移失败退出。提供 PostgreSQL 两实例示例，分别设置可直达的 TCP 通告地址，共享密钥和备份目录；默认部署仍可直接启动。
- [x] 实际构建镜像，分别用未设置与 4 worker 启动；按实例和 PID 去重检查有效请求／调度租约、健康状态和真实请求。验证容器内部动态 peer 端口的互通，外部不能访问内部监听。标准 Docker 的隔离 Linux 机器通过；OrbStack 内置 Docker 的独立 bridge 隔离失败，见[容器验收](research/c7_container_acceptance/verification.md)。
- [x] 依照新文档从零启动两个实例与 PostgreSQL；跨主机共享目录若有真实环境则测互见和文件锁，否则记录未覆盖及部署前自检要求，不声称该文件系统已经验证。[最终容器验收](research/c7_container_acceptance/verification.md)使用标准 Docker 同机演练；OrbStack 内置网络未通过，不能直接使用默认隔离假设。

## 3. 真实多进程验收

- [x] 搭建可重复执行、只管理自己创建资源的测试脚手架：临时 SQLite、PostgreSQL 16 容器、受控模型和独立应用进程；支持三种部署形态、点对点请求、任务归属判定、定时屏障及强杀／断通道。
- [x] 按 `design.md` 矩阵逐条执行父任务 AC1–AC10，SQLite 额外执行 AC11。对作者与协作者分别验证断通道结果，并用真实 HTTP/SSE、数据库事实和模型调用计数作断言。失败项保留日志，安全脱敏；不能用同进程双应用用例冒充真实进程。[验收总记录](research/verification.md) 已覆盖业务场景；诊断保留白名单日志摘要，未知原始错误细节不保留。
- [x] 测量强杀恢复、取消以及设置生效时间，记录健康数据库和争用条件；按实际证据界定上限和已知例外。[任务与流](research/c7_task_acceptance/verification.md) · [调度与取消](research/c7_scheduling_acceptance/verification.md) · [设置与排空](research/c7_operation_acceptance/verification.md)
- [x] 按新增 README 操作完成一次当前版本的多进程排空、备份、停服、迁移、启动和恢复受理演练。[运维验收](research/c7_operation_acceptance/verification.md)通过真实应用接口执行对应步骤；代理维护模式和 Compose 编排未在同一演练实操。旧版 `0043` 迁移及旧代码快照回滚仍归 C8。

## 4. 文档、回归与收口

- [x] 验收通过后更新 README 的单进程限制、镜像参数、SQLite／PostgreSQL 条件、私网和共享卷、反向代理、时间参数及每进程 CPU 限流；逐处核查 `.trellis/spec/` 的单进程、单个／一个 worker、`--workers` 与多实例表述，保留仍正确的上下文。
- [x] 更新 `TODOS.md` 第 25 项的 C7 状态、验收记录与限制；C8 和父任务 AC13 不提前勾选。更新父任务进度及 C7 的逐项验收证据。
- [x] 在 `backend/` 运行 `uv run ruff check .`、相关迁移与多进程回归、`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20`；PostgreSQL 用专用隔离数据库，相关测试 `-n 0` 串行运行并报告跳过项。测试失败按实际原因处理，不降低门禁。[后端记录](research/backend-regression.md)
- [x] 在 `frontend/` 运行 `npm run lint`、`npm run build`、`npm test`。核对 `docker compose config`、Alembic head 和 `git diff --check`。收口时完成 Trellis 质量检查、必要规范更新与提交范围核对，不包含工作区其他任务草稿。[前端记录](research/frontend-regression.md)

完成前若发现任一部署形态、故障注入或全量门禁未通过，记录实际缺口并保留现有生产限制；不能仅凭单元测试、同机模拟或历史通过记录宣布多进程部署已获验收。
