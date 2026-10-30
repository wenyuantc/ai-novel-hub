# 管理员项目管理最终审核

- 审核角色：已派发的 `trellis-check`；直接审核及修复，没有再派生实施/审核代理，没有执行 Git 写操作。
- 范围：本任务全部 `backend/**`、`frontend/**` 变更及新增文件；依据 `check.jsonl`、PRD、design、implement、研究材料与原文规范审核。README/spec/任务状态由主会话维护。
- 结论：本轮三个局部问题已修复，最终前后端质量门禁通过。没有待修复的本任务阻塞项；现有并发保存限制见下文。

## Findings (fixed)

1. 文件：`frontend/src/stores/project.ts`、`frontend/src/stores/project.test.ts`。
   - 问题：保存回调原先在 `targetId=null` 时仍可写入项目，退出后旧页面的迟到保存能恢复旧项目。
   - 修复：`setProject` 必须匹配当前目标 ID，reset 后拒绝保存回填。新增退出后的迟到保存与当前项目以外保存的回归，保留正常当前项目保存；退出后回填曾真实失败，修复后通过。

2. 文件：`frontend/src/stores/project.ts`、`frontend/src/stores/project.test.ts`。
   - 问题：只校验目标 ID 仍不足，新会话再次加载同一 pid、但尚未取得项目时，旧保存会抢先回填并关闭 loading。
   - 修复：进一步要求响应 ID 匹配当前实际已加载的 `project.id`。同 pid 未加载回归先真实失败，修复后通过，还确认拒绝旧保存后新加载正常完成。
   - `adminProjectsUI.test.ts` 的两个 SSR 场景用 `setState({project})` 提供静态输入，不再把保存 action 当初始化入口；未削弱界面断言。

3. 文件：`frontend/src/pages/AdminProjectsPage.tsx`。
   - 问题：筛选区搜索框在文档流里使用 `calc(100vw - 82px)`，违反响应式规范对占位滚动条宽度的约束。
   - 修复：筛选区改为直接 flex 换行，搜索框使用 `width:280`、`maxWidth:'100%'`，不再依赖视口减固定值。
   - 验证：主会话在干净浏览器重新登录，375×812 下 `documentElement.scrollWidth=clientWidth=375`；最终截图已更新。

## Findings (not fixed)

- 已知限制，按主会话明确确认保留：新会话已经重新授权并完整加载同一 pid 后，旧保存回调仍可能覆盖这个已授权项目的新读取结果。当前用户已拥有该项目权限，owner 不转移，能力仍按当前 user/owner 派生；这是原有保存响应的版本竞争，不是管理员权限绕过。本任务不新增多人冲突合并，也不宣称解决全部并发保存；本轮修复覆盖 reset 后及新加载尚未完成时的状态回填。
- 既有 Node `--localstorage-file` ExperimentalWarning、Gemini SDK `generate_images` 弃用提示没有屏蔽或修改。浏览器既有 Router/InputNumber 提示见 `browser-checks.md`；未扩大到无关升级。

## 权限与数据流审核

- 严格 helper `get_owned_project`、`get_owned_chapter` 保持原义；人工管理独立使用 manageable helper，真实 actor 来自 CurrentUser，非所有者管理员资格由数据库当前启用状态重新判断。子资源继续同时限定项目 ID 和子资源 ID。
- 追踪 entity、outline、generation、rewrite、batch、wizard、analysis、kb-index、agent 的共享 service 调用；其入口仍先校验严格拥有权。开放 API 的共享人工 helper 调用前仍校验 Key 授权项目，未随人工管理开放他人账户资源。
- 跨用户 `ProjectOut`/手工封面返回 owner；显式 actor 使 `reading=null`。个人列表仍只取自己的项目。跨用户项目的风格绑定/AI 设置和章节风格 PATCH 返回 403；生成中章节仍受 409 保护。
- 手工记忆同步关键词检索块，但 `_enqueue_vectors` 仅所有者可通过；管理员保存不排队所有者 embedding。正文保存、版本恢复、手工封面与 Reader 目录/正文均不引入模型调用。
- 前端同时控制按钮、挂载、effect 和订阅：AgentDock、向导、批量任务查询、章节任务接续、分析、索引轮询、生成日志与私有资源选择在管理模式关闭。Reader 普通/隐藏/卸载/keepalive 进度写入均关闭。项目 ID 与当前用户参与能力派生，加载不匹配时不挂载项目内容副作用。
- 查询使用 SQL 分组统计和稳定排序；分页与 count 共享条件，特殊字符按字面匹配。删除保留现有外键级联和名称确认，未改动账户/其他项目范围。
- 未发现新增跳过测试、忽略类型/ lint、降低阈值或额外依赖；原跨用户隔离断言保留。

