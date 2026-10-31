# 实施与验证计划

状态：2026-10-31 功能实施、独立检查与规范沉淀已完成。用户已要求提交收尾，并在提交计划后明确要求合并到 main、推送；正在按计划执行提交与收尾。检查结果以 verification 和 research 报告为准，不因计划勾选而推定通过。

## 0. 执行方式与基线

- [x] 核对用户批准的是最新 `prd.md` / `design.md`；确认当前 Alembic head、任务状态和工作区差异后才启动。
- [x] 实施/检查分别使用 `trellis-implement` / `trellis-check`。每个派发提示以 `Active task: <task.py current 输出路径>` 开头，优先原生上下文注入；缺失时子代理自行读取 jsonl、PRD、design、implement 和对应 phase 2.1/2.2 详情。
- [x] 明确文件所有权：后端生命周期/任务/迁移共享边界由同一实施负责人维护；前端可在 API 契约冻结后独立派发，但不得同时修改共享类型。所有子代理被告知并非独自在代码库工作，不回滚他人改动、不 commit/push；主会话负责规划、spec 和收尾。
- [x] 保存本任务开始前的文件基线。既有 `.codex/config.toml`、`.gitignore` 和两个 `source-command-trellis-*` skill 目录不属于本任务，不改动、不纳入后续提交。

## 1. 数据模型与最底层保护（AC1、AC4、AC6）

- [x] 新增 0024（若 head 已变化则重核顺序），添加生命周期字段/索引/状态约束，Project→User 改为 RESTRICT；审计 actor ID 允许真实系统空值。
- [x] 新增集中 `project_lifecycle` predicate 和写事务屏障，复用已有 no-op UPDATE 锁思路；原 owner/manageable 权限含义不变，读写调用明确区分。
- [x] 建立写入口覆盖清单，标明每个服务的父行锁与 commit 边界，包括多次提交/独立 session；不留仅检查读取却允许后提交的缺口。
- [x] 修改删号存量检查与友好错误，保留自删/最后管理员护栏；project_count 包含回收站，数据库约束是最终防线。
- [x] 更新 PROJECT_RULE.exclude、导入字段保护和 APP_SCHEMA；新导入项目不带删除或协调状态，旧 v1 文件仍兼容。
- [x] 先写带存量父子数据的迁移/回滚保护测试，覆盖章节版本、封面、授权及历史审计保留，执行外键一致性检查。

里程碑门禁：模型、迁移、基本授权与删号保护定向测试通过；未接入自动清理前不做真实数据的不可逆验证。

## 2. 任务停止与删除协调（AC5）

- [x] 从现有取消实现提取按已授权 project/Task 执行的内部核心，不伪装成 owner、不新增管理员通用任务接口。
- [x] 登记并等待 queued/paused 的取消收尾、running 执行体和已终态任务的成功回调；对失败/超时返回可核实结果，不用终态计数代替执行体退出。
- [x] 新建/重试/继续、自动分析、enqueue_memory 等直接插 Task 的路径共享父项目准入屏障；调度器不领取删除中/回收站项目。
- [x] 将原 DELETE 接到“持久化意图 → 取消并排空 → 最终校验 → 软删除”的协调流程；不跨取消等待持有数据库行锁。
- [x] 失败、请求取消、数据库异常时清理自己的 token；启动恢复遗留意图，已保存内容保留，已取消任务不自动复原。
- [x] 对暂停后才收到取消、完成事件已产出、on_success 自动分析、另一个用户取消请求仍在 on_abort 中、超时执行体继续收尾等场景写确定性事件屏障测试，不靠 sleep 争抢时序。
- [x] 测试最终失败审计与返回值明确“作品未删除”，并验证恢复后无自动任务/模型调用。

里程碑门禁：FakeLLM 阻塞/失败/cancel 与回调事件测试全部通过；证明协调器不会提前声明删除成功。

## 3. 全入口隔离与回收站服务（AC1–AC4、AC7）

