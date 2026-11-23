# C3 实施与验收清单

开工前在目标分支核对 C1 和 C2 的实际代码、迁移 head 和已评审的[需求](prd.md)、[设计](design.md)。实施、故障注入和回滚的结果见[综合验收记录](research/verification.md)。父任务记录的部署约束继续适用。

Trellis 自动载入规范时有 32 KiB 上限。实施与复核时另读 `database-guidelines.md` 的「后台任务表」、`skills-mcp.md` 的「中断的外部调用回收」、`agent.md` 的外部提案与原身份章节、`error-handling.md` 的「后台任务与 SSE 契约」，以及 `quality-guidelines.md` 的 PostgreSQL 隔离库与迁移冻结结构章节。

## 1. 固定对照场景

- [x] 使用 `peer_app_factory` 建两个应用、独立连接池与 worker。先用正常 API 入队，并证明 A 确实在执行、B 确实先受理。以事件控制扫描、取得处理权、真实发送、数据库提交和执行体退出；从第三个会话观测已提交结果。固定时钟或屏障，避免靠睡眠推测因果。记录旧版本实际失败点。
- [x] 分别为本人、管理员、回收站、外部调用记录现有 HTTP 状态码、等待上限、审计、收据与返回投影。特别区分在线受理可核对的无主单章（cancelled）、重启恢复遗留的 running 管理意图（interrupted）以及收据已经证明成功或暂停的任务。
- [x] 验证停止调度后仍能处理 HTTP 排队取消和删除，以及真正关闭应用后租约退出。记录仅靠本机列表时漏掉远端成功回调和已终态清理的窗口。

## 2. 迁移与持久身份

- [x] 将运行中取消意图绑定任务原轮次。在 C2 的领取事务内同时登记作品执行体的原身份，在原协程退出后另行确认。记录作品删除发起者、请求侧清理身份、有效完成或失败的证据、外部调用的原始 worker 和 claim。新归属用独立字段或记录表示，不能破坏旧签名和严格的 JSON 字段校验。避免把运行时细节带进公开任务响应、审计、日志或单书存档。
- [x] 实现应用级请求租约、原操作的唯一认领和条件归还。保留仍被引用的终止 worker 登记，覆盖心跳、主库阻塞、SQLite 配对失效、进程换代和部分启动失败。逐项把现存内存集合对应到[管理员取消](research/admin-drain.md)和[作品排空](research/project-drain.md)给出的持久证据。
- [x] SQLite 和 PostgreSQL 往返迁移。覆盖空库、0067 的健康和无从归属的旧名额、已退出的旧删除意图、老版本有效与损坏的外部调用。验证拒绝时数据、索引及 Alembic 版本均完整。排空后降级，再用 0067 读取原业务数据。另验原生快照冷恢复、单书 v16 导出和导入。

## 3. 本人及管理员取消

- [x] **AC1、AC2** 分别从 A、B 受理单章、分析和向导取消。交换角色并验证信号实际到达执行者、章节回退一次、生成记录一次。本人请求在领取已提交但回执未到、最终保存进行中、正在返回富文档投影时也保留原轮次和授权限制。超过等待上限返回当前状态，后续扫描仍能完成有效的取消。
- [x] **AC3、AC4** 管理员 API 立即返回 202、一个操作号、一次成功受理审计。两个 drain 同时到达同一轮的首次清理前再放行，交换胜者；在排队、待审核、运行中的单章和批量任务上分别验证一次有效副作用。分别让本人先受理和管理员先受理，核对最终业务结果、真实赢家和重复审计数。
- [x] **AC5** 在取消受理、围栏决定、接管提交和读取业务收据之间停住，再改变它们的先后。原执行者、接管者及再次接管者迟到时只处理有权处理的原轮次。验证任务 ID 复用、同 UUID 重新入队、已标待核对、原处理器未知、已提交成功或暂停，以及提交回执丢失。
- [x] 完整验证授予、撤权、受理后连接断开，以及请求结束前原角色或审核身份变化时，本人返回数据和管理员审计符合既有行为。对已有行缺 UUID、旧知识库索引及分析、向导特殊收据单独测试。

