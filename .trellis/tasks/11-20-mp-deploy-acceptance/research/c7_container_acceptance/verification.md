# C7 容器与部署验收记录

## 复现方式与范围

在项目根目录构建本次检出版本的镜像，以下命令所用的标签应当唯一：

```sh
C7_IMAGE=ainovel-c7-local-probe:unique
docker build -t "$C7_IMAGE" .
cd backend
uv run --no-sync pytest -n 0 -q tests/test_c7_container_diagnostics.py tests/test_c7_container_startup.py
uv run --no-sync ruff check tests/c7_container_diagnostics.py tests/c7_container_smoke.py tests/c7_migration_container_smoke.py tests/test_c7_container_diagnostics.py tests/test_c7_container_startup.py
uv run --no-sync python ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_container_acceptance/healthcheck_smoke.py
uv run --no-sync python tests/c7_migration_container_smoke.py --image "$C7_IMAGE"
uv run --no-sync python tests/c7_container_smoke.py --image "$C7_IMAGE"
```

三个手动脚本自行生成随机项目名、测试专用数据库和密钥，并为 Compose 显式使用临时 `--env-file` 和 `-p`；它们不读取项目 `.env`、不复用已有数据库或卷。迁移演练失败时，结构化报告默认保存到系统临时目录的 `ainovel-c7-container-diagnostics`，并将本轮隔离库快照保存到权限受限目录；可用 `--diagnostics-dir` 指定其他位置。成功则删除本轮容器、网络和数据卷。报告不包含原始日志、凭据、数据库 URL 或正文。验收仅覆盖本次配置和环境。

迁移日志仅提取尾部最多 1000 行中的固定级别、四位迁移版本变化及已知异常类别计数，每个容器最多保留 100 次版本变化。未知异常、异常消息、SQLSTATE、SQL 参数和业务正文均不保存；无法提取的信息属于未知，不能据此声称完整日志已留存。创建命令超时或非零、daemon 可能仍在处理时，即使当前按唯一标签清理和复查均为空，报告仍将清理标为未确认。

本轮镜像 `ainovel-c7-deploy-test:1795572115020` 的 image ID 为 `sha256:b31cdded551cc45f276b2179539e40608c55cc0c7be686031afc48545485600d`，镜像内和工作区 `backend/alembic/env.py` 的 SHA-256 均为 `2f49468408f6c0ef12ba0cf70294845cd3676860ff27f91be278effd984b9eed`。全部容器验收结束并确认无容器使用该标签后，主任务已删除这个本轮专用标签和镜像。

## 启动和迁移

- 入口和诊断定向测试 45 项通过，Ruff 通过；覆盖默认 1 worker、4 worker、非法值、迁移失败阻止启动、`exec` 和信号传递、失败脱敏、快照、资源清理以及 TCP 健康检查。
- 对两个 Compose 文件分别使用新 PostgreSQL 卷、阻塞初始化脚本进行真实测试：临时初始化服务器在 Unix socket 可以查询，但 TCP 健康检查失败；放行后正式服务器通过 TCP 查询并变为 healthy。两个临时项目及其卷均已清理。
- 使用同一镜像分别测试 PostgreSQL 16 和 SQLite 的空库、0034 旧库。四个场景均同时启动三个实例，在真实历史迁移执行 DDL 后注入异常：三个实例均退出、HTTP 不可达，数据库版本、结构和预置测试数据保持不变。撤销注入后，三个实例的 `/healthz` 和读取数据库设置的 `/api/v1/auth/config` 均返回预期结果，仅一个实例分别执行 70 次或 36 次迁移；SQLite 外键检查为空。四组演练均完成并确认清理。
- 受控故障验证了只保存测试 SQLite 文件、报告和快照的权限，以及不复制测试 env；另在隔离 PostgreSQL 16 以自造数据验证了 `pg_dump` 快照可以恢复。正常执行不保留数据库快照。最终版本的真实 PostgreSQL 旧库受控故障报告见 [migration-controlled-failure.json](migration-controlled-failure.json)：三个实例均有版本线索和 `RuntimeError` 计数、快照为 0600，提取快照时可核对自造的旧数据，未复制 env。该报告不是原始日志。

本轮受控故障的临时快照位于 `/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/c7-controlled-failure-bqaovx1m/`，只含本次生成的测试数据；不再需要时可删除该目录。正常验收成功路径没有留下快照。

## 独立网络访问：当前环境未通过

OrbStack 2.2.5 中，标准双实例脚本以默认 1 worker 启动后，两个公开 HTTP 端口的健康与设置请求均正常，两个实例的 peer DNS 和 TCP 可互通；但不共享应用网络的临时容器仍能直连 peer 私网 IP 和端口，因此脚本以 `external_network_reachable` 失败并退出码 1。安全脱敏的实际故障记录见 [orbstack-isolation-failure.json](orbstack-isolation-failure.json)，其中 `cleanup.confirmed` 为 true。

