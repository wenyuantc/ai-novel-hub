# 当前代码契约与影响面

调研日期：2026-10-31。以下是代码证据和实施注意事项，不是已完成的改动或测试结论。确认后的产品规则见 `../prd.md`，拟实施机制见 `../design.md`。

## 1. 删除与账号边界

- `backend/app/services/project_service.py:24` / `:32`：`get_owned_project` 严格校验 owner，`get_manageable_project` 只给当前启用管理员人工内容例外；不能把前者全局放宽。
- 同文件 `:262`：DELETE 在 `AuditOperation` 中 `db.delete(project)`，删除前统计章节/版本用于摘要，当前是永久删除。
- `backend/app/models/entities.py:172` / `:176`：Project 归属用户使用 CASCADE；ProjectCover 的图片二进制存数据库，并经 project_id 级联。
- `backend/app/services/admin_service.py:130`：删号在真实 actor 的审计边界内执行既有自身/最后启用管理员护栏，随后物理删 User；当前没有作品存量检查。
- `admin_service._project_count_subquery` / `_user_out`：现有 project_count 是所有实际 Project 行的计数。新正常作品目录的过滤不能错误复用到删号判定。
- `backend/tests/test_admin.py::test_delete_user_cascades` 与 `test_admin_projects.py::test_admin_subresources_stay_in_project_and_delete_only_target` 明确断言现有物理删除；实施时应改成“软删除保留 + 直接访问拒绝 + 管理员明确 purge 后级联”，不是简单删掉旧断言。

## 2. 准入与取消竞态

- `backend/app/models/entities.py:78–81`：ACTIVE = queued/running；OPEN 额外含 paused。删除必须覆盖 OPEN，不能只看 ACTIVE。
- `backend/app/services/task_service.py::create_task`：handler 校验/抢占、Task 插入与 commit 在同一事务；需要保持此原子性。
- `cancel_task`（`:272`）：queued 先条件 UPDATE 到 cancelled 并提交，再执行 on_abort；paused 同理；running 调用引擎取消。数据库终态出现时，队列/暂停任务的收尾可能还没结束。
- `backend/app/services/task_engine/engine.py::_finalize`（`:348`）：on_abort → 写终态 → prune → on_success → 清理 hub/_finalizing；可见终态不等于完整执行链退出。
- `cancel_running`（`:407`）：对执行体 cancel 一次并用 asyncio.wait 最多等待 10 秒；它不会因等待超时直接证明执行体结束。
- `handlers/chapter.py::on_success` 会 enqueue 自动分析；`handlers/analysis.py::on_success` 会 enqueue_memory；`kb_index_service.enqueue_memory` 有直接任务写入路径，不能只守 HTTP create_task。
- `handlers/chapter.py::on_abort` 目前吞异常只记日志；删除协调不能因此把失败收尾当成成功。
- `Task.project_id` 使用 CASCADE，`chapter_id` / `retry_of_id` 使用 SET NULL。软删除保留 Task 行；物理 purge 才能按项目 FK 清理。
- 引擎自身声明单进程前提；本任务不引入跨实例取消/租约能力。`main.py:62` 起 lifespan 负责启动、停止和数据库释放；普通测试 fixture 不进入 lifespan，而是显式启停引擎。

## 3. 必查访问旁路

| 位置 | 现状 / 风险 |
|---|---|
| `chapter_service.py:34` 的 get_owned_chapter | 直接 join Project，仅校验 user_id；只改 get_owned_project 会漏掉按章节 ID 的入口 |
| `writeback_service.py:444` 的 get_owned_change | 通过 join 校验用户，仍需父项目生命周期和写锁 |
| `admin_project_service.py` | count 与分页共用 conditions，可集中添加正常项目条件 |
| `api_key_service.py:92` / `:259` | 授权项目 ID 校验与 get_authorized_project 直接查 Project；保留 grant 不应让回收站可读 |
| `services/open_api/catalog.py:81` | list_novels 自己构造 Project 查询；只修普通书架不会影响开放列表 |
| `task_service.py` | 全局 Task 列表/详情/输出依赖 user_id，不检查关联项目；会暴露被软删作品的历史输出 |
| `generation_log_service.py:76` | save_log 只判断 Project 行存在；取消收尾与普通日志访问需区分 |
| `project_transfer/exporter.py:131–134` | 独立流会话重新 db.get(Project)，软删除后行仍存在；prepare 校验不足以约束稍后流读取 |
| `inspiration_service.py:449` | 从会话 project_id 直接取项目用于知识注入；灵感自身保留不等于允许读回收站内容 |
| `kb_index_service.py`、`book_import/reverse.py`、`batch_service.py` | runner 中存在直接 db.get(Project) 和多次提交；需要纳入取消/准入与提交点核查 |
| `agent/tools/base.py:52` | 已有 no-op UPDATE 锁项目且保留 updated_at，适合复用，不重复造不同锁语义 |
| `cover_service.py::_lock_project` | 自有项目锁；必须和统一生命周期条件/删除屏障一致 |

