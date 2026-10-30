# Research: 管理员项目列表与既有工作区复用

- Query: 为管理员提供跨用户项目查询、删除、资料/正文/设定编辑时，前端最小可靠复用方式及路由、API、状态、阅读器与 AI 边界。
- Scope: internal
- Date: 2026-10-30

## Findings

### 1. 结论与推荐

建议新增独立的 `/admin/projects` 管理列表（`RequireAdmin`），从列表进入现有 `/projects/:pid/...` 工作区，复用人工内容编辑页面。项目详情响应提供明确的项目归属与管理能力元数据；页面根据「当前管理员正在访问其他人的项目」显示持续可见的归属横幅、返回项目管理入口，并关闭未授权的 AI / 智能体 / 用户资源操作。

这是前端改动最小的方案，前提是后端为选定的人工内容接口实现管理员权限，且不把项目所有者作为请求操作者。跨用户操作是否允许，仍由后端每次请求判断，前端能力信息只控制交互。

不要用 `location.state`、查询参数或一次点击保存在 store 的布尔值作为唯一管理身份来源：直接访问、刷新、新标签页、退出/重新登录后都会丢失或失效。更不要切换登录用户或在 API 客户端根据当前浏览器路径自动冒充项目所有者。

可选传输契约应由后端最终设计统一，例如：项目响应含 `owner: {id, username}` 与 `can_use_ai` / `is_owner`，或者受控的等价字段。当前 `Project` 没有 `user_id` / `owner`（`frontend/src/api/types.ts:161`），因此列表的 owner 信息不能独自承担刷新后的权限判断。

### 2. 现有结构与复用证据

| 文件与行号 | 当前行为 | 本次用途 |
|---|---|---|
| `frontend/src/router.tsx:23` | Router 配置，已有 `/admin/users` 的 `RequireAdmin`，作品内容位于 `/projects/:pid` | 加入管理列表，保留既有编辑页面 |
| `frontend/src/components/RequireAdmin.tsx:7` | 使用真实认证状态和 `adminGuard`；普通用户跳转，后端兜底 403 | 新管理员列表直接复用 |
| `frontend/src/utils/adminUsers.ts:7` | `unknown` / anonymous / 非管理员 / 管理员四种分支 | 已有守卫测试范式 |
| `frontend/src/pages/AdminUsersPage.tsx:30` | `query`、`page`、`pageSize` + memo loader + AntD 表格；变更后刷新 | 管理列表交互模式，避免复制账号专属操作 |
| `frontend/src/components/AppLayout.tsx:40` | 账户菜单中管理员入口 | 增加「项目管理」 |
| `frontend/src/components/AppLayout.tsx:86` | 全局侧栏底部已有「用户管理」 | 增加同级入口，桌面/移动复用 |
| `frontend/src/pages/project/ProjectLayout.tsx:12` | 用 `:pid` 加载 project store，再展示 `Outlet` | 管理归属横幅、能力上下文的自然位置 |
| `frontend/src/stores/project.ts:20` | `load(id)`、`refresh()` 固定调用 `projectApi.get`；单个项目缓存 | 保留现有详情数据来源；如响应附带管理元数据，刷新必须保留 |
| `frontend/src/hooks/useProjectId.ts:4` | 只从 `useParams()` 取 `pid` | 路径前缀本身不影响该 hook |
| `frontend/src/hooks/useLoader.ts:15` | memo loader，卸载使用 `alive` 忽略结果，按项目资源广播刷新 | 管理列表和现有内容页继续使用 |
| `frontend/src/utils/projectRefresh.ts:1` | 按项目 ID 广播失效，无用户数据缓存 | 人工 CRUD 刷新可直接复用 |
| `frontend/src/App.tsx:33` | `userId` 变化时重置 AI、项目、任务、风格、Skill store | 新增任何用户相关状态必须同步清理 |