## AC1–AC9 核对

| 验收项 | 证据与结果 |
|---|---|
| AC1 管理入口和普通用户隔离 | 新后端测试覆盖 401/403/404；管理入口/路由测试及三身份浏览器验收通过。 |
| AC2 实时撤权和子资源隔离 | 新测试覆盖降级后 GET/PATCH 立即 404、管理列表 403、跨项目子资源及不存在 ID；禁用/失效沿用 CurrentUser 与既有会话回归，本次全量通过。 |
| AC3 列表查询、统计和分页 | 新测试验证多用户、组合条件、分页总数、零章节统计、字面特殊字符；代码按 updated_at/id 降序；浏览器查询/过滤/两页通过。 |
| AC4 人工内容维护 | 新测试覆盖项目/正文/世界观及人物、组织、关系、职业、大纲细纲、选角、版本、伏笔、快照、封面；既有业务规则全量回归通过。浏览器所有者重新读取确认管理员保存。 |
| AC5 无所有者模型副作用 | 新测试对手工记忆保存前后任务数与 FakeLLM 零调用作断言，验证风格拒绝与生成中保护；浏览器人工操作四类模型调用为零。 |
| AC6 归属、Reader 和自己的能力 | 静态页面/能力/store 回归与实际浏览器共同覆盖；管理员阶段项目任务/分析、阅读进度请求为零，所有者的 AI/向导/智能体入口保留。 |
| AC7 删除范围与交互 | 新后端测试验证目标级联、用户和另一项目保留；浏览器取消不删、完整名称确认后 DELETE 204、列表更新通过。 |
| AC8 账户资源与 AI 边界 | 严格 helper 调用链审核及 AI/配置/私有风格/日志/进度拒绝测试通过，未冒用所有者身份。 |
| AC9 门禁与真实浏览器 | 下方最终门禁全绿；管理员/所有者/普通用户、桌面/375px、明暗主题证据见主会话的 `browser-checks.md`，本审核没有把 SSR 充当浏览器验收。 |

## Verification

以下为本审核直接执行的最终结果，退出码均为 0：

| 工作目录 | 命令 | 实际结果 |
|---|---|---|
| backend | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .` | All checks passed。 |
| backend | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest` | **1069 passed，1 warning，405.78s**；包括 21 项迁移，进程真实退出。 |
| frontend | `npm run lint` | ESLint 通过。 |
| frontend | `npm run build` | `tsc -b` 与 Vite 构建通过，3440 模块；入口 177.87 kB，vendor-antd 1146.12 kB，低于既有 1500 kB 上限。管理页独立懒加载 chunk。 |
| frontend | `npm test` | **61 文件、459 测试通过**，最终一次 4.15s。 |
| frontend | `npm test -- src/stores/project.test.ts src/components/project/adminProjectsUI.test.ts` | 2 文件、8 测试通过；后续又完成上述完整测试。 |
| 仓库根目录 | `git diff --check` | 通过。 |

- TypeCheck：前端已包含在最终 build；后端没有配置独立类型检查器，按现有 Ruff/pytest 门禁执行，未伪称运行未配置的检查。
- 旧实现阶段的 `1048 passed + 21 locale failed` 不是最终结果：无效 `C.UTF-8` 导致迁移配置中文被按 ASCII 读取。旧进程退出后，本审核只启动一次正确 locale 的后端完整测试，现已 1069 全绿；未改迁移或放宽断言。
- 浏览器由主会话独立完成：合成数据实例中管理员阶段 174 请求，预期未登录 401 之外失败为 0；项目任务/分析 0、reading-progress 0、FakeLLM chat/embedding/tools/images 全 0。最终 375px 修复已补验，细节和截图路径见 `browser-checks.md`。

无待修复的本任务阻塞项。README/spec 与任务收尾由主会话继续处理；本审核未提交代码。

## 追加：菜单命名

用户追加要求后，菜单、标题、面包屑和返回入口统一为“作品管理”，同步README/spec；原业务路径与权限保持。四文件ESLint、相关18项既有测试、类型检查和构建通过，见 `naming-check.md`。前述浏览器截图记录的是更名前验收，没有将旧截图描述成新名称的实拍。