普通人工写入涉及 story/organization/relationship/career/foreshadow/outline/plan/snapshot/chapter/version/cast/cover/kb/memory/reader 等 service；还应检查 inspiration apply、agent 提案/撤销、analysis/writeback。实施时逐一建立“入口 → 父项目授权与写锁 → 写入 → 提交”的映射，不能以搜索命中数量代替覆盖验证。

## 4. 关联数据与格式

- `services/project_transfer/registry.py` 的 RULES + NOT_EXPORTED 覆盖项目关联表；后者包括 tasks、agent_*、kb_chunks、retrieval_chunks、api_key_projects、project_covers。回收站保留不能只保护可导出的表。
- PROJECT_RULE 当前 `exclude=("style",)`；生命周期与协调字段应全部加入 exclude，importer 不能接受备份注入删除状态。`exporter.APP_SCHEMA` 当前仍为 0021，本次迁移需同步真实 head。
- `InspirationSession.project_id` / converted_project_id 为 SET NULL；它是用户级会话，不随作品 purge 销毁。
- 用户级风格、模型、Skill/MCP 不随项目物理删除；风格引用计数仍应计入真实回收站引用，避免误清私人资源。
- 检索服务有按项目/source 缓存；`backend/tests/conftest.py` 因 SQLite 项目 ID 复用专门清空它。软删/恢复/purge 同样不能遗留陈旧矩阵。

## 5. 审计扩展约束

- `models/audit.py` 故意没有业务外键，actor_user_id 当前 NOT NULL；系统清理要使用诚实的系统身份必须明确迁移该字段并同步 schema/UI，不能塞伪造用户 ID。
- `schemas/audit.py` 对动作与摘要使用 Literal/白名单，前端 `api/auditLogs.ts` 镜像标签；新增生命周期动作需要逐层同步。
- `audit_writer.py::__aenter__` 只记录启用管理员，且会跳过自己的项目；新管理员恢复/purge 要用显式局部审计选项，不能意外扩大其他人工内容审计范围。
- `AuditOperation.commit` 先 flush 业务再审计，同事务提交；失败回滚后留安全失败，不记录 SQL 参数或正文。
- 已有审计日期清理的 nonce/高水位与受保护 audit.cleanup 记录契约继续保留，不能为本任务旁路或重写。

## 6. 前端可复用点

- `ProjectListPage.tsx:47` 自己 modal.confirm；`DeleteProjectModal.tsx:32` 被管理目录/工作区复用，两者当前都说永久删除，需统一成回收站规则。
- `utils/projectAccess.ts::canConfirmProjectDelete` 要求非空且逐字相同，多余首尾空白不能算确认。
- `AdminUsersPage.tsx:338–365` 现有用户名二次确认和永久删除警告需改为“作品先清理”的真实行为；`:188` 已有禁用保留数据的说明。
- 管理路由由 RequireAdmin 保护，`router.tsx` 有 lazyPage；`AppLayout.tsx` 同时维护桌面下拉、移动导航、pageName，新增入口须一起更新。
- `api/client.ts` 已支持带 JSON 的 POST 和 ApiError.body，不必为了危险删除给 api.del 发明特殊请求体；复用现有 Cookie/401/错误处理。
- `useLoader` 已有 alive 标记抑制卸载/参数变化后的旧响应，复杂动作仍需保护目标与异步操作代次；不为本任务无关重写通用 hook。
- Vitest 为 node 环境，包含 `src/**/*.test.ts`，已有 UI 测试使用静态渲染；纯静态断言不能替代真实点击、确认取消和移动布局的浏览器验收。

## 7. 质量门禁与调研结论

- 后端：`uv run ruff check .`、`uv run pytest`，SQLite FakeLLM/fake_dns；不要在测试触发真实模型。
- 前端：`npm run lint`、`npm run build`、`npm test`；新增测试文件使用现有被发现的后缀。
- SQLite 不能证明 PostgreSQL 行锁行为；实施后还需临时 PostgreSQL 验证父行屏障、FK RESTRICT 与恢复/清理竞态，无法实测时如实记录，不写成通过。
- 具体里程碑、测试矩阵和完整命令见 `../implement.md`。本轮仅完成本地代码调研和规划产物校验，未修改产品代码、未运行产品测试、未启动任务或提交 Git。
