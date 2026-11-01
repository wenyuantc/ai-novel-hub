# 系统设置实施计划

2026-11-01；用户明确授权规划完成直接实施，范围见PRD/design。

## 0. 启动

- [x] 规划三文档及implement/check清单齐备，task validate通过。
- [x] 记录HEAD/status，保护原有.codex/config.toml、.gitignore及两个source-command skill修改。
- [x] 创建codex/admin-system-settings，设置授权元数据，task.py start后读取Phase2.1。
- [x] 后端Trellis implement代理拥有backend所有产品/迁移/测试；前端代理拥有frontend所有产品/测试；主会话拥有任务文档、规范和集成验收。互不回退他人改动。
- [x] 所有提示以Active task路径开头；native上下文优先，子侧pull fallback。前后端按design冻结DTO，不交叉修改types.ts。

## 1. 后端

- [x] strict六项GET/PATCH、false/null/省略/来源/revision409及权限测试。
- [x] SystemSettings模型/导出、0027迁移；无业务FK/不进备份。
- [x] 管理器/service：部署基线、启动加载、锁+revision CAS、同事务审计、确认提交后同步发布同一Settings并notify。
- [x] 回滚不发布；提交确认丢失/请求取消协调重读，独立工作会话与shutdown排空；坏持久配置不可静默回默认。
- [x] deps/main/测试fixture初始化，新管理路由及注册，管理/公开配置no-store。
- [x] 局部system_settings审计动作/目标/白名单摘要，失败无未提交after。
- [x] 实际注册、两级并发、普通/批量自动分析、开放API保留原计数。
- [x] MCP试用/发现/智能体Worker实际出站前读实时开关，连接/排队后关闭仍阻断待发调用，保留已发调用。
- [x] Gate/Event真实业务测试，审计故障/提交确认/恢复/新应用重启、迁移/回归。

## 2. 前端

- [x] api/types.ts镜像design及窄审计摘要，api/systemSettings.ts。
- [x] RequireAdmin懒加载设置页面及AppLayout全入口，紧凑六项表单/来源/部署/默认。
- [x] 编辑暂存、保存busy、逐项恢复确认及待保存、dirty刷新保护、409/结果未知/权限/账号/卸载失效。
- [x] auth独立公开配置同步，App单一30秒可见轮询/回前台刷新；慢请求去重/失败保持/保存后废弃旧响应。
- [x] 如提取可见轮询工具，保留dashboard/adminTasks导出和60/5秒既有行为。
- [x] 审计标签/摘要，API/controller/轮询/结构测试，375px/浅暗/键盘。

## 3. 验证门禁

backend工作目录：

```bash
uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_system_settings.py tests/test_system_settings_runtime.py tests/test_system_settings_migrations.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_auth.py tests/test_admin.py tests/test_tasks.py tests/test_batch_generate.py tests/test_mcp_plugins.py tests/test_api_keys.py tests/test_admin_audit_logs.py tests/test_migrations.py tests/test_project_transfer.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest
```

frontend工作目录：

```bash
npm test
npm run lint
npm run build
```

局部命令按实际新增文件名校准，不创建空测试满足计划。

- [x] AC1/2/9：管理401/403、严格白名单、false/null、版本竞争、新应用重启及恢复、凭据注入/no-store。
- [x] AC3/4/5/7：真实注册/调度/普通及批量自动分析/当前分钟新阈值，不取消原任务。
- [x] AC6：连接/队列屏障关闭后待发MCP调用为零，已发调用可完成，无回收/提案/脱敏回归。
- [x] AC8：fake timers/EventTarget覆盖30秒/可见/回前台/慢请求/迟到响应/卸载/保存/脏编辑。
- [x] AC10局部40项验证审计/确认丢失/关停/迁移及计数边界；最终完整产品快照1332 passed/exit0，并结合修正后40项定向通过。
- [x] 独立Trellis check全范围审查，修复重跑局部与全量门禁。
- [x] 临时库/测试账号/假外部服务真实浏览器核验保存/恢复/公开入口、桌面375px/浅暗/失败恢复；不改真实开发库，不调用真实LLM。
- [x] PG未实测，报告如实记录，不把SQLite声称等价。
- [x] git diff --check与verification.md记录实际通过/阻塞项。

## 4. 收尾与回退

- [x] 按真实实现更新系统设置规范及索引、auth-admin/MCP/open-api/admin-audit/前端契约。
- [x] 用户已授权提交、收尾、合并main与推送；按commit-plan.md执行，保护原有修改。
- [x] 不扩大白名单或多实例框架；必须改变已确认边界则先澄清。
- [x] 回退仅本任务文件；部署回退优先留表，真正downgrade停服务/留覆盖快照，说明恢复部署行为。

## 最终检查补充

独立全范围check修复本页confirm所有权、API限额数据库上限及并发测试的Gate身份假设；前端最终634测试/类型/lint/等价构建通过。真实浏览器保存/恢复、冲突、公开同步、冷启动、边界、审计、401/403、主题/移动均完成，证据见research/browser-verification.md。后端最终全量1332 passed/exit0，17个产品/迁移文件哈希未变，结合最终40项定向；前端634通过。首轮既有writeback超时已独立复跑1+整模块9通过，未将首轮exit1记为通过。完整证据与边界见verification.md。

## 工作提交

产品提交：5a8a1bc6c37f077d6d0db2169dd91d8f8c0f45bc；规范/验收提交：78ed9ba8458a346a546198cf062181b1e514f03a。用户已授权收尾、快进合并main与普通推送；不修改或提交原有无关文件。归档过程中一并清理研究摘录的单个EOF空行。