管理列表建议字段：项目名、所属用户、状态、章节数、总字数、创建/更新时间。使用 `PROJECT_STATUS`（`frontend/src/api/projects.ts:32`）及 `formatTime`，请求返回 `Page<AdminProject>`，搜索变更回第一页，删除当前页最后一项时回退。新页面适合沿用 `lazyPage`，避免增加首包体积。

### 3. 路由方案比较

| 方案 | 优点 | 代价/风险 |
|---|---|---|
| 管理列表 `/admin/projects`，编辑继续 `/projects/:pid/...`（推荐） | 既有跳转、API、store 和阅读器能最大限度保留；无隐藏的全局 API 作用域 | 必须从服务器返回归属/能力；工作区与 Reader 都要显示管理身份；后端普通内容接口需要显式支持管理员 |
| 编辑使用 `/admin/projects/:pid/...` | URL 直接表达管理入口，可用管理员路由守卫包裹全部子页面 | 不能只注册一组别名路由，必须统一改写下面列出的导航、store 和 API；Reader 是独立路由组；前缀绕回普通路径会丢失管理状态 |
| 在 `/projects/:pid` 增加 `?admin=1` 或 `location.state.admin` | 表面改动少 | 参数丢失、刷新和新标签页不可靠；同一资源的能力状态有两份来源；不是可接受的鉴权方式 |

如后端最终要求专用管理端点，建议把作用域作为显式参数/不可变的项目 API 对象在页面边界传递，并统一生成链接与 API 路径；不要在 `client.ts` 里查看 `window.location` 后重写任意请求。异步回调、后台任务、路由切换会让该做法混用作用域。

#### 显式 `/admin/projects/:pid` 时必须处理的硬编码边界

- `frontend/src/components/AppLayout.tsx:32`：项目识别正则仅匹配 `/projects/(数字)`；否则管理工作区被当作全局页，显示新建作品及全局资源导航。
- `frontend/src/components/StudioNavigation.tsx:50`：所有项目导航统一生成 `/projects/${projectId}/${key}`；需要统一 base path 和管理模式菜单过滤。
- `frontend/src/components/AppLayout.tsx:70`、`:103`：返回书库、概览、面包屑均使用普通项目路径。
- `frontend/src/pages/project/ChaptersPanel.tsx:27`、`:70`、`:104`、`:147`、`:165`、`:189`、`:197`、`:235`：阅读、新建后、从大纲创建、行菜单/点击/双击/链接进入编辑器。
- `frontend/src/pages/project/ChapterEditorPage.tsx:108`、`:634`、`:641`、`:649`、`:821`：错误返回、上一章/下一章、阅读、返回列表。
- `frontend/src/pages/project/OutlinesPanel.tsx:191`、`:329`：建章后跳转与已建章节链接。
- `frontend/src/pages/project/OverviewPanel.tsx:154`、`:195`：删除后返回列表、向导入口。
- `frontend/src/pages/project/WizardPage.tsx:115`、`:169`：向导完成跳转；如跨用户模式不允许向导，应直接阻止该子页面。
- `frontend/src/pages/ReaderPage.tsx:82`、`:113`、`:165`、`:313`、`:340`：初次定位、切章、返回编辑；Reader 路由在 `router.tsx:29`，位于 `AppLayout`/`ProjectLayout` 之外。
- `frontend/src/components/reader/ReaderAnalysisPanel.tsx:22`：查看分析/去分析链接。
- `frontend/src/utils/shelf.ts:121`、`:127`：书架继续读/继续写链接。管理员列表应独立构造进入链接，不改变自己的书架默认行为。
- `frontend/src/api/tasks.ts:116`：后台任务跳转仍是普通路径；本次不授权访问他人任务，不应通过改写这里绕开边界。

### 4. API 路径与网络边界

所有请求使用 `frontend/src/api/client.ts:91` 的 `rawRequest`，保留 cookie、统一错误、401 清理。`buildUrl` 在 `client.ts:34` 只拼接 `/api/v1`，没有项目作用域或管理 header。

