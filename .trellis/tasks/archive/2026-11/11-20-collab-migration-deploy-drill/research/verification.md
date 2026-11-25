# C8 隔离演练验收记录

状态：**C8 隔离验收通过**。双库主场景、异常退出冷快照、活动备份和旧任务矩阵均已在最终控制器下复跑；独立执行者只根据最终 README，用命令完成 SQLite 迁移及恢复，并通过网页操作和验证业务。旧 PG 容器的匿名卷可能未清理，新轮改用专属 tmpfs。未排空旧任务的治理限制和未覆盖环境见下文。

## 环境与复跑

- 旧应用、迁移与依赖来自 `4501f18da97502eb3e352bf85b1853bb0903d60e`（head `0043`）；新版来自 `b7b3d248a7462deedd0507275a0e6fd0a6ec8e0e`（head `0070`，含 C1–C7）。两端分别经 `git archive` 导出到专属临时目录，使用各自 `uv.lock`、Python 3.12 和 `uv sync --frozen --python 3.12`。不读取项目 `.env`，子进程仅继承必要的系统变量和新生成的测试配置。
- 旧版 `uv.lock` SHA-256 为 `5c089b8d0f7ba8f593557545980cbe8749fafa302c3469af5d6d8942bba97ae6`，新版为 `9b452dbd015244ced12447b9528a0f9453cde15ca9fd58eea43a2498b655f154`；最新两轮分别实际调用旧版与新版隔离虚拟环境的 `python --version`，均为 Python 3.12.10。本机事后核对控制器 Python 3.12.10、`uv 0.7.3`。较早运行只记录解释器大版本要求，不能追认其各副本的 Python 微版本。
- SQLite 使用临时文件；PostgreSQL 使用随机名称的专属 `postgres:16-alpine` 容器，本轮服务端为 16.15，端口只发布到 `127.0.0.1`。应用及 OpenAI 兼容假模型也只监听回环地址；所有账号、作品、模型凭据、密钥和数据库均为虚构测试数据。
- 较早轮次使用 `--pull never`，但未逐轮记录镜像 ID，不能把事后检查当作当时的直接证据。新增记录的 PG 完整轮次从本次容器读取到镜像 ID `sha256:c54a696c68013b2f8f42f479e00a2b6ad24c5ef10ee31cdfb8972cec3dad7eb7`；对该容器执行 `docker exec <本轮容器名> pg_dump --version` 和 `docker exec <本轮容器名> pg_restore --version`，两者均为 PostgreSQL 16.15。
- [c8_drill.py](c8_drill.py) 实际启动两个版本的独立 Uvicorn 应用、旧版真实任务引擎和本地假模型；各场景从独立快照恢复，旧版进程退出后才启动访问相应克隆的新进程。脚本结束时清理专属进程、容器和临时库。完整过程中每次模型请求检查测试凭据，不记录 token、请求正文或模型响应正文。
- 上一轮主脚本 SHA-256 为 `a055c3ec4c1d94c3b026f6d7da3b0cb2de427f5945c7d50042bfc2fd0b693a5f`：[SQLite](result-sqlite-final.jsonl) 完整运行 191.938 秒，[PostgreSQL](result-postgres.jsonl) 完整运行 324.533 秒；各有 12 次预期假模型调用、0 次意外调用。较早一轮 SQLite 复跑保留为 [result-sqlite.jsonl](result-sqlite.jsonl)，使用此前脚本 SHA `80d713267618730fab6a2a2ac19f059c2eebb713c0f80f0eafcf940924181385`，不包含最后追加的两项单迁移边界实验。
- 增加运行时版本记录后的主脚本 SHA 为 `502b6151d4a5272f43955e0ec8d387b0e58471b660ff025339c77bd5f4a9fbc4`；[SQLite](result-sqlite-environment-final.jsonl) 完整运行 202.953 秒，[PostgreSQL](result-postgres-environment-final.jsonl) 完整运行 318.396 秒，两轮各有 12 次预期模型调用、0 次意外调用，退出码均为 0。
- 增加 UTC 和锁等待证据的控制器 SHA 为 `e8e60d904bfa5e89b94c79aa0ee4b53ec5a6fd31f0952fa64d602bf1bc719c32`；[SQLite](result-sqlite-verified-final.jsonl) 于 `2026-11-25T06:45:15.852Z` 开始、运行 193.675 秒，[PostgreSQL](result-postgres-verified-final.jsonl) 同时开始、运行 265.094 秒；均退出 0，12 次预期模型调用、0 次意外调用。该轮保存了实际 UTC 时间、等待屏障、逐次迁移退出码和降级前后各表摘要。此后调整了 PG 容器存储及应用进程组，受影响场景已重新运行。
- 改用 tmpfs 后、增加进程组隔离之前，控制器 SHA `a18fe980e100349f5c0a5be8882fdc85f4c780113d898c00179f73f3acec450b` 的 PostgreSQL [主场景](result-postgres-tmpfs-final.jsonl)、[异常退出](result-crash-postgres-tmpfs-final.jsonl)与[备份](result-backup-postgres-tmpfs-final.jsonl)分别用时 193.629、146.911 和 22.710 秒；三次均记录数据目录为 tmpfs、匿名卷 0，并在结束后确认专属容器不存在。随后的进程组及监听 fd 修正版已另行复跑，见下文。
- 进程组及监听 fd 修正后的主控制器 SHA 为 `ff741091f72c4856e22197d081b0922234eccb1323a2006506b0579679db9609`；[SQLite](result-sqlite-socket-final.jsonl) 完整运行 201.760 秒，[PostgreSQL](result-postgres-socket-final.jsonl) 场景 210.111 秒并于 210.648 秒确认容器清理；均退出 0，各有 12 次预期及 0 次意外模型调用。PG 记录 tmpfs、匿名卷 0。
- 修复组长提前退出、超时子进程和环境继承后的**最终主控制器** SHA 为 `96ce03bda4d896789b011b06dc641aed58df6029e76964a60bcdea4bb50a1df9`。主场景 [SQLite](result-sqlite-guardian-final.jsonl) 201.898 秒、[PostgreSQL](result-postgres-guardian-final.jsonl) 场景 228.065 秒并于 229.027 秒确认容器清理，均退出 0，各有 12 次预期及 0 次意外模型调用。两份日志记录运行时 Python 版本、UTC 及结构／逐表摘要，PG 日志另记录容器镜像；此前轮次保留各自的实际 SHA，不追认此版。
- 同一最终控制器下，真实旧应用 `SIGKILL` 冷快照 [SQLite](result-crash-sqlite-guardian-final.jsonl) 160.321 秒、[PostgreSQL](result-crash-postgres-guardian-final.jsonl) 172.025 秒（含容器清理）；真实活动备份 [SQLite](result-backup-sqlite-guardian-final.jsonl) 20.740 秒、[PostgreSQL](result-backup-postgres-guardian-final.jsonl) 24.141 秒（含清理）；旧任务取消矩阵 [SQLite](result-cancel-sqlite-guardian-final.jsonl) 194.315 秒、[PostgreSQL](result-cancel-postgres-guardian-final.jsonl) 205.512 秒（含清理）；旧智能体矩阵 [SQLite](result-agent-sqlite-guardian-final.jsonl) 83.738 秒、[PostgreSQL](result-agent-postgres-guardian-final.jsonl) 93.299 秒（含清理）。10 个进程均退出 0；主场景首事件记录控制器自身 SHA，8 个补验日志的首事件同时记录各自脚本 SHA 和实际导入的控制器 SHA。取消矩阵使用旧应用运行时取得的未排空在线快照；智能体矩阵使用旧应用正常退出后的待审核快照，两者均不冒称来自 `SIGKILL` 冷快照。
- [backup_drain.py](backup_drain.py) 单独验证**旧版真实手动备份正在执行时**的排空；其 ASGI 包装仅在原 `database.snapshot_database` 入口设置等待屏障，放行后完整调用原函数。PG 宿主适配器仅转发到专属容器的原生 `pg_dump`。结果见 [SQLite](result-backup-sqlite.jsonl) 和 [PostgreSQL](result-backup-postgres.jsonl)。
- 追加以未来时间和本机虚构 SMTP 配置先开启、再关闭旧版每日备份后，[SQLite](result-backup-sqlite-final.jsonl) 26.334 秒、[PostgreSQL](result-backup-postgres-final.jsonl) 45.199 秒均再次走通活动备份屏障及恢复。这一补验不连接真实邮件服务。
- 使用当时主控制器重新执行的 UTC 轮次：[SQLite](result-backup-sqlite-utc.jsonl) 23.401 秒、[PostgreSQL](result-backup-postgres-utc.jsonl) 35.113 秒均再次通过；PG 另有容器归属及删除后查询确认事件。此处耗时是备份场景完成时间，PG 完成清理为 35.545 秒。
- 进程组隔离版备份 [SQLite](result-backup-sqlite-final-isolated.jsonl) 22.485 秒、[PostgreSQL](result-backup-postgres-final-isolated.jsonl) 场景 25.072 秒并于 25.389 秒完成容器清理；两次均完成计划备份开关、活动备份等待和 `0043` 恢复核验，PG 记录 tmpfs 和匿名卷 0。
- 控制器监听 fd 修正后的备份 [SQLite](result-backup-sqlite-socket-final.jsonl) 22.016 秒、[PostgreSQL](result-backup-postgres-socket-final.jsonl) 24.136 秒并于 24.784 秒完成容器清理；两轮均验证旧版计划备份开启后关闭、活动备份等待及恢复，PG 记录 tmpfs 和匿名卷 0。
- [legacy_cancel_matrix.py](legacy_cancel_matrix.py) 在相互独立的快照克隆上分别检验旧分析、批量、智能体和章节任务的本人／管理员取消与部分重发：[SQLite](result-cancel-matrix-sqlite-final.jsonl) 196.936 秒、[PostgreSQL](result-cancel-matrix-postgres-final.jsonl) 270.433 秒，均完成 9 个场景且意外模型调用为 0。更早的两轮 [SQLite](result-cancel-matrix-sqlite.jsonl) / [PostgreSQL](result-cancel-matrix-postgres.jsonl) 使用 SHA `81ce8dd32cb9cc385e80a10c7fae486b966aadebee5de2d390235d1ea338b854`，只观察重发任务到运行中，并不包含最终终态。
- [legacy_agent_followup.py](legacy_agent_followup.py) 另核对旧智能体待审核任务的继续、审核与同会话重发：[SQLite](result-agent-sqlite.jsonl) 91.138 秒、[PostgreSQL](result-agent-postgres.jsonl) 134.347 秒，均完成 5 个独立场景且意外模型调用为 0。
- [legacy_crash_drill.py](legacy_crash_drill.py) 单独从真实旧应用在模型阻塞时被 `SIGKILL` 的冷库快照验证遗留任务。早期 [SQLite](result-crash-sqlite.jsonl) 154.584 秒、[PostgreSQL](result-crash-postgres.jsonl) 200.235 秒，使用脚本 SHA `bf711e7fa58dd73b9d36ff0c9ba1aa137d180a67f9d9bc99e2dfe647c9f89b76` 和当时主控制器 SHA `e8e60d904bfa5e89b94c79aa0ee4b53ec5a6fd31f0952fa64d602bf1bc719c32`。追加旧端口及控制器／模型验证后的最终 [SQLite](result-crash-sqlite-port-final.jsonl) 155.164 秒、[PostgreSQL](result-crash-postgres-port-final.jsonl) 163.845 秒，均退出 0、意外模型调用为 0；PG 在 164.422 秒确认容器清理。最终脚本和主控制器 SHA 见下表。
- 上面列出的早期 `result-cancel-matrix-*-final.jsonl` 和 `result-agent-*.jsonl` 轮次运行时导入的是当时的主控制器 SHA `a055c3ec4c1d94c3b026f6d7da3b0cb2de427f5945c7d50042bfc2fd0b693a5f`；其自身脚本 SHA 以各结果文件首行为准，不能追认随后增加的 Python 微版本及镜像身份记录。最终 `guardian-final` 轮次的控制器 SHA 由各日志首事件直接记录。
- [prepare_old_frontend.py](prepare_old_frontend.py)、[prepare_new_frontend.py](prepare_new_frontend.py) 与 [prepare_maintenance_proxy.py](prepare_maintenance_proxy.py) 用于独立复核夹具，分别从固定提交安装真实前端，以及创建只监听回环地址、由临时标记文件启停的测试维护代理。它们不构成生产代理配置。另在第二个独立夹具实测：开启代理后，已登录普通作者创建作品返回 503，已登录管理员保存备份设置返回 200；测试后专属进程组及目录已回收。
- 最终版独立复核曾发现旧应用的 PPID／PGID 均指向 `--prepare-only` 控制器，按该组停服使控制器及假模型一起退出；随后还发现控制器保留了旧应用监听 socket，应用退出后端口仍开放。这些夹具轮次不计为完成。主控制器现以独立会话启动各应用及并发迁移进程，并在子进程继承 fd 后关闭控制器副本。受控的[真实进程与回环模型检查](result-process-isolation.jsonl)中，应用进程组 SIGTERM、应用 SIGKILL 和上下文自动停服后，旧端口均拒绝连接，控制器均存活、本地假模型均返回 HTTP 200；SIGTERM 情形中的派生子进程也已退出。该检查的应用 HTTP 服务是临时测试替身，完整旧版应用由后续双库场景和独立复核另行验证。
- 主会话对 README SHA-256 `d1f43f9a32258a01acd7c54b20a939707438465d7ef1afedbc347eb44b2d093b` 复跑 [check_readme_commands.py](check_readme_commands.py)：28 个 Shell 块的语法检查通过；实际检查 SQLite 未 checkpoint 的已提交 WAL、单文件快照、0043／0070 版本、特殊路径、拒绝覆盖／错误版本与 SHA，并以假 Docker 检查 PG 正常及失败分支。
- 同一 README 版本的 [check_readme_postgres_compose.py](check_readme_postgres_compose.py) 在随机专属 Compose 项目的 PG16 tmpfs 容器中真实停止 app、执行 `pg_dump` 得到 1,745 字节快照，并恢复到独立空库，核对 `0043` 和虚构标记；专属项目及资源已清理。运行时镜像 ID 为 `sha256:c54a696c68013b2f8f42f479e00a2b6ad24c5ef10ee31cdfb8972cec3dad7eb7`。该最小虚构库只验证命令，不代替完整旧应用 PG 演练。复验命令及边界见[独立命令记录](readme-command-verification.md)。