- [x] 普通列表/count、owner/manageable helper、章节直接 ID、分析回写直接 ID 使用统一生命周期过滤。
- [x] 开放 API 授权查询、小说列表、封面、下载；Task 列表/输出/SSE/重试/继续/清理/裁剪；生成日志、导出独立流会话和灵感知识注入全部核查。
- [x] 项目写入矩阵接入事务屏障：人工内容、大纲/快照、正文/版本、封面、知识库/记忆、阅读进度、灵感应用、智能体效果/撤销与分析回写。
- [x] 实现仅管理员的列表/恢复/purge schema、路由和服务；分页与字面搜索复用既有模式，items 与 count 条件一致。
- [x] 恢复/purge 校验原始 deleted_at 代次、confirm 真布尔值及精确名称，行锁下重读期限；定义重复/已清理/超期/异常缺少 owner 的结果。
- [x] 内部 last_deleted_at 持久高水位跨恢复/重启保留，锁内单调生成 deleted_at；冻结或回拨时钟时，旧purge仍必须409。公开API和前端字段不变，高水位不导出/不接受导入。
- [x] SQLite projects启用AUTOINCREMENT并验证物理purge后ID不复用；结合时间高水位，旧请求也不能误操作同名新实体，迁移仍保留全部存量父子数据及审计序列。
- [x] 恢复保持原 ID/归属/创作状态及关联内容，不启用账户、不重建私人资源、不自动排任务；彻底删除仅清目标外键图，处理检索缓存/任务 hub。
- [x] 用完整关联数据 fixture 做删除→隐藏→恢复逐项比对→明确 purge 的往返；检查其他作品和用户级资源不变。

## 4. 自动清理与审计（AC4、AC8）

- [x] 实现统一时钟、30 天半开可恢复窗口，启动补清 + 每小时后台清理；每次固定 cutoff，键集分页、每项独立事务、失败下轮重试。
- [x] cleaner 与恢复/purge 复用锁定后的单项目事务；活动执行体异常则跳过，不能在并发恢复成功后再按旧快照清理。
- [x] 新增 project.restore/purge/expire、有界生命周期摘要和系统 actor 内部入口；保留旧日志、普通自主删除不扩展成全用户行为审计。
- [x] 管理员恢复/手动 purge 即使自己的作品也显式记录，其他模块审计范围不变；审计写失败让对应业务回滚。
- [x] 同步审计 schema、前端类型/标签和系统 actor 展示，旧日志/日期预览/nonce/高水位清理回归通过。
- [x] lifecycle 启停顺序明确，普通 fixture 不意外启动后台清理；用可控时钟测试到期前/恰到期/到期后、长期不访问页面、停机补清和 shutdown 无悬挂任务。

## 5. 管理界面与原入口同步（AC3、AC6–AC8）

- [x] 新增懒加载 `/admin/recycle-bin`，同步 RequireAdmin、桌面/移动导航和页名；使用 AntD 安静的信息密集型管理布局。
- [x] 新增严格镜像后端的 API 类型与客户端；只从 api 模块请求，不重写 Cookie/401/ApiError；deleted_at 原始字符串直接回传。
- [x] 搜索、所属用户筛选、分页、到期状态、恢复确认、逐字名称 purge 确认；忙碌防重复、失败保留目标、旧响应失效和末页回退。
- [x] 统一书架、工作区和管理目录的删除提示：移入回收站、保留 30 天、管理员恢复、停止任务及失败含义。
- [x] 用户管理作品计数注明含回收站；有作品时禁止删号并解释先禁用/清理，处理服务器返回的并发冲突，不只禁用按钮。
- [x] Key编辑保留隐藏grant：范围未变的PATCH省略all_projects/project_ids，投影[]允许只改元数据；创建/明确改范围保持原验证和替换语义，配回归及实际UI保存验证。
- [x] 根据正确项目 ID 清理项目/任务前端状态，不让旧响应重新挂载被删项目；不重置其他作品未保存内容。
- [x] 加入 api/recycleBin、utils/recycleBin 及对应 UI `.test.ts`，按现有静态渲染测试习惯验证标签/权限/确认规则；真实交互另走浏览器。

