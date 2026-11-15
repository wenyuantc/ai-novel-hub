# E4 章节占用原语独立复核

2026-11-10：本次只复核 0046 持久列、单书备份规则及内部章节占用原语，不表示章节 AI 已接入协作者生产路径。

## 已修复

1. `require_running_chapter(..., for_update=False)` 过去只依赖调用方声称已持发送 lease，未验证任何物理门禁；无锁情况下的实际调用本来能通过。现在该模式一律拒绝并回滚，`for_update=True` 仍按认证/User → 发送写门禁 → 项目 → Task → Chapter 锁序验证。新增无锁、只持 User、只持项目锁三组直接调用反例。未来只有设计并验证绑定同一连接和事务的 lease 凭据后，才能重新开放读取模式；普通布尔标记不是证明。
2. `require_running_chapter` 校验失败原来未回滚调用方事务：测试先写正文、替换 claim UUID、捕获冲突再 commit，原正文会被覆盖。现在所有异常都先回滚，调用方不得假定先前同一事务内的写入仍存在。
3. 手工复位的非数据库异常和非法状态参数原来可能让调用方捕获异常后提交之前的部分写入。现在都在异常路径回滚；测试分别在章更新后注入异常、先写正文再传无效状态，随后 commit 验证持久状态未改变。
4. 领取激活在进入异常保护前调用 `dataclasses.replace`，畸形入参可能使此前已执行的 queued→running CAS 留在可提交事务；且只验证转换后的 running 身份，未验证原 queued 快照必须无 execution UUID。现在先在事务保护内验证原快照和运行身份，再锁后交叉验证数据库。两个反例验证错误后即使调用方 commit，也不会保留该领取。

## 复核结论与边界

- 每个单章／batch attempt 由服务端生成独立 claim UUID；验证 Task UUID、queued_at、execution UUID、真实 actor、project、原 grant 与 Task/Chapter 双向指针。新批量尝试重新生成 claim，旧清理不能清掉同一执行体的后续尝试。坏结构与身份字段的待写修改失败关闭，检查阶段不自动 flush。
- 系统清理需调用方先确认旧执行体已停止；它按原 Task、排队/运行代次、grant、claim 与章节指针条件回退，不要求撤权者仍有权限，也不写正文。任务已删除或证据缺损时返回失败，不推测旧身份。当前原语本身并不证明旧执行体停止。
- 0046 SQLite 原位扩列，避免重建外键父表；历史 generating 且 claim 为空可以保留，但不能当成可信占用。任意 generating 或 claim 未空时拒绝降级。单书备份排除运行列并忽略导入文件注入，整库备份保留运行事实；恢复前仍须停止旧进程。旧任务后来补发统计 UUID 不能证明原章节占用。
- 当前 `SendGuard.verify` 的 `after_verify` 不接收数据库会话，且其约定只允许不获取数据库锁的回调；Fake provider 分支不持实际发送门禁。未来发送集成需在真实 `_send_lease` 内校验同一事务上的 lease 凭据，并在 Task → Chapter → 配置的明确顺序中复核，不能把目前关闭的读模式当成授权证明。
- 本切片没有接任务受理/领取、章节与批量生成、实际出站、结果保存、富文档提案、取消清理、人工复位入口或旧任务修复。提交结果未知、完成收据、取消/删除与清理的原子性、真实 HTTP 及跨连接竞争均需完整接线后验证；不能据此开放协作者章节 AI 或部署。

## 验证

- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_chapter_claim_core.py tests/test_chapter_claim_migration.py --tb=short`：26 passed。包括 SQLite 外键启用的升级/降级、场景和版本保留、整库快照和单书 HTTP 备份/注入反例。
- `uv run ruff check app/services/chapter_claim.py app/models/entities.py app/services/project_transfer/registry.py alembic/versions/0046_chapter_claim_identity.py tests/test_chapter_claim_core.py tests/test_chapter_claim_migration.py tests/test_chapter_claim_postgresql.py`：通过；所改 Python 文件的 `compileall` 通过。项目未配置独立 Python 类型检查器。
- 本次未并发运行 PostgreSQL。此前核心作者记录的 3 项 PG 结果发生在本次修复前；请主会话在 `backend` 目录、专用本机隔离库已配置的条件下错峰重跑：`LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_chapter_claim_postgresql.py --tb=short`，检查输出确为通过而非跳过。测试使用 `AINOVEL_COLLAB_POSTGRES_TEST_URL`，迁移也可使用 `AINOVEL_COLLAB_CHAPTER_POSTGRES_TEST_URL`。