从 `backend/` 执行（若任务移动，按实际路径修改 `c8_dir`）：

```bash
c8_dir=../.trellis/tasks/archive/2026-11/11-20-collab-migration-deploy-drill/research
set +x
c8_env=(env -i PATH="$PATH" HOME="$HOME" LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8)
for c8_name in DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_API_VERSION XDG_RUNTIME_DIR SSH_AUTH_SOCK; do
  if printenv "$c8_name" >/dev/null; then
    c8_env+=("$c8_name=$(printenv "$c8_name")")
  fi
done
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/c8_drill.py" --shape sqlite
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/c8_drill.py" --shape postgres
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/backup_drain.py" --shape sqlite
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/backup_drain.py" --shape postgres
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/legacy_cancel_matrix.py" --shape sqlite
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/legacy_cancel_matrix.py" --shape postgres
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/legacy_agent_followup.py" --shape sqlite
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/legacy_agent_followup.py" --shape postgres
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/legacy_crash_drill.py" --shape sqlite
"${c8_env[@]}" ./.venv/bin/python "$c8_dir/legacy_crash_drill.py" --shape postgres
```

这些命令使用 Bash 数组和白名单环境；`set +x` 避免 shell 调试输出连接配置。Docker 连接变量若已设置则原样传入，避免静默切换 daemon；Git 和一般应用不继承这些变量，PG 活动备份补验为调用容器内 `pg_dump` 而有意向该测试应用及适配器传入相同白名单。PostgreSQL 需要 Docker、支持容器 tmpfs 的运行环境和本地 `postgres:16-alpine` 镜像；脚本使用 `--pull never`，为专属数据目录设置 1 GiB 的 tmpfs 上限。每条 JSON 事件中的 `elapsed_s` 为自各脚本启动以来的累计秒数；本轮新增 `timestamp_utc`，记录事件的实际 UTC 时间。此前 JSONL 没有绝对时间戳，也没有可靠起止时间，不以文件修改时间追认。完整过程及实际耗时以各对应结果文件为准。

