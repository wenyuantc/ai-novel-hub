# 部署启动与在线迁移

## 1. 范围与触发

修改镜像启动、Uvicorn 进程数、Compose、Alembic 在线迁移、跨实例内部通道或数据库恢复时，须检查本契约。涉及数据库结构变化的升级须停机排空，新旧版本不能混跑；任务执行者租约和运行期锁仍由任务引擎负责。

## 2. 命令与配置

- `backend/docker-entrypoint.sh`：校验 `UVICORN_WORKERS`，执行 `alembic upgrade head`，成功后 `exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --workers ...`。
- `UVICORN_WORKERS`：正整数字符串；未设置时为 `1`，空值、零和非法值拒绝启动。默认 Compose 显式传递该变量。
- 根目录 `.dockerignore` 排除 `.env` 副本、SQLite 数据及旁车、备份和轮转日志，保留 `.env.example`；构建上下文规则有假数据验收。
- PostgreSQL Compose 就绪探针通过 TCP 执行 `SELECT 1`，避免将数据库初始化时的临时本地 socket 误判为对应用可用。
- `docker-compose.multi.yml` 是独立的 PostgreSQL 双实例示例；每个实例设置自身 `TASK_PEER_BIND_HOST`、`TASK_PEER_ADVERTISE_HOST`，`TASK_PEER_PORT=0` 由每个进程分别取得端口。
- `TASK_PEER_ENABLED`、`TASK_PEER_SOCKET_DIR`、`TASK_PEER_BIND_HOST`、`TASK_PEER_ADVERTISE_HOST`、`TASK_PEER_PORT`、`TASK_PEER_NETWORKS` 控制内部监听及允许网段。
- `0043` 旧版没有 `TASK_ADMISSION_CLOSED`。升级前须在部署层停止新受理、保留维护及验收通道，使用旧版全站任务监控与备份管理逐项确认排空。
- `python -m app.services.backups.sqlite_restore <snapshot> <destination> [--previous-lock <path>]` 由新版代码提供；它验证并冷恢复快照，不自动迁移。`0043` 旧代码没有此模块，恢复后须用匹配快照的旧代码启动。

## 3. 数据与生命周期契约

在线 Alembic 使用专用 `NullPool` 连接，读版本前先取得迁移互斥。PostgreSQL 用固定项目专用会话 advisory lock，锁、迁移和解锁必须在同一实际会话中；镜像启动使用的 `DATABASE_URL` 须直连 PostgreSQL 或使用会话池，不能依赖事务池在提交后继续分配同一会话。会话异常关闭可释放锁；提交确认不明时退出并重新核验真实数据库状态。

SQLite 仅支持同机进程共享同一主库文件，并遵守主库与租约侧库、生命周期锁的既有配对规则。迁移专用连接在事务外关闭外键、显式执行 `BEGIN IMMEDIATE`，在同一事务内检查版本并执行迁移；成功前运行 `PRAGMA foreign_key_check`。真实忙锁等待可重试，其他错误回滚并退出。应用正常连接仍按原规则启用外键。

迁移不得自行提交／回滚、进入自动提交块或修改 SQLite 外键开关。`backend/alembic/env.py` 在迁移执行期间检查连接事务与事务控制 SQL；历史 SQLite 重建迁移仅在外键开启时走自动提交块，迁移连接已在事务外关闭外键。`tests/test_c7_migration_connection_contract.py` 对迁移源码的已知直接建连接入口进行静态检查，既有 SQLite 生命周期守卫经审查可读取主库并锁住配对文件。**静态检查不能证明动态调用、任意辅助函数或独立进程不修改数据库；迁移评审仍须确认结构变更只使用 Alembic 连接。**

同一实例内的 worker 可共享私有 UDS；跨容器或主机使用能直达原进程的私网 TCP 地址时，多个 worker 需要各自可达的实际端口。`TASK_PEER_NETWORKS` 只校验监听、通告及出站目标地址，不限制入站来源。内部协议有鉴权但不提供传输加密；目标环境必须另行提供网络访问控制，并在有窃听风险的链路提供加密。不能仅凭端口没有发布或 Docker 网络标记 `internal` 就推断其他容器无法连接；应从不受信网络实测。共享 `BACKUP_DIR` 的文件锁也必须在目标环境验证。SQLite 不支持跨主机共享主库。反向代理须正确传输 SSE。

