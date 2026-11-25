# C7 系统设置、引擎外状态与排空：真实进程验收

2026-11-25 在隔离环境运行 [run_operations.py](run_operations.py)，覆盖父任务 AC7–AC9 和 AC1 的备份子项。Python 3.12，PostgreSQL 16.15 容器；所有应用、数据库、模型和备份目录均由脚本新建。脚本不读取项目 `.env`，只清理自己的临时目录和 PostgreSQL 容器。

## 运行结果

| 形态 | 实际应用 PID | 设置收敛 | 结果与日志 |
| --- | --- | --- | --- |
| SQLite，同机两个独立进程 | 56486 / 56676 | 0.285 秒 | AC7–AC9 场景通过；[输出](result-sqlite-final.log) |
| PostgreSQL，同一 Uvicorn 双 worker | 58984 / 58983 | 0.451 秒 | AC7–AC9 场景通过；[输出](result-pg-workers-final.log) |
| PostgreSQL，同机两个独立进程 | 58981 / 59187 | 0.462 秒 | AC7–AC9 场景通过；[输出](result-pg-instances-final.log) |

最终复跑前后核对的 C7 `alembic/env.py` SHA-256 均为 `2f49468408f6c0ef12ba0cf70294845cd3676860ff27f91be278effd984b9eed`。表中时间是本次隔离环境从保存设置到两端内存值可观察的实测，并非异常环境下的承诺。PostgreSQL 双实例使用同宿主机上的独立 Uvicorn 进程、不同公开端口及本机 TCP 内部通道；不是两个独立主机或最终 Compose 拓扑。

每种形态均观察到：

- 在一个进程关闭注册、MCP 并调整全站和用户并发值后，对端下一次注册、插件列表、刷新及试用调用均返回 403，测试 MCP 服务未收到网络连接；两进程运行时值收敛到 3 和 2。
- 一次手动备份进入真实快照后，启动额外进程；新进程和原对端仍观察到备份运行中。放行后快照调用一次、只有一个 ready 副本，下载内容与数据库记录的 SHA-256 和大小一致。
- 从一个进程导出作品并生成导入预览，在另一进程确认成功；同一令牌再次消费被拒绝。同一灵感步骤的两个请求中，一个正在执行时另一个得到 409，最终仅保存一个生成轮次。
- 一个进程在会话操作占用提交后被测试屏障暂停；另一进程的并发消息等待，放行后第一条受理、第二条因已有活动轮次返回 409，数据库只有一条用户消息，模型没有收到未安排的调用。
- 排空期间两个进程均对新建、失败任务重试、待审核任务继续返回约定 503；原失败与待审核状态不变、被拒的新章节仍为草稿。原运行中和排队任务完成，待审核任务人工取消，活动数归零。
- 活动数归零且排空仍开启后，再发起手动备份；下载并校验 SHA-256，在隔离副本检查版本 `0070`、2 个测试用户和 4 个测试作品。随后停止所有应用、执行 `alembic upgrade head`、重新启动，确认排空仍开启且活动数为零；关闭排空后新任务成功。

SQLite 工作区轮次检查了全部受测 HTTP 请求以及 5 份应用日志，未观察到 HTTP 500，也未发现 `database is locked`、`SQLITE_BUSY` 或 `SQLITE_LOCKED` 日志。此结论只覆盖本脚本的 AC7–AC9 与排空重启请求，其他验收场景仍须各自检查。

## C6 到 C7 的停机升级

额外执行 `--from-c6`：从提交 `d9d234c2acb974bf91046b88fcfdabe3d813b413` 用 `git archive` 导出旧后端到临时目录，初始进程实际加载该目录代码。排空归零、备份并验证后，全部停止旧进程，使用当前工作区 C7 代码执行迁移并启动；脚本核对新进程加载路径、排空状态及重新受理。没有新旧进程混跑。

| 数据库 | 旧进程 | 新进程 | 结果 |
| --- | --- | --- | --- |
| SQLite | 53627 / 53949 | 54897 / 55194 | 通过；[输出](result-c6-sqlite-final.log) |
| PostgreSQL 16.15 | 53921 / 54077 | 56928 / 57331 | 通过；[输出](result-c6-pg-final.log) |