为全新执行者准备独立 SQLite 夹具时，可在单独终端从项目根目录执行下列命令；若任务移动，按实际路径修改 `c8_dir`。它会打印专属 `fixture.json` 路径后等待 `finish` 标记，不打印文件中的虚构密钥。保持该进程运行，在另一终端将路径保存到 `fixture_path`，依次准备旧前端和测试维护代理。代理最初未开启，执行者按夹具控制信息自行启停；环境信息不代替 README 的升级和回滚步骤。

```bash
c8_dir=.trellis/tasks/archive/2026-11/11-20-collab-migration-deploy-drill/research
env -i PATH="$PATH" HOME="$HOME" LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
  backend/.venv/bin/python "$c8_dir/c8_drill.py" --shape sqlite --prepare-only
```

在另一个终端重新设置 `c8_dir`，并将 `fixture_path` 设为准备进程输出中的 `fixture.json` 路径，再执行：

```bash
c8_dir=.trellis/tasks/archive/2026-11/11-20-collab-migration-deploy-drill/research
c8_tool=(env -i PATH="$PATH" HOME="$HOME" LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 backend/.venv/bin/python)
"${c8_tool[@]}" "$c8_dir/prepare_old_frontend.py" "$fixture_path"
"${c8_tool[@]}" "$c8_dir/prepare_maintenance_proxy.py" "$fixture_path"
```

旧前端准备命令只导出源码并安装依赖；维护代理命令统一启动旧前端及代理，并通过长驻监督进程持有它们的认证清理通道。若复核者已在夹具启动新版后端并将其回环 `new_app_url`、**实际监听进程** `new_app_pid` 写入私有 `fixture.json`，执行 `"${c8_tool[@]}" "$c8_dir/prepare_new_frontend.py" "$fixture_path"`；新版前端通过同样验证连接归属的测试代理访问新版后端。所有 backend 的监听 PID、端口和工作目录必须匹配该夹具；`lsof` 缺失或无法确认时失败关闭。

回滚到旧应用后，先保持夹具 `maintenance_marker` 文件存在，再向代理提供已恢复旧后端的回环地址及实际监听 PID；它先验证新目标，认证停止旧代理组，保持维护状态并更新 `old_frontend_url`、`maintenance_proxy_url` 与 `old_supervisor_control`。具体 `restored_old_url`、`restored_old_pid` 由本轮启动者提供：