为继续收集 4 worker 证据，单次测试驱动暂存这项失败后继续其余步骤，最终仍以退出码 1 结束：每个实例均有 4 个独立进程，登记和跨实例 peer TCP 连接正常，但不共享网络的容器仍可直连这些端口。一次独立对照使用普通 bridge 和 `--internal` bridge 建立无端口发布的 TCP 服务，不同网络的测试容器都能连接。该现象与 [OrbStack issue #1944](https://github.com/orbstack/orbstack/issues/1944) 中报告的行为一致。

这只能证明在当前 OrbStack 环境中跨 bridge 不隔离，不能据此推断公网或其他宿主的可达性。peer 请求有挑战和签名验证，但 `TASK_PEER_NETWORKS` 不是入站源地址 ACL，通信也没有由此加密；部署仍需可信私网、网络访问控制或经验证的加密隧道。不得把未发布端口单独当成隔离证据。

## 隔离 Linux 机器中的标准 Docker 对照

另创建了本轮唯一的 OrbStack 隔离 Ubuntu 24.04.5 ARM64 机器，限制为 4 CPU、4 GiB 内存、16 GiB 磁盘，使用 `--isolated --isolate-network`，不挂载用户文件。启动内层 daemon 前，确认新机器进程与 PID 1 的网络命名空间相同但与现有 OrbStack Docker host 网络不同，且无已有 Docker socket 或 dockerd。其后在机器内安装并启动 Docker Engine 29.10.1 和 Compose 5.8.0，显式使用该机器自身的 Unix socket，确认 daemon ID 与原引擎不同。OrbStack 机器共享 Linux 内核，不等同于独立物理机或完整硬件虚拟机。

镜像从原引擎经 `docker save`/`docker load` 导入；导入后两端 image ID 不同，原因未进一步确认，但 12 层 RootFS digest 及 Config 全字段一致，容器内 Alembic SHA 与工作区一致。传入的 Compose、验收脚本和诊断模块也逐一核对 SHA 一致。最终验收在该机器内执行：

```sh
orb run -m <本次唯一机器名> -u root -w /tmp/c7-deploy/backend \
  env -u DOCKER_CONTEXT DOCKER_CONFIG=/tmp/c7-docker-config \
  DOCKER_HOST=unix:///var/run/docker.sock PATH=/usr/bin:/bin PYTHONUNBUFFERED=1 \
  /usr/bin/python3 tests/c7_container_smoke.py --image ainovel-c7-deploy-test:1795572115020
```

两个场景均退出码 0：默认每实例 1 worker 时，2 个 peer 端口从独立 bridge 不可直连；每实例 4 worker 时，8 个 peer 端口从独立 bridge 不可直连。外部探针先与自己网络中的临时 TCP 服务建立正向对照；探测后又从受信实例重连同一批 peer，均成功。应用两端的健康和数据库设置请求正常。机器本身分别能直接连接 2/2 和 8/8 个 peer 端口；这不代表公网可达。脚本确认本轮 Compose 资源、外部探针和临时标签清理完成。

此对照验证的是一个隔离 Linux 机器内 Docker bridge 的行为，不是真实多主机测试，也不能证明目标生产环境具备相同隔离策略。OrbStack 内置 Docker 的跨 bridge 失败结果仍然成立，不能以此对照覆盖。本轮唯一机器 `c7acceptf65bb606e579` 在文档演练及最终网络复验后已删除，并核对机器列表中不存在。

## 按部署文档从零启动

在同一隔离机器中，以 `git archive HEAD` 传入受控源码（显式排除 `.env`、数据库文件及备份目录），仅覆盖本任务的 Dockerfile、Alembic 环境、容器入口和双实例 Compose；测试 `.env` 和共享备份目录在机器内临时生成。使用随机独立项目名、显式 `--env-file` 和该机器自己的 Docker socket，执行部署草稿中的 `config --quiet`、`up -d --build`、`ps`、`logs --tail=100`，全部成功。两个实例的 `/healthz` 与 `/api/v1/auth/config` 均为 200，后者 JSON 的注册和 MCP 配置符合预期。本轮项目、卷、临时镜像标签和 env 已确认清理；安全脱敏的步骤状态见 [from-scratch-result.json](from-scratch-result.json)，构建和应用原始日志未保存。

该次演练直接生成测试 env，没有从 `.env.example` 复制填写，也没有逐项操作管理后台页面和执行草稿中的 peer 查询、正反向探测。peer 网络结果来自上文的独立脚本，后台状态来自其他独立业务验收；这些结果不能单独证明本次演练完整执行了草稿的每一步或只依赖文档即可完成。

随后在另一台本轮唯一隔离 Debian 12 ARM64 机器上补做了草稿的这些步骤。机器限制为 4 CPU、6 GiB 内存、20 GiB 磁盘；确认其网络命名空间与现有 Docker 引擎不同、没有预存 Docker socket，安装并启动独立的 Docker Engine 29.10.1 / Compose 5.8.0。执行前内层 Engine 的预载镜像数为 0。受控归档只包含 HEAD 源码及本任务明确列出的文件，在机器内复制 `.env.example` 为临时 `.env`，再填写随机数据库、管理员和加密配置；未读取用户 `.env`。

这次同一 Compose 项目依次通过 `config --quiet`、`up -d --build`、`ps`、`logs` 和两个实例的两种 HTTP 请求；两个应用镜像中的 Alembic 文件 SHA 与工作区一致。无头浏览器只访问测试机本地端口，在「系统设置 → 任务排空状态」看到两个存活进程；其主机名与 PID 同草稿 SQL 查询结果一致。两个 peer 端口均能从受信实例互连；独立 bridge 的探针通过同网 TCP 正向对照后不能直连两个 peer，探测后再次确认受信端仍可连接。浏览器未保存截图、凭据、页面正文或原始日志。

本次脱敏逐项结果见 [from-scratch-result-c7b24h6y.json](from-scratch-result-c7b24h6y.json)，保留了步骤状态、测试进程和端口，未覆盖旧报告。结束时确认临时容器、卷、自建网络及工作目录均为 0；唯一测试机 `c7accepta91a2942294f` 已删除并在机器列表中确认不存在。该演练依然只是同机隔离环境，不证明实际外网、跨主机共享文件系统或目标生产环境的网络策略。

在已准备独立 Docker Engine 的测试机器上可用以下命令重做；脚本只接受本次 C7 唯一机器名称，测试完成后不自动删除机器：

```sh
cd backend
uv run --no-sync python ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_container_acceptance/from_scratch_smoke.py --machine <本次唯一机器名>
```

脚本复跑时使用新建的专用目录及 Docker 配置，并将脱敏结果写入独立新文件，不能覆盖已有报告；若目录创建或清理结果不明，报告会标记清理未确认。原始成功结果保留不变。

上述两份历史从零演练记录使用的是本轮最终 `.dockerignore` 安全修复之前的规则及恢复提示变更之前的 `sqlite_restore.py`，不能作为这两处最终源码的镜像证据。后续脚本已显式覆盖工作区这两个文件并核对 SHA；最终规则另以独立假数据构建与实际应用启动验证。

目录占用、创建结果不明、所有权校验、Docker 配置隔离、结果文件不覆盖、浏览器本地访问与脱敏、页面和数据库进程匹配、peer 清理边界的 14 项定向测试通过：

```sh
cd backend
uv run --no-sync pytest -n 0 -q ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_container_acceptance/test_from_scratch_smoke.py
```

这次是单机双实例、默认每实例 1 worker 的从零构建演练；它不证明跨主机共享文件锁、生产网络或代理配置。

## 最终源码与候选文档复验

2026-11-25 新建本轮唯一的隔离 Debian 12 ARM64 机器 `c7accept0f743c4e85a8`，使用 `orb create --isolated --isolate-network --memory 6G --cpus 4 --disk 20G debian:12 <机器名>`。启动内层 daemon 之前，确认机器内进程和 PID 1 处于相同网络命名空间，且与现有 Docker host 命名空间不同，机器内没有预存 Docker socket 或 dockerd；随后仅在该机器安装并启动独立 Docker Engine 29.10.1、Compose 5.8.0。OrbStack 机器共享 Linux 内核，这不是独立物理机或多主机环境。

比较网络命名空间时，曾在现有 Docker daemon 上以 `--rm --network host` 运行一个本轮临时容器，引用本机已有的 `postgres:16-alpine` 基础镜像，只读取 host 网络命名空间标识；该容器未挂载现有卷，数据目录使用 tmpfs，并已自动删除。没有修改或删除该基础镜像；构建、部署、数据库和 peer 演练均在新机器的独立 daemon 内完成。

源码由 `git archive HEAD` 导入机器，排除 env、数据库及备份，再显式覆盖本任务的 Dockerfile、`.dockerignore`、`.env.example`、双实例 Compose、Alembic 环境、容器入口和 SQLite 恢复工具；网络预检另覆盖其两份测试脚本。逐项核对机器内覆盖文件与工作区 SHA。最终 `.dockerignore` 的 SHA-256 为 `ebdad1ffde4c13468dad78d4e7fe575a507ff359992b5c0b9fc786b4b2bb26b4`；实际应用容器内 Alembic 环境为 `2f49468408f6c0ef12ba0cf70294845cd3676860ff27f91be278effd984b9eed`，SQLite 恢复工具为 `68839655d5a7f62c1eabc17ca6fdc7a06057165608e7f678e97d00d5bd61098c`，均与工作区一致。受控构建上下文并不包含未提交的非运行期测试改动。

本次先执行候选文档的 Docker 网络自检，随后才执行实际双实例部署。为保证只清理本轮资源，自检构建比文档中的 `docker build -q .` 额外添加了预先确认不存在的唯一标签和镜像标记，脚本另显式指定专用诊断目录；Docker 客户端始终指定该机器自己的 socket 和新建配置目录。核心命令的等价参数如下，`WORK`、`IMAGE` 和 `LABEL` 均为本轮唯一值：

```sh
orb run -m "$MACHINE" -u root -w "$WORK/source" \
  env -u DOCKER_CONTEXT DOCKER_CONFIG="$WORK/docker-config" \
  DOCKER_HOST=unix:///var/run/docker.sock PATH=/usr/bin:/bin \
  /usr/bin/docker build -q --label "$LABEL" --tag "$IMAGE" .
# IMAGE_ID 使用上一步构建返回的 sha256
orb run -m "$MACHINE" -u root -w "$WORK/source" \
  env -u DOCKER_CONTEXT DOCKER_CONFIG="$WORK/docker-config" \
  DOCKER_HOST=unix:///var/run/docker.sock PATH=/usr/bin:/bin PYTHONUNBUFFERED=1 \
  /usr/bin/python3 backend/tests/c7_container_smoke.py --image "$IMAGE_ID" --diagnostics-dir "$WORK/reports"
cd backend
python3 ../.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_container_acceptance/from_scratch_smoke.py --machine "$MACHINE"
```

自检退出码为 0。默认每实例 1 worker 的 2 个 peer 端口以及每实例 4 worker 的 8 个 peer 端口，均可从受信实例连接，但独立 bridge 中的探针不能连接；探针自身网络的 TCP 正向对照与探测后的受信端复测均成功。Docker 宿主机在隔离机器内部，分别可直连 2/2 和 8/8；这不是公网可达性结论。[自检脱敏记录](readme-preflight-final-u2fs8c7z.json)

从零部署脚本在机器内复制 `.env.example` 并用随机值填写数据库、管理员、加密和回环监听配置，接着执行 `config --quiet`、`up -d --build`、`ps` 和 `logs`。两个实例各自的 `/healthz` 及读取数据库设置的 `/api/v1/auth/config` 均返回 200，后者的注册和 MCP 配置符合预期。只访问本机测试端口的无头浏览器在管理页面看到两个进程，其主机名和 PID 与 peer 数据库登记一致；两个 peer 的受信连接、独立 bridge 正向对照、外部阻断和受信复测通过。[最终源码部署脱敏记录](from-scratch-result-8vgiu3v6.json)；日志、凭据和页面正文未留存。

候选 README 在自检前的 SHA-256 为 `0d7dd48585ad00ae11f32e8677f503505f5c0acc7b3678187953f7291f92b146`，演练后为 `46b2435fa6c98fbbf9e382fb362dc67e157e484221ddb6e1918372f3114edbc8`。候选文档维护者确认期间只在恢复章节补充自定义数据库角色或地址的目标校验说明；多实例部署、自检、启动和网络步骤未变。这里保留两次哈希，不将本演练当作恢复章节的验证。

复验结束后，验收脚本改为按祖先目录中的文件标记定位项目根目录，并根据脚本自身位置传输探针；远程 peer 探针只要求 tooling 内的测试模块，不依赖项目的 Alembic 文件。模拟活动任务与归档深度、无项目树的远程 tooling、缺少标记及归档探针传输路径后，定向测试共 25 项通过，相关四个脚本的 Ruff 检查通过。之后把修复后的脚本复制到本轮临时的模拟归档深度，在同一隔离机器重新完整执行双实例构建、页面和 peer 测试并通过：[模拟归档复验记录](from-scratch-result-286477g3.json)。模拟归档目录在活动任务记录目录内，并非真实移动或归档任务。

两次部署报告的 `cleanup_confirmed` 均为 true。清理前核实本轮镜像标签的 ID 和标记后移除；内层 Docker 的容器、卷及自建网络数量均为 0，本轮远程和本地临时目录已核实删除；唯一测试机已删除并在机器列表中确认不存在。未读取用户 `.env`、数据库或现有卷。

本次以程序完成模板复制和随机填写，未逐字手工执行文档中的密钥生成命令；未覆盖旧单实例切换、真实排空备份和回退、代理与 SSE 配置、跨主机共享目录及文件锁或目标生产网络。该标准 Docker 同机结果不改变前述 OrbStack 内置 Docker 跨 bridge 实测失败的结论。