从 `0043` 升级协作功能时，先停止新请求、排空并验证备份，停掉全部旧写进程后再制作最终快照。回退前停止新版并另存新版库，然后恢复升级前最终快照和相应旧代码、原 `ENCRYPTION_KEY`；快照后的数据不会自动合并。PG 恢复到全新空库，SQLite 按 README 保留主库、WAL/SHM 及租约侧库／锁文件。旧版按作品读取全部生成记录，不能直接连接已经写入协作者记录的新库：隔离演练中作者实际读到了协作者的记录。

## 4. 校验与错误矩阵

| 情形 | 必须行为 |
| --- | --- |
| worker 数未设置 / 正整数 | 分别使用 1 / 指定进程数。 |
| worker 数为空、零或非法 | 非零退出，不执行迁移或启动 HTTP 服务。 |
| 并发在线迁移 | 后到者等待；取得锁后重新检查版本，不重复执行已完成迁移。 |
| 迁移失败、进程被杀、SQLite 外键检查失败 | 回滚未提交结构和版本；失败实例不启动服务。后继仅能从完整旧库重新迁移。 |
| 迁移尝试自行结束事务或自动提交 | 拒绝；不得放开外层原子边界。 |
| peer 地址不可直达或内部连接中断 | 沿既有 C4 流降级／受控结束，不把网络问题当作执行者失联证据。 |
| 不受信网络能连接 peer TCP | 不通过网络隔离验收；部署层实施访问控制并重新测试，不能以协议鉴权或未发布端口代替网络隔离。 |
| 升级前存在旧版排队、运行中或待审核任务 | 先在旧版排空；若已升级，核对具体任务及章节，不能假定取消受理等于完成，也不能补造旧任务身份。 |
| 直接降级协作版本 | 不作为运维回滚路径。即使没有新版写入，旧提案或中断任务也可能触发有损降级保护；拒绝时应保留数据并使用快照恢复。 |

## 5. 正例、基线与反例

- 基线：未设置 `UVICORN_WORKERS` 时只启动一个应用进程。
- 正例：同库三个实例启动，只有一个执行迁移，其他实例等待后读取已更新的版本。
- 反例：仅检查 `/healthz` 就声称数据库正常；该端点不检查数据库，还须通过真实业务请求及执行者状态验证。
- 反例：把多副本负载均衡名作为单个 peer 的通告地址，或者把容器私有 UDS 当作跨容器共享路径。
- 反例：只撤掉新版前端，让 `0043` 旧服务继续读取新库；旧版可能暴露协作者生成记录。

## 6. 必需测试

- `tests/test_c7_migration_concurrency.py` 和 `tests/test_c7_migration_postgresql.py`：空库与旧库三进程、长等待、失败回滚、持锁者退出、在线命令、离线 SQL 和事务边界。
- `tests/test_c7_migration_connection_contract.py`：现有迁移及直接连接创建的正反样例；对动态行为继续人工评审。
- `tests/test_c7_container_startup.py`、`tests/test_c7_container_diagnostics.py`：进程数、失败退出、信号、就绪判定和安全诊断。
- `tests/c7_migration_container_smoke.py`、`tests/c7_container_smoke.py`：隔离镜像的三实例迁移故障与恢复、默认及四 worker、双实例内部连通。记录实际命令、数据库版本和未覆盖环境。
- C7 验收的测试与演练应使用不含项目 `.env` 的隔离源码和临时数据库，显式指定临时 `DATABASE_URL`。
- 真实任务流、取消、排空、备份与设置跨进程验收见 C7 任务记录；完整 SQLite 与前端回归按项目质量规范执行，PostgreSQL 专项使用专用库串行运行。
- C8 的 `research/c8_drill.py` 和 `research/backup_drain.py` 分别覆盖真实旧应用产生的数据、两种数据库的升级与快照回滚、遗留任务、拒绝降级、并发迁移及活动备份；以该任务 `research/verification.md` 的版本和实际覆盖范围为准，不能把短暂观察窗口推断为永久状态。

## 7. 错误与正确方式

```python
# 错误：迁移中自行提交，后续失败会留下已提交结构。
op.get_bind().commit()

# 正确：只在 Alembic 提供的连接上执行，提交由外层迁移事务负责。
op.execute("ALTER TABLE example ADD COLUMN note TEXT")
```

```sh
# 错误：忽略迁移结果后仍启动服务。
alembic upgrade head; uvicorn app.main:app

# 正确：迁移失败即退出，成功后把信号直接交给 Uvicorn。
alembic upgrade head && exec uvicorn app.main:app --workers 1
```