```bash
"${c8_tool[@]}" "$c8_dir/prepare_maintenance_proxy.py" "$fixture_path" \
  --retarget "$restored_old_url" --backend-pid "$restored_old_pid"
```

结束时先由各启动者停止自己运行的应用，再从私有 `fixture.json` 取出监督控制文件路径（不要打印控制文件内容或令牌），分别请求监督者认证停止其所属前端及代理。没有新版前端时第二个字段可能不存在：

```bash
c8_old_control=$("${c8_tool[@]}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["old_supervisor_control"])' "$fixture_path")
"${c8_tool[@]}" "$c8_dir/fixture_supervisor.py" --stop "$c8_old_control"
c8_new_control=$("${c8_tool[@]}" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("new_supervisor_control", ""))' "$fixture_path")
if [ -n "$c8_new_control" ]; then
  "${c8_tool[@]}" "$c8_dir/fixture_supervisor.py" --stop "$c8_new_control"
fi
```

收到两者的成功确认后才在夹具目录创建 `finish`，并等待准备进程退出。认证失败、监督者异常死亡或退出结果未知时不对 JSON 中 PID 盲发信号；按报告的身份人工核查，不能声称清理成功。临时验证用的维护标记只在文件身份未变化时删除。准备前端还需要 `npm`、锁文件依赖及 `lsof`。真实故障检查涵盖组长先退出、派生子进程忽略 SIGTERM、错误凭证、无效帧、监督者 SIGTERM／SIGKILL、受控服务自退出、控制端口伪装和重复清理。监督者被 `SIGKILL` 或宿主断电时无法保证自动回收。

```bash
c8_dir=.trellis/tasks/archive/2026-11/11-20-collab-migration-deploy-drill/research
c8_tool=(env -i PATH="$PATH" HOME="$HOME" LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 backend/.venv/bin/python)
"${c8_tool[@]}" "$c8_dir/check_fixture_process_safety.py"
"${c8_tool[@]}" "$c8_dir/check_controller_safety.py"
"${c8_tool[@]}" "$c8_dir/check_postgres_cleanup.py"
"${c8_tool[@]}" "$c8_dir/check_application_process_isolation.py"
"${c8_tool[@]}" "$c8_dir/check_owned_process_cleanup.py"
"${c8_tool[@]}" "$c8_dir/check_proxy_interleaving.py"
"${c8_tool[@]}" "$c8_dir/check_fixture_services.py"
```

第二条命令为控制器导入安装 Python 审计钩子，先证明钩子能拦截 `.env` 打开，再导入本任务控制器；并向专属子进程的 stderr 注入随机假秘密与非零退出码，确认控制器错误和输出不含该秘密。此检查只覆盖控制器导入及该故障分支，子进程不继承 Python 审计钩子；实际应用与迁移子进程通过独立源码副本、缺失 `.env` 检查和白名单环境隔离。第三条命令用受控 Docker 响应验证容器创建取消、归属、删除与查询异常；不操作真实容器。其余检查分别覆盖真实进程隔离、超时／取消及守护身份、端口复用交错，以及固定新旧前端与测试代理的安装和清理。

控制器为每个自己创建的进程组保留私有 socketpair 和随机身份，由组内守护验证后自行向本组发送信号；组长先退出时仍处理组内后代，SIGTERM 无效后有界升级 SIGKILL，认证失败不盲杀。守护与目标分离，目标不继承守护使用的信号忽略设置及控制 fd。超时或取消时，即使子进程继承 stdout／stderr，清理也有界；[真实故障结果](result-owned-process-final.jsonl)包括超时、取消、创建期间取消、守护先死、错误身份和幂等停止，[真实应用替身与模型](result-app-process-final.jsonl)包括组长 SIGKILL、子进程忽略 SIGTERM、应用端口关闭及控制器／假模型继续存活。此机制只管理仍在本组的进程，主动脱离会话的后代以及控制器自身被 SIGKILL／宿主断电均不能保证自动清理。

前端监督控制口使用私有 0600 状态文件、随机挑战和双向 HMAC，先验证监督者再发送签名指令，响应也须验签；异常控制帧被拒绝。[监督故障检查](result-fixture-process-final.jsonl)覆盖非对象、截断、过长和错误身份、受控服务自退出、监督者 SIGTERM，以及 SIGKILL 后控制端口被伪装的失败关闭；伪装方没有收到明文令牌。旧／新测试代理先检查 PID、cwd 和监听地址，建立回环 TCP 连接后再次验证**该已建立连接**的对端属于目标 PID，然后才在同一连接发送业务 HTTP。两种强制交错见[代理测试](result-proxy-interleaving-final.jsonl)：初检后端口被接管时，无关服务可观察到一次 TCP 握手，但收到 0 个业务 HTTP 请求；连接已验证后目标退出并由他人接管端口时，无关服务的握手和 HTTP 请求均为 0。这是本机夹具有限场景的实测，不代表完全没有网络接触或证明生产代理安全。[完整夹具复跑](result-fixture-services-guardian.jsonl)使用固定提交的真实旧／新前端和本机替身后端，验证错误 PID／端口／cwd／已退出时不转发、被接管端口返回 503、维护状态下重定向以及认证清理；不等同真实应用升级验收。

PG 资源使用随机名称、本轮专属标签和容器内专属 tmpfs 数据目录，启动时通过 Docker inspect 确认数据目录为 tmpfs 且没有匿名卷；容器创建从发起 `docker run` 起即处于清理保护内。删除前需从成功的 Docker 查询和标签确认其归属，使用 `docker rm -f -v` 删除后再次查询确认不存在；tmpfs 随该容器消失。删除命令失败但容器已不存在时以查询结果为准；仍存在、标签不匹配或查询失败均报告随机名称及可用的容器 ID 并失败，不删除身份不明的资源。若创建中断且没有返回 ID，脚本短暂重复查询后仍找不到时报告创建结果无法确认和随机名称，不把短观察窗口声称为绝对消失。受控检查覆盖创建取消／超时、标签不匹配、删除失败仍存在、查询失败和已不存在；最终真实 PG 五组场景均已完成并查询确认本轮容器不存在。