## 6. 验证矩阵与命令

实施中新增后端 `tests/test_recycle_bin.py`、`tests/test_project_lifecycle.py`，必要时单列 `test_recycle_bin_tasks.py`；文件名如需调整在报告中同步。现有删除测试按新规则重写并保留“物理 purge 才级联”断言。

| 类别 | 必须覆盖 |
|---|---|
| 授权 | 401/403/404、owner/admin/第三人、降级/禁用、跨项目子资源 ID、管理员私有模型零越权 |
| 恢复完整性 | 正文/版本/所有设定历史/快照/封面/知识库/记忆/阅读进度、Task 历史与暂停取消状态、禁用 owner、既有授权恢复/已撤销授权不重建 |
| 危险确认 | 取消零写入、空名称/多余空白、confirm 非 true、旧 deleted_at、恢复后再次删除、重复/已清理目标 |
| 时间 | UTC/带时区输入、微秒代次、到期边界、冻结时钟、启动/周期清理、部分失败重试 |
| 并发 | 删号与创建、普通写入与删除屏障、Task 创建/领取/取消/恢复、on_success 派生、restore 与 purge/expire |
| 审计 | 真实 admin/system actor、同事务失败注入、日志保留、摘要无正文/凭据、既有受保护日期清理 |
| 浏览器 | 桌面 + 375px 明暗主题、筛选/分页、恢复、取消/确认 purge、删除超时反馈、普通用户无入口、正常工作区不再读取回收站内容 |

所有命令在对应目录单独执行，不依赖真实模型：

```text
工作目录 backend/
uv run ruff check .
uv run pytest tests/test_recycle_bin.py tests/test_project_lifecycle.py tests/test_tasks.py tests/test_admin.py tests/test_admin_projects.py tests/test_admin_audit_logs.py tests/test_open_api.py tests/test_project_transfer.py tests/test_migrations.py
uv run pytest

工作目录 frontend/
npm run lint
npm run build
npm test

仓库根目录
git diff --check
```

- [ ] PostgreSQL 额外验证：用单独临时数据库跑迁移带数据保留、实际 RESTRICT、父行屏障和恢复/清理竞争；两个独立连接配合事件屏障，核对最终数据库不变量，不把 SQLite 的串行通过当作生产行锁证据。 **未执行：当前无可用PG环境，发布前补验，见environment-checks.md。**
- [x] 真实浏览器只用隔离测试账户/作品，清楚区分普通删除与不可逆 purge；不使用真实用户数据或真实模型/外部 MCP。截图/请求证据写入本任务 verification/research 文档。
- [x] 若 PostgreSQL 或浏览器验证不可用，明确记录未验证项与原因，不能宣称全量验收完成。

## 7. 全范围检查、规范与收尾

- [x] 派发 trellis-check 对本任务全量 diff 做独立检查：从 AC1–AC8 逐项验证，不只看最后一轮改动；修复后重跑受影响门禁。
- [x] 更新 backend admin-projects/auth-admin/database/admin-audit/project-transfer 及必要 frontend spec，新增回收站可执行契约并在对应索引挂接；不能保留“DELETE 立即级联”和“删号清空作品”的旧规范。
- [x] 记录 `verification.md`：实际运行的命令、结果、浏览器证据、数据库类型和仍存在的验证缺口；没有执行的不写成通过。
- [ ] 主会话按用户授权处理提交、归档和 journal，再快进合并 main 并正常推送 origin/main；不纳入既有无关改动。 **用户已授权，按 commit-plan.md 执行中；不强制推送。**

## 8. 回滚点

- 自动清理接入前：优先在临时数据库撤回代码/迁移验证，不对用户库实验。
- 发布前：备份数据库并停止写入；新旧版本不可混跑。
- 发布后：发现异常先停清理/停写并前滚修复；不要直接切旧应用使回收站作品重新可见。
- schema downgrade 对回收站/删除中数据及本次新增生命周期审计动作/摘要设置保护；不可逆清理、已取消任务及外部副作用不随代码回滚恢复。