| API 文件 | 路径构造点 |
|---|---|
| `frontend/src/api/projects.ts:4` | `/projects`、`/projects/{id}`，封面 URL/上传/删除亦在此文件 `:16` / `:21` / `:27` |
| `frontend/src/api/story.ts:12` | `makeCrud` 提供世界观、基础角色、大纲 CRUD；角色选角在 `:27` |
| `frontend/src/api/chapters.ts:16` | 章节 list/get/create/update/delete 与从大纲建章；AI 请求却是根级 `/chapters/{id}/generate` / `polish` |
| `frontend/src/api/chapterVersions.ts:19` | 版本 CRUD / restore 嵌套项目章节路径；generation logs 同文件 `:30`；SSE rewrite 根级路径 `:36` |
| `frontend/src/api/organizations.ts:18` | 组织与成员 |
| `frontend/src/api/relationships.ts:18` | 关系类型、关系、历史、图谱；generate 与人工 CRUD 混合在同一模块 |
| `frontend/src/api/careers.ts:22` | 职业/持有人/角色职业分配/历史；generate 与人工 CRUD 混合 |
| `frontend/src/api/outlines.ts:33` | 树、移动、细纲、建章、快照、导入导出，以及 AI continue/expand |
| `frontend/src/api/foreshadows.ts:18` | 伏笔 CRUD / plant / resolve / abandon / history / reminders |
| `frontend/src/api/memories.ts:15` | 记忆 CRUD / search；章节上下文 preview 是根级 `/chapters/{id}/context-preview` |
| `frontend/src/api/kb.ts:16` | 知识库文档/设置/分块，以及 search / reindex / rebuild |
| `frontend/src/api/reader.ts:5` | 目录、阅读进度、keepalive 进度上报 |
| `frontend/src/api/analysis.ts:31` | 状态、分析详情、回写操作、AI 分析/重写，部分是根级章节/分析资源路径 |
| `frontend/src/api/cards.ts:11` | 内容卡片 import/export/preview |

`projectApi.list()` 应继续表示当前用户自己的书架，不改成管理员全量列表。全量列表属于 `adminApi.listProjects()`，以免首页、API 密钥授权项目选择等消费方意外暴露或误选他人项目。

### 5. 人工编辑与未授权动作必须同时处理 UI 和自动行为

以下为设计建议，最终范围需主会话在规划摘要中明确，不能将“完整管理”自行扩为消耗别人模型配置或私有资源。

| 区域 | 可直接复用的人工内容能力 | 跨用户模式应隔离的行为与证据 |
|---|---|---|
| 项目外壳 | 项目加载、页面内容、归属横幅 | `ProjectLayout.tsx:32` 智能体按钮与 `:37` `AgentDock` 都不应挂载；`?agent=open` 也不能打开 |
| 概览 | 书名、题材、简介、主题、标签、卖点、读者、目标字数、状态、人工封面、删除 | `OverviewPanel.tsx:32` 分析设置卡；`:60` 批量分析；`:105` 从项目建立灵感会话；`:195` 向导；`CoverPanel.tsx:80` AI 封面 |
| 世界观/角色/组织/关系/职业 | 现有表单和 CRUD、关系图谱/成员/阶段维护 | 世界观 `WorldPanel.tsx:94`，角色 `CharactersPanel.tsx:129` / `:137`，组织 `OrganizationsPanel.tsx:166`，关系 `RelationshipsPanel.tsx:185`，职业 `CareersPanel.tsx:107` / `:109` 的 AI 入口/弹窗 |
| 大纲 | 树、人工增删改、排序、细纲、从已有纲目建章 | `OutlinesPanel.tsx:224` 续写、`:235` 向导、展开任务；人工批量建章不等同 AI 批量生成，应按端点性质分类 |
| 章节列表 | 列表、阅读、人工新建、删除、从大纲创建 | `ChaptersPanel.tsx:95` 批量生成、`:48` 挂载时读取最近批量任务、`:111` 批量任务横幅。只隐藏按钮仍会请求他人任务 |
| 正文编辑器 | 标题/正文保存、字数、前后章、未保存离开防护、版本内容（若后端允许） | `ChapterEditorPage.tsx:147` 自己的 AI 配置 hook、`:187` 分析 hook、`:406` 自动接任务、`:435` generating 状态查询任务；`:680` AI 面板；分析抽屉、局部改写、重写、角色选角中的智能操作都需按能力阻止 |
| 阅读器 | 目录、正文与切章；既有分析内容是否可读由后端内容范围决定 | `ReaderPage.tsx:172` 进度上报及 `:234` 卸载上报会改变项目所有者的阅读进度，管理阅读应关闭这两条写入；`:74` 也不宜用所有者进度作管理员继续阅读位置。独立全屏页要另外显示归属 |
| 知识库 | 已有文档元数据/分块查看、人工删除或注入设置（若范围包含） | `KnowledgePanel.tsx:78` 上传后立即返回并追踪建索引任务，不能作为纯上传开放；`:71` 索引轮询；reindex/rebuild/search 可能使用向量模型，需依后端分类隐藏/拒绝 |
| 剧情记忆 | 既有记忆查看、手动记忆 CRUD、钉住/屏蔽 | search / 上下文 preview 可能依赖检索模型，不可因为按钮名是“预览”就推定无模型调用 |