旧 PG 演练曾使用自动创建的匿名卷，受控真实复现表明 `docker rm -f` 后匿名卷可能仍存在；该复现只创建并清理了自己的容器及卷。对本任务日志容器名／ID 只读查询 Docker events，仅能关联部分容器，未找到可证明属于它们的历史卷挂载。**旧轮可能留有无法归属的匿名卷，数量未知；没有删除任何来源不明的卷，也没有执行 prune。** 新轮使用 tmpfs 并检查零匿名卷，避免继续产生同类残留。

本轮脚本 SHA-256（如主会话随后修改 README 检查脚本，应重新核对）：

| 脚本 | SHA-256 |
| --- | --- |
| `backup_drain.py` | `398ad633a62ee3f41ee6a6d09009ee6dabd481131704011fda4c0ffb63e2af7c` |
| `c8_drill.py` | `96ce03bda4d896789b011b06dc641aed58df6029e76964a60bcdea4bb50a1df9` |
| `check_application_process_isolation.py` | `519f133eeb08819540f24865d57f6eb9e9ca9006ea7741dabe8a03a38831aa8f` |
| `check_controller_safety.py` | `964bcaa0ece8a59dae72ae9120aabd9a6bf46fc78af7a1bea14a623de9487463` |
| `check_fixture_process_safety.py` | `0b32099d80d0c8fc5a5bbf8d6e62ec07f0c6afd87078218b5e0b204cd52a9c24` |
| `check_fixture_services.py` | `047fd0da7062cd3287b4babe967b0f47e6ff6242328efd674cd1455387efd19e` |
| `check_owned_process_cleanup.py` | `71139f3d563cb06e2bd4443dd0bef911e032a8d44a1470f5f87c41ad29bb0fea` |
| `check_postgres_cleanup.py` | `e8cdc3671bbca4538f4c0a9c61fd13c937799dcb930128f001cf267d391b23b2` |
| `check_proxy_interleaving.py` | `51445525e304c718e164c7be861ed8bf10b0505d7ae679be6298fceda0e9939f` |
| `check_readme_commands.py` | `672558e2dacd98d02a71a00c4255c37c87839c391796f88bc5fe7a9b98c0d7b5` |
| `check_readme_postgres_compose.py` | `bf54c2580c37e4ba7f53010f31cbc271f783d7a8d95ec7d514dae3fff688e3fb` |
| `fixture_backend.py` | `f5f81ae2825566be5f29c0c52a1c3c4a6522f55a36e2288a3c88b4b56d7153ba` |
| `fixture_process.py` | `8efbb8677e99e89c7bf9a018f5023f515d3343e22f5f2d761129e08dccda6dd7` |
| `fixture_supervisor.py` | `83656f588db11cc09dc18a4706d68230b799584de093e398aa126c71a5045baa` |
| `legacy_agent_followup.py` | `cf64e368875b2f6ecdf3243ea55122b40733233c888d553b8c7f3d93c35c2768` |
| `legacy_cancel_matrix.py` | `a14bc466cf5d46a1bc876fb612fafac191285279e95ad811d1888d7a3ef19902` |
| `legacy_crash_drill.py` | `6dd02d473af6ecab0b8d92a0865b83d3e1c9199ec345cdfe78411090b1f6082d` |
| `prepare_maintenance_proxy.py` | `5a4ee0cb20721285e16efa4e57e1fb89afd2f247d2f95eeea0063e744eafa53d` |
| `prepare_new_frontend.py` | `f17b4dd9b45f0617acac722372f7f647e6e31d38ef6df0507db7478b1ebc78d3` |
| `prepare_old_frontend.py` | `aeaf59560415cc27f1d851ab50ee60940f7438e54deae77971a724aa30e377f2` |

主会话在固定 `b7b3d248a7462deedd0507275a0e6fd0a6ec8e0e` 的无 `.env` 隔离源码副本，以 `env -i` 和专属 SQLite 执行 `uv run --no-sync ruff check .` 及 `uv run --no-sync pytest -q -n 4 --dist loadfile --durations=20`：Ruff 通过，pytest **7519 passed、76 skipped、19 xfailed、21 warnings、0 failed**，pytest 耗时 2718.57 秒，外层 2726.5 秒；JUnit 7614 项、0 failures、0 errors、95 skipped（其中含 19 xfailed）。跳过项不计为通过；临时目录已清理。

此前一轮检查覆盖 14 个研究脚本、38 份 JSONL、5 个 Shell 块和 47 个链接；这是当时的快照。最终检查中 20 个研究脚本 Ruff、格式和 Python 内存编译均通过；7 个 Shell 块通过 `bash -n`，62 个本地链接存在，55 份 JSONL 可解析。最终 10 份双库日志事件均含实际 UTC；两种数据库各 5 组降级前后的版本、结构和逐表摘要一致，已知虚构密码、模型密钥和连接字段的脱敏扫描无命中。五个最终 PG 容器均经 Docker 查询确认不存在；无关资源未清理。不能将旧轮时间或 SHA 追认到新轮。

## 旧版真实数据、排空与快照

两种数据库均通过旧版 HTTP 接口、任务引擎和受控假模型生成 2 个普通用户、3 个作品、模型配置与加密凭据、完成的章节生成记录、智能体会话与提案、知识库文档、批量任务、分析任务及章节任务；额外有旧版引导的管理员。首次优雅停止一个受模型屏障暂停的旧版任务，随后启动旧版确认其状态为 `interrupted`。

旧版继续运行时，受控假模型令同项目两个分析分别处于 running、queued，令另一作品一个章节生成处于 running、另一个处于 queued，另有 queued 知识库索引和两个 paused 任务。SQLite 和 PG 在未排空快照时，管理端计数均为 queued=3、running=2、paused=2、succeeded=2、interrupted=1；生成中章节和各任务均由旧版真实操作产生，没有直接插库伪造。

脚本用回环测试代理拒绝新的普通业务 POST、保留维护操作；旧版管理接口把 `schedule_enabled` 保存为 false，并确认备份概览无活动执行。用户分别取消旧排队章节和旧知识库索引，放行已受理分析及生成，取消两个待审核任务；两种数据库的全站排队、运行、待审核计数均归零，原 interrupted 保留供人工核对。先制作在线快照并恢复到独立目标核验 `0043`，然后停止旧应用，再制作并验证最终快照；**停服后快照是实际回滚点**。测试代理只模拟入口控制，没有验证生产反向代理。