两侧数据库版本均为 `0070`：这是 **C6 到 C7 的代码和启动迁移路径演练**，不是跨 schema 版本升级。最终轮次 C6 `alembic/env.py` 的 SHA-256 为 `06e971abd3add578f7be88c0335c9a8ba5fa895d7c987b765af0bea401b5dc16`，C7 为 `2f49468408f6c0ef12ba0cf70294845cd3676860ff27f91be278effd984b9eed`。涉及 `0043` 的协作旧版升级与快照回滚仍由 C8 验收。

## 当前版本的备份恢复后启动

另用 [run_restore.py](run_restore.py) 对照候选 README 的「多实例排空、升级与回退」「下载文件与解密」及数据库恢复章节执行。读取的候选文件 `/tmp/ainovel-c7-regression.t0UYTenZ4CCB/README.candidate.md` 前后 SHA-256 均为 `0d7dd48585ad00ae11f32e8677f503505f5c0acc7b3678187953f7291f92b146`；运行的当前代码 `alembic/env.py` SHA-256 为 `2f49468408f6c0ef12ba0cf70294845cd3676860ff27f91be278effd984b9eed`。验收后核对的 SQLite 恢复模块 SHA-256 为 `68839655d5a7f62c1eabc17ca6fdc7a06057165608e7f678e97d00d5bd61098c`，文件修改时间早于本轮。测试使用新建的虚构用户、作品、章节和随机假模型凭据，未读取用户 `.env` 或使用现有数据库、卷。

| 恢复目标 | 停服前 PID | 恢复后 PID | 结果 |
| --- | --- | --- | --- |
| 同机 SQLite 文件 | 16859 / 16971 | 17232 / 17338 | 正式冷恢复 CLI，保存原文件组并建立新租约配对；[输出](result-restore-sqlite.log) |
| PostgreSQL 16.15 新空库 | 17040 / 17226 | 17953 / 18045 | `template0` 新库、原生 `pg_restore --exit-on-error --no-owner --no-privileges`；[输出](result-restore-postgres.log) |

两例都先让真实模型任务处于运行中，再开启排空并确认新任务返回 503，放行已受理任务、等待活动数归零。通过管理员接口执行不加密的真实手动备份，下载后以数据库副本记录核对大小和 SHA-256，并先在隔离副本验证；停止两个源应用后才执行实际恢复。SQLite 将主库及存在的租约、锁和附属文件移至同文件系统的专属回滚目录，调用 `python -m app.services.backups.sqlite_restore` 并传入旧锁；PostgreSQL 在同一专属容器中创建另一空库并恢复下载的 dump，不改写源库。

随后使用同一当前代码和原随机测试加密密钥执行 `alembic upgrade head` 并启动两个真实应用进程；两侧版本均为 `0070`。恢复后的管理员和作者能够登录，作品和章节内容一致，排空开关仍拒绝新任务。关闭开关后，新任务成功，本地假模型收到预期的随机测试凭据，证明恢复后的加密配置可用。ASGI 包装只提供 PID 和执行者观察，不替换备份、恢复或调度逻辑；所有临时库、容器和快照在成功后清理。以下为等效复跑命令，本轮另外指定了各自的 `--diagnostics-dir` 并用 `tee` 保存上述输出；在 `backend/` 运行：

```sh
c7_dir=../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_operation_acceptance
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python "$c7_dir/run_restore.py" --shape sqlite
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python "$c7_dir/run_restore.py" --shape postgres
```

这里没有通过生产代理设置维护模式；只由隔离测试控制写入。没有演练 Compose 停启与配置核对、跨主机卷、GPG 加密快照及错误密码、失败后的人工回滚，也没有演练 `0043` 旧版升级或回退。PostgreSQL 恢复目标是同一个专属容器中的另一个数据库，不是独立主机。恢复期间正常故障诊断沿用下节的脱敏机制。

## 失败诊断与脱敏验证