特别需要避免的两类隐式行为：

1. `useChapterAnalysis(chapterId)` 在 `frontend/src/hooks/useChapterAnalysis.ts:39` 自动读状态，`:57` 读分析详情，`:97` 根据 `active_task_id` 读取并订阅任务。若不授权他人任务，应增加显式 enabled/能力参数，在 effect 和 action 层一起阻止；只不渲染抽屉不够。
2. `ChapterEditorPage.tsx:278` 手工保存只提交 `{title, content}`，并刷新分析状态；保留此保存/版本规则。若所有者在生成中，不能把查询他人任务失败解释为“卡住”并向管理员提供复位（当前 `:448` 正是该 fallback）。应依服务器锁定/冲突信息处理，不取消或接管所有者任务。

### 6. 用户级资源与默认值保存陷阱

- 项目默认风格控件 `StyleSelect.tsx:18` 从当前登录人的 `useStyles()` 加载选项，而不是项目所有者的库。未知 ID 只显示 `风格 #id`（`:32`）。
- `OverviewPanel.tsx:123` 保存基本信息时始终发送 `default_style_id: v.default_style_id ?? null`，即使管理员只改书名，也会重复提交所有者的私有风格 ID。后端若按操作者校验风格，会错误拒绝；若按管理员自己的选项放行，可能把管理员私有资源绑定到他人项目。
- 章节风格 `frontend/src/components/chapter/ChapterStyleSetting.tsx:24` 变更即独立 PATCH `style_id`，仍使用同一用户级 `StyleSelect`。
- 最小建议：他人项目内把默认/章节风格绑定视为不在本次范围的用户资源，保留现有绑定，保存内容时省略该字段；如要支持重新绑定，必须提供后端受控的可分配选项契约，不能读取所有者的完整私有风格库。
- `AppLayout.tsx:87`「模型与创作资源」链接仍指向管理员自己的 `/settings/ai`。可保留为自己的资源但容易混淆，管理他人项目时建议隐藏或明确标注当前账户；绝不将全局资源页切换为所有者作用域。
- 备份/生成记录可能包含私有模板、配置引用或提示词快照，不能自动归为正文读取。`chapterVersions.ts:30` 的 generation log 与 `VersionDrawer` 同区呈现，需按后端返回数据确认范围后决定是否挂载该标签。

### 7. 状态一致性