活动备份补验另从旧版 `POST /api/v1/admin/backups/runs` 发起手动备份：屏障处看到 `active_run.status=running`，因此不能认定已排空；放行后备份 `succeeded`、`active_run=null`，下载文件的字节数和 SHA-256 与备份记录一致，在隔离目标恢复为 `0043`，测试作品存在。SQLite 及 PG 的早期轮次分别通过；屏障之外的备份实现没有替换。早期脚本 SHA-256 为 `070b0f9c0894f8c9b668c89842d3f0ee403771f1941da23a7e483af89b889981`，追加计划备份开关对照后的脚本与复跑结果见上文。

SQLite 快照使用 Python `sqlite3.Connection.backup`（可包含已提交的 WAL 内容）；恢复旧快照时，先移存新版主库及其 WAL/SHM、租约侧库与锁文件，再用**新版源码**执行 `python -m app.services.backups.sqlite_restore <旧快照> <原主库路径> --previous-lock <移存的锁文件>`。不运行新版 `upgrade head`，随后启动 0043 旧版应用。

PG 手工快照及恢复等价于以下命令，实际通过专属容器中的 PG16 客户端执行，`snapshot` 和目标数据库均是本轮独占资源：

```bash
docker exec "$container" sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -Fc "$1"' \
  sh "$source_db" > "$snapshot"
docker exec "$container" sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" exec createdb -h 127.0.0.1 -U "$POSTGRES_USER" --maintenance-db=postgres --template=template0 -- "$1"' \
  sh "$new_empty_db"
docker exec -i "$container" sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" --exit-on-error --no-owner --no-privileges --dbname="$1"' \
  sh "$new_empty_db" < "$snapshot"
```

## 正常升级、隐私反例与快照回滚

SQLite 和 PG 均从真实旧库一次迁移到 `0070`；旧生成记录的 `actor_user_id` 均为作品作者，已有 1 条智能体提案和 2 个模型配置均完成 0047／0049 身份回填。新版中旧作者登录、读取原作品与章节成功；真实邀请并接受编辑者后，作者和协作者各发起一次章节生成，均成功，本地假模型验证收到原测试凭据。

两端均在虚构作品 1 的章节 9 上记录了作者用户 2 与协作者用户 3 的结果：新版作者、协作者各自 `GET /api/v1/projects/1/chapters/9/generation-logs` 返回 200，列表分别只含本人记录 ID 6 和 7。停止新版后将新库恢复到**独立克隆**，启动 0043 旧代码并以作者登录，同一路径返回 200，作者实际读到 `[7, 6]`，包含协作者记录 7。新旧应用不同时连接同一库。这是对错误回退路径的隔离反例，不是可用的兼容方案。

两种数据库均在新应用停止后保留新版快照，再恢复停旧应用后的最终 `0043` 快照并启动旧代码。旧作者成功登录和读取作品；新版新增作品不在旧快照中；使用原 `ENCRYPTION_KEY` 发起的新任务成功，且本地假模型验证收到原测试凭据。PG 恢复到全新空库；SQLite 使用上述冷恢复工具，保留被移存的新版文件组。恢复后没有自动合并升级后的新增数据。

## 异常退出后的冷快照

旧应用先以真实 HTTP、任务引擎和本地模型建立一条 running 分析、同项目一条 queued 分析、一条 running 章节生成及一条 queued 章节生成，同时保留 queued 知识库任务和两个 paused 任务。两次模型调用都到达屏障且尚未放行时，只对本次创建的旧应用进程发送 `SIGKILL`，并等待操作系统确认退出码 `-9`。最终轮 SQLite 的终止和冷快照时间分别为 `2026-11-25T07:12:22.038Z`、`2026-11-25T07:12:22.139Z`；PG 分别为 `2026-11-25T07:12:24.889Z`、`2026-11-25T07:12:25.294Z`。两轮均在旧应用上下文尚未退出时确认旧端口拒绝连接、控制器仍存活且控制器内假模型返回 HTTP 200；原两次模型屏障仍未放行。旧进程终止之后才放开原屏障；PG 同时确认源库其他客户端连接数为 0。

在旧进程退出后直接读持久库，两个 running、三个 queued、两个 paused 和两章 generating 均与终止前一致；此后才制作独立 `0043` 冷快照并恢复验证，状态与源库一致。先前的优雅停止对照则使在途旧章节任务变成 `interrupted`，不能将两种退出方式混为一谈。

将冷快照分别恢复成独立副本、迁移并启动新版后，两种数据库均实测了观察及重启、批量继续／结束、本人取消旧排队分析、本人／管理员取消旧排队章节、重试中断任务和重建旧知识库索引。两个数据库各自在首次观察约 10 秒、重启后再观察 8 秒的窗口内，旧分析仍为 running／queued，后继分析 8 秒仍 queued 且没有模型调用；旧运行章节任务变为 interrupted，章节仍 generating。具体 HTTP 及后续状态见下表和上述冷快照日志。旧分析管理员取消／重发矩阵来自旧进程存活时的在线快照；最终智能体专项矩阵的快照在旧应用正常退出后取得。两者均不冒称来自强制终止冷快照。

## 未排空升级的实际观察

以下结果分别在 SQLite 和 PG 的独立克隆上取得；每个取消、继续和重发操作使用自己的克隆。记录的窗口不代表无限时间内的永久状态。