正常结束后脚本清理专属临时目录、应用进程及 PG 容器；受捕获的失败在清理前写入独立的持久 JSON，默认目录为脚本旁的 `failures/`，可用 `--diagnostics-dir` 指定。首选目录无法写入时退回系统临时目录下的 `ainovel-c7-diagnostics/`，控制台打印实际路径。文件权限为 `0600`。报告包含 UTC 时间、阶段及相对耗时、应用和命令 PID／退出码、异常类别、文件／函数／行号、HTTP 500 计数，以及按日志文件统计的 error、traceback、SQLite 忙锁和启动失败计数；命令 stderr 只匹配固定错误类别，未知情况归为 `command_failed`。不保存原始异常消息、命令参数和输出、应用日志行、环境变量、请求或模型正文，也不保存密钥或令牌。最终三形态和两次 C6 升级成功轮次均未创建各自指定的失败诊断目录。

用 [verify_failure_redaction.py](verify_failure_redaction.py) 进行了两种受控失败：在真实 SQLite 两进程启动后，把随机假密码、JWT、加密密钥、令牌、请求和模型正文放入异常及临时日志；再让子进程将同类标记写入 stderr 并以 17 退出。验证器逐值检查报告和控制台均不含这些标记，核对诊断路径、清理结果、PID、行号、错误计数及权限。两例分别留下[应用故障诊断](diagnostics/sqlite-20261125T024447-ab750105821a441e90aa818de36395f2.json)和[子进程故障诊断](diagnostics/sqlite-20261125T024450-3ca7935940654d05a15aaf37542c8403.json)。在 `backend/` 复跑（归档后把路径改成归档位置）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_operation_acceptance/verify_failure_redaction.py
```

诊断改造过程中的一次 PostgreSQL 双实例验收在初始迁移阶段失败，[当时的报告](diagnostics/success-pg-instances/pg-instances-20261125T024224-893ec3041f584665bf72e3ff5eaff6bd.json)仅能定位到旧版脚本 `command` 调用及 `RuntimeError`，未保存原始 stderr，故根因无法确认。随后补充了固定错误分类和从宿主端口执行 SQL 的数据库就绪检查，不能将其视为该故障已查明。验收脚本被 SIGTERM、SIGKILL 终止或主机突然中断时无法保证写出报告或完成清理。

## 测试包装及限制

- 临时 ASGI 包装只添加 PID 和已生效并发值的观察入口。为了定向将请求送到同一共享端口的指定 worker，测试把 Uvicorn 和 HTTP 客户端的 keep-alive 设为 300 秒，并在每次应用响应核对 PID；生产部署不依赖粘连或长连接固定到某个 worker。
- 备份的 `snapshot_database` 在调用原函数前加等待屏障；智能体的 `_claim_or_wait` 在原领取成功提交后加等待屏障。两处放行后均继续原实现，未替换租约、权限、调度、竞争或业务写入。外部模型为本地受控替身。
- PostgreSQL 备份仍使用真实 `pg_dump` 和 `pg_restore`。因为宿主机无 `pg_dump`，临时适配器通过专属 PostgreSQL 容器执行，并把真实输出送到应用提供的临时目标。`run_operations.py` 的备份校验读取恢复副本的版本、用户和作品；真正启动恢复目标应用的过程另见上节 `run_restore.py`。
- 没有使用最终生产镜像或跨主机共享卷；未验证真实线上服务。按 README 文本由独立操作者从零执行仍须主任务确认。AC1 中作品删除及外部工具调用进行中启动其他进程不属于本脚本，不能以备份结果冒充覆盖。

## 复跑

从仓库的 `backend/` 执行，先设置下列验收目录。脚本按 `.trellis` 祖先定位仓库，任务归档后只需将变量指向归档后的目录。PostgreSQL 运行需要本地已有 `postgres:16-alpine` 镜像及 Docker；脚本不会拉取镜像。

```sh
c7_dir=../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_operation_acceptance
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python "$c7_dir/run_operations.py" --shape sqlite
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python "$c7_dir/run_operations.py" --shape pg-workers
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python "$c7_dir/run_operations.py" --shape pg-instances
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python "$c7_dir/run_operations.py" --shape sqlite --from-c6
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. ./.venv/bin/python "$c7_dir/run_operations.py" --shape pg-instances --from-c6
```

三个脚本通过 `backend/.venv/bin/ruff check`，恢复脚本通过 `ruff format --check`；记录中的相对链接有效。恢复脚本的控制器导入使用 Python 审计钩子禁止打开 `.env` 后通过，应用和迁移子进程的工作目录为专属临时目录。当前结果只对应本次代码和上述形态，不代替迁移专项、全量回归或其他父任务验收项。