- `ProjectLayout.tsx:25` 通过 `project.id === pid` 避免展示错项目；能力判断也必须绑定当前项目 ID，不能复用旧缓存的 owner。
- `useProjectStore.load` / `refresh` 当前没有请求序号，慢请求可覆盖后进入的项目（`stores/project.ts:20` / `:31`）。若本次修改 store 保存管理元数据，应同时确保只接受当前目标请求结果，并覆盖“自己的项目→他人的项目→返回”的切换测试；这是直接改动该状态时需守住的边界。
- 不新增可被用户编辑的全局“当前项目所有者”身份。服务端返回元数据可以随 project 缓存，操作仍只传项目/内容 ID。
- 现有项目刷新广播只发生在当前浏览器内，不提供管理员与所有者跨浏览器实时推送。验收可要求所有者重新读取后看到保存结果，不能承诺自动实时刷新或多人协作冲突处理。

### 8. 建议验证

现有环境：`frontend/vite.config.ts:50` 为 Vitest `node`，没有 jsdom；`frontend/src/api/admin.test.ts:11` 提供 fetch stub 范式，`frontend/src/components/agent/agentUI.test.ts:1` 等使用 `react-dom/server` 检查实际渲染。

- API 测试：管理列表搜索/分页/状态/用户参数、请求方法/路径、cookie、403/404/401、删除 204；如果增加作用域 API，覆盖所有不同路径风格，特别是根级 `/chapters` / `/analyses`，而不只 `/projects`。
- 纯逻辑/渲染测试：管理员入口显隐；自己的项目仍显示 AI；他人项目显示用户名/项目名且不渲染 AI / 智能体 / 批量 / 私有资源入口；复制链接刷新后管理身份仍正确。
- effect 测试或实际浏览器网络验收：跨用户编辑器/章节列表不发起他人任务查询或订阅；Reader 滚动、切章、隐藏、卸载均不发进度写入。
- 保存验证：管理员只改标题时不发送/清空私有风格 ID；修改正文后所有者重新打开可看到正确字数和内容；不触发 AI。
- 删除：取消无请求、确认只删目标；删除当前页最后一条回退；删除后返回管理列表。
- 切换：快速切换自己的项目与他人项目不串 owner/能力；登出或 401 清理缓存；管理员被降级后服务器拒绝后续请求。
- 浏览器：桌面与 375px 导航/列表/编辑器、浅暗色、直接访问/刷新、错误与空列表；不得仅用静态 HTML 代替交互和网络验证。
- 最终门禁：在 `frontend/` 执行 `npm run lint && npm run build && npm test`，包体积仍遵守 1500 kB 上限，不改变阈值。

## Related Specs

- `.trellis/workflow.md`：本任务仍为 planning；仅写研究，不启动实施。
- `.trellis/spec/frontend/component-guidelines.md`：管理员守卫、现有页面/编辑器不变式、外壳/移动布局、主题 token。
- `.trellis/spec/frontend/state-management.md`：全局 store 和用户切换清理。
- `.trellis/spec/frontend/type-safety.md`：传输类型镜像、PATCH 省略/null 语义、统一 API 客户端。
- `.trellis/spec/frontend/quality-guidelines.md`：lint/build/test、node 测试环境、按需加载与颜色限制。
- `.trellis/spec/backend/auth-admin.md`：需要由主会话在新权限方案确定后同步原“管理员不能读小说内容”契约。

## External References

本次只核实仓库现状，没有依赖新增的第三方 API 设计或外部资料。依赖版本来自 `frontend/package.json`：React `^18.3.1`、React Router `^6.30.6`、AntD `^5.29.3`、Zustand `^5.0.16`；无需新增依赖即可完成上述前端方案。

## Caveats / Not Found

- 当前代码没有通用的项目管理作用域、能力上下文、owner 元数据或管理员项目列表；以上新增契约是设计建议，不是现成功能。
- 后端端点具体授权白名单、查询效率、任务并发锁、响应字段脱敏由另一研究任务负责，本文件不替它作结论。
- “设定”是否包括知识库、记忆、版本/分析历史、导入导出等需在最终规划摘要明示。仅“完整管理”选项不足以推导代所有者使用模型、访问私有模板/风格库、接管后台任务或改阅读进度。
- 研究期间未改产品代码，未运行 lint/build/test 或浏览器；此文件列的是实施后的验证要求。