| 旧版快照中的状态与操作 | 新版实测 |
| --- | --- |
| running 分析、同项目 queued 分析 | 新版运行约 10 秒后仍分别是 running、queued；同串行键新分析受理后 8 秒仍 queued，未调用模型。停止并重启新版后再观察 8 秒，旧两任务仍为 running、queued，模型调用为 0。 |
| running 章节生成 | 新版启动后任务为 interrupted，但该章节仍为 generating；不能把任务中断当作章节已安全复位。 |
| queued 章节生成（无旧 claim） | 本人取消返回 409，观察 2 秒后仍 queued、章节 generating；重新发起返回 409。管理员取消返回 202 仅为受理，观察 12 秒后仍 queued、章节 generating，重新发起返回 409。 |
| paused 批量继续 | 返回 400，提示「模型绑定无效或已失效」，无错误码，仍 paused，未调用模型；与旧调研中「必定回队列」的推断不同。 |
| paused 批量提前结束 | 返回 200，状态变为 succeeded；操作前后模型调用数均为 0。 |
| queued 旧分析取消和重发 | 本人取消返回 200、变 cancelled；独立副本中管理员取消返回 202、12 秒后 cancelled，再次发起分析返回 202，8 秒时 running，放行假模型后 succeeded。 |
| running 旧分析取消 | 本人取消返回 409、2 秒后仍 running；管理员取消返回 202、12 秒后仍 running。此时未证实收尾，不能把 202 当成完成。 |
| paused 批量取消和重发 | 本人取消返回 200、2 秒后 cancelled，重新发起返回 202，8 秒时 running，放行假模型后 succeeded；独立副本管理员取消返回 202、12 秒后 cancelled。 |
| paused 智能体取消和重发 | 本人取消返回 200、管理员取消返回 202，独立副本中分别观察 2 秒、12 秒后均 cancelled。本人取消后同会话新消息返回 202、调用模型并 succeeded；原提案仍 proposed，不能把任务取消当成提案已处理。 |
| paused 智能体未取消时 | 同会话新消息、直接继续、直接结束、整轮拒绝提案分别在独立副本返回 409；旧任务仍 paused、提案仍 proposed，未调用模型。 |
| 原 running、启动后 interrupted 的旧章节 | 本人及管理员取消在独立副本均返回 409；2 秒后仍 interrupted，章节仍 generating，直接重新发起返回 409。 |
| interrupted 旧章节任务重试 | 返回 409，错误码 `task_actor_unavailable`，仍为 interrupted。 |
| 旧版 queued 文档索引 | 新版启动后旧任务和文档均为 failed；对原文档重新发起索引返回 202，新索引任务 succeeded。 |

## 降级保护与并发迁移

- 含新版生成记录及协作邀请的库停服后对克隆执行 `alembic downgrade 0043`；SQLite 和 PG 首先由 0061 的 AI 用量保护拒绝。版本保持 `0070`；SQLite 比较 `sqlite_master` 和全部 135 张表的逐行摘要，PG 用固定 `--restrict-key` 的 `pg_dump --schema-only` 比较结构，并比较 134 张应用表的行数与逐行摘要，前后无变化。早期 JSONL 只保存运行时比较的布尔结果，并未保留前后结构或逐表摘要；最终轮在每次拒绝的事件中保存 `before`／`after` 版本、结构 SHA 和各表 `[行数,数据摘要]`，两库各 5 组均一致。该结果不声称更早的 0052 / 0044 保护都在此完整降级中被触发。
- 新版代码仅迁移、不启动应用写新数据：只含旧完成任务与生成记录的基线可从 0070 降回 0043；另一个有真实旧版智能体提案的库在 0047 被拒；带旧 interrupted 任务的库在 0049 被拒。拒绝的版本、结构和各表数据均未变化。两个数据库还分别对未排空旧库做单迁移实验：0046→0045 因旧生成中章节被拒，0051→0050 因旧未完成分析／索引被拒，版本、结构及逐行摘要均不变；这些中间迁移版本仅用于隔离实验，没有启动中间版本应用。
- 最终轮从 `0043` 冷库克隆启动两个独立新版迁移进程；测试专用第三连接先持有相同迁移锁。SQLite 以 `BEGIN IMMEDIATE` 持锁，持锁约 5.748 秒时两进程都记录了等待 SQLite 写锁；PG 持 advisory lock，约 0.737 秒时数据库同时观察到两个 advisory lock 等待者。放行前版本均为 `0043`，放行后两端迁移退出码均为 `[0,0]`，两库本轮迁移执行次数均为 `[27,0]`，最终到 `0070`；两个真实应用都登录并读取作品，四表的行数及选定字段摘要前后一致：用户的 `id/username/is_admin`，作品的 `id/user_id/title`，章节的 `id/project_id/title/content/status`，生成记录的 `id/project_id/chapter_id/kind/status`。这不是四表全部列的比较，也不能与降级保护的逐表全列摘要混淆。该轮证明两个迁移进程等待**第三连接的测试屏障**，不单凭此推断某个迁移进程等待了另一个；也不代表跨主机文件锁或生产 Compose 编排。

## README 独立复核

[独立执行者记录](readme-independent-verification.md)在虚构的 SQLite 夹具中按 README 操作旧版页面、排空、在线及停服快照、升级 `0043→0070`、验证协作及新生成、备份新版库并恢复到旧版。恢复后的旧版可登录、读取作品和解密原模型配置，真实发起测试任务；解除本机测试代理的维护状态后，普通入口登录和生成也成功。在线备份、升级前快照和新版快照均在独立目标恢复核验；测试代理不证明生产代理的配置。

README 在独立复核中途修订：第一次停服 `0043` 快照使用原硬编码版本的命令，`expected_revision=0043` 命令随后在保留的同一 `0043` 快照上复验，大小和 SHA-256 一致；新版 `expected_revision=0070` 命令在真实停服的测试库上执行并恢复验证。当时核对的 README SHA 为 `c38ec2be4fc35e5b87e35c0790f7c5082e7faf76f581e3f3d0d3d6985681dfd1`，**不能说整个过程使用同一 README 哈希**。其后 README 又补充旧智能体提案的实测边界，最终版计划另行独立复做。独立执行者停止自己的进程后，主会话核验并停止测试前端与代理；夹具准备进程 exit 0，临时目录已删除。

后续独立复核中，执行者停掉旧服务的处理进程后发现端口仍被父进程占用，最初将其误认为 `uvicorn --reload` 重载父进程；事后确认该进程实际是演练夹具控制器，并曾与旧应用共用进程组及持有监听 fd。夹具现已修正，README 也补充停止真实 `--reload` 父子进程的说明。该轮及后续因夹具问题或文档修订而中止的轮次均不计为最终版完整复核，详见[独立执行者记录](readme-independent-verification.md)。