## 4. 作品排空与外部调用

- [x] **AC6、AC7** B 删除 A 正在执行的作品。覆盖 A 运行中、已终态但成功回调仍在途、任务行被裁剪、名额归还已提交但原协程尚未退出、退出确认提交回执迟到，以及排队或暂停取消已写终态但请求仍在清理、清理失败、原凭据已被替换。让 B 先碰到屏障，再放行 A；最终软删事务前再尝试登记新工作。断言失败时作品仍可见、原资源安全、重试只处理原轮次；也覆盖永久删除和过期清理对相关屏障的复核。
- [x] **AC8** A 已冻结原 token 时启动 B，A 可继续完成。确认 A 失联后由 B 清旧 token；同时更换 token 时旧恢复不触碰新意图。对主库暂不可写、缺 SQLite 配对证明和禁用/停止调度但仍正常提供 HTTP 的场景分别验收。
- [x] **AC9** 用真 SDK 与本机受控工具服务观察实际调用次数。A 提交 executing 后、真正发出请求前、收到响应头后、写入结果前分别暂停；B 访问会话、重复确认或请求删除。健康调用即使跨过两分钟也能完成。原 worker 确认失联后，未知结果在重试、启动和定时恢复下均不会再次发包。测试历史合法凭据、签名损坏、变更被替换、旧 worker 恢复运行和自动外部调用现有禁用行为。
- [x] 在独立进程验证本人取消实际到达执行端，管理员的重复请求共用受理号，且作品在另一端仍收尾时不会进入回收站。记录两个进程的身份、原任务的模型请求次数及释放时间，使用临时库和测试作品。

## 5. 验收与回滚

**AC10** 从上述正常入口和以下既有模块回归。列表中的命令在 `backend/` 下运行。新增跨进程测试纳入全量和 PostgreSQL 专项，以实际收集到的测试标识登记结果。单进程断言发生变化时记录原行为、新的可观察证据和原因。

```sh
uv run pytest -q tests/test_tasks.py tests/test_collaboration_cancel.py tests/test_sqlite_cancel_rollback.py
uv run pytest -q tests/test_admin_task_cancellation.py tests/test_admin_chapter_claim_cancellation.py tests/test_admin_uncertain_execution.py
uv run pytest -q tests/test_recycle_bin_tasks.py tests/test_project_lifecycle.py tests/test_agent_external_deletion_guard.py
uv run pytest -q tests/test_agent_recovery_claim_identity.py tests/test_agent_unknown_outcome.py tests/test_agent_mcp_send_lease.py
uv run pytest -q tests/test_task_worker_handler_fencing.py tests/test_task_worker_recovery_crash.py tests/test_task_slot_lifecycle.py
uv run pytest -q tests/test_task_worker_migration.py tests/test_recycle_bin_migrations.py tests/test_backup_sqlite_restore.py tests/test_project_transfer.py
```

- [x] **AC11** 在 SQLite 做完整回归和 Ruff。在隔离 PostgreSQL 16 的专用库串行跑 AC1、AC3、AC6，另覆盖涉及行锁的唯一认领、外部调用和迁移用例。核对 SQL 确有不同事务竞争（可用独立会话观察阻塞），记录方言、测试名、通过数以及跳过/预期失败的原因。
- [x] 把 AC1–AC11 的实际证据写入本任务研究目录。更新对应后端规范、父任务进度和部署说明。多进程正式部署留待 C7，跨进程订阅与即时唤醒留待 C4。
- [x] 验证回滚顺序。停止受理与执行，处理或核对所有新意图、在途工作、未决业务收据及引用的 worker。存在无法证明的清理时保留证据并中止降级；有可用备份时演练现有离线恢复流程。确认降级成功再启动相匹配的旧版本。

```sh
uv run ruff check .
env -u AINOVEL_POSTGRES_TEST_URL LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20
```