最终全新执行者在 SQLite 夹具中从头到尾使用 SHA-256 为 `d1f43f9a32258a01acd7c54b20a939707438465d7ef1afedbc347eb44b2d093b` 的 README，完成旧库排空、在线及停服快照恢复验证、`0043→0070` 升级、协作邀请和生成记录隔离、新版备份恢复验证、旧快照回滚、旧版任务及普通入口验证。旧任务取消后的一条智能体提案仍为 `proposed`，没有被误记为已处理。业务操作主要通过真实 HTTP API 完成；执行者另查运行中应用的 OpenAPI 获取路径和参数，浏览器只检查首页和登录表单。按「只看 README」的严格含义，本轮不能单独证明不查接口文档即可完成页面操作，也不证明 PostgreSQL、GPG 或生产代理。执行者停止自己的应用后，主会话认证停止专属前端和代理，准备控制器退出码为 0，临时目录已移除。详见[最终版独立执行记录](readme-independent-verification.md)。

后续另一名全新执行者仅依据同一哈希的 README 及应用页面，实际完成旧任务排空、备份下载及独立恢复、停服快照、升级、网页邀请及生成、新版备份及旧快照回滚；恢复后先通过仅供本机测试的验收入口执行任务，再解除维护通过普通入口生成。其未查看 OpenAPI、源码或既有演练记录。原备份目录经重启旧版并下载核对；每日计划保持原样关闭，但本轮虚构夹具为完成备份而临时关闭的加密开关未恢复开启，故未证明原加密备份配置恢复。执行者停止其应用后，主会话认证停止三个监督器；控制器退出码 0，临时目录已删除。完整步骤和边界见[网页独立复做记录](readme-independent-verification.md)。

## 范围与尚待确认

- 不涉及真实线上实例、生产代理维护模式、跨主机文件锁、GPG 加密备份或真实外部模型。旧版排空演练关闭定时备份并核对在途状态；测试代理与 API 路径不等同生产网络控制。
- 独立复核用本地测试代理会缓冲完整 HTTP 响应，并设 30 秒总超时；没有验证生产 SSE 的逐帧传输或超过 30 秒的模型流。
- 目标旧应用的 `SIGKILL` 已专门实测；若连演练控制器本身也被 `SIGKILL` 或宿主突然断电，无法保证 `finally` 执行。可定位的资源须按日志身份人工核查，身份不可证实时不能盲删。
- `0043` 的旧依赖、迁移和应用均在独立源码副本运行；没有修改产品代码或用直接插库替代旧任务造数。单个模型、数据规模和短暂观察窗口不能保证所有任务类型或生产负载的行为一致。
- 故障迭代保留说明：早期扩展排队场景时曾在生成运行中创建同作品章节，使旧任务因上下文变化失败；调整为先创建章节后重新执行。PG 首次结构比较因 `pg_dump` 随机 `\\restrict` 键产生摘要差异，固定 `--restrict-key` 后又逐表比较实际数据并重跑。早期新版任务查询遗漏 `task_key` 得到 422，控制器曾重复进入同一异步客户端上下文；调整测试请求及生命周期后复跑。独立复核首次受宿主 `C.UTF-8` 不可用影响，设置 `en_US.UTF-8` 后继续；一次本机 `shasum` 也因该 locale 失败，脚本哈希改用 Python `hashlib` 计算。前端监督首轮替身后端未消费 POST 请求体，导致代理读取出错；随后重复停止已清理监督者也被误报为未确认。该轮专属进程组核对无存活成员后删除临时目录，修正替身及停止状态后重新实跑。这些是演练数据或夹具问题，不是生产数据故障。
- 首次独立 README 复核覆盖 SQLite，实际 PostgreSQL 升级与回滚由本任务脚本完成；旧任务结果只覆盖表中具体任务类型和观察窗口。旧智能体提案在任务取消后仍未决，后续人工处理路径未在本次证明。

## 验收项证据映射

| 项目 | 目前证据 |
| --- | --- |
| AC1 | 两种数据库分别由 0043 应用真实产生多用户、作品、生成记录、提案、知识库以及 queued、running、paused、interrupted、succeeded 和 generating 章节；完整数量与任务 ID 见最终 JSONL。 |
| AC2 | 两种数据库均完成排空后的 0043→0070 迁移、旧数据与 0044/0047/0049 回填、新任务及协作邀请。活动备份边界另在两种数据库验证。 |
| AC3 | 上表及两种数据库的独立副本记录了受测状态、模型调用、同串行键后继、本人／管理员取消、继续及重发；真实 SIGKILL 冷快照复验的具体子集见上节，补充矩阵分别使用在线和正常退出后的快照。仅覆盖所列类型和有限观察窗口，旧智能体遗留提案的后续人工处理未证明。 |
| AC4 | 两种数据库均验证新数据降级拒绝、无新写入的允许／拒绝基线，以及 0046 / 0051 单迁移保护；保留拒绝前后结构与数据摘要。 |
| AC5 | 两种数据库均从停旧版后的最终快照恢复到 0043、启动旧应用并验证账号、作品、原密钥与任务；新版新增作品不存在。 |
| AC6 | 两种数据库均在隔离新库克隆观察到旧代码可启动且作者能看到协作者记录；README 的禁止说明由主会话维护。 |
| AC7 | 两种数据库均记录两个真实迁移进程等待第三连接持锁、各自退出码、27 次总迁移、双端业务读库及四表选定字段摘要；不外推为彼此等待、四表全部列或跨主机结果。 |
| AC8 | [网页独立 SQLite 复做](readme-independent-verification.md)只依据同一哈希的 README，用命令完成迁移、快照及恢复，业务操作和验证通过页面完成；另记原加密备份配置未恢复等环境边界。 |
| AC9 | 可复跑脚本、最终双数据库日志、旧任务补验、失败清理与独立复核已记录；未覆盖生产环境及文中所列情形。 |
| AC10 | README 的操作步骤和 `TODOS.md` 第 11、25 项已按双库及独立复核结果更新；未排空旧任务另列第 34 项。 |

## 归档路径复核

2026-11-25 归档后更新了任务上下文清单及本文复跑命令中的任务路径。`task.py validate` 通过；三份较大的规范或历史资料仍会被自动注入截断，需按清单提示直接完整阅读。C8 文档和 `TODOS.md` 的 99 个本地文件链接均存在。使用归档路径重跑 README 的命令检查和真实 PostgreSQL Compose 快照恢复，两项均通过，README SHA-256 仍为 `d1f43f9a32258a01acd7c54b20a939707438465d7ef1afedbc347eb44b2d093b`。这些复验不代替前述完整双库演练。
