# 管理员作品管理

## 1. 范围 / 触发条件

适用于 `/admin/projects` 列表、管理员对他人项目的人工内容维护，以及与任务、私有配置、阅读进度共用的授权 helper。此契约扩展 [管理员与会话](./auth-admin.md)：管理员可以维护小说内容，但不能冒用所有者身份使用其模型、任务或账户资源。

前后端共用现有 `/projects/{id}` 编辑路径，不通过临时令牌、客户端 owner 参数或全局请求重写切换登录用户。普通用户访问他人资源仍为 404。

## 2. 签名

```python
# backend/app/services/project_service.py
async def get_owned_project(db, user_id: int, project_id: int, *, lock: bool = False) -> Project
async def get_manageable_project(db, actor_user_id: int, project_id: int, *, lock: bool = False) -> Project
async def project_out(db, project, notice=None, *, actor_user_id: int | None = None) -> ProjectOut

# backend/app/services/chapter_service.py
async def get_owned_chapter(db, user_id: int, chapter_id: int) -> Chapter
async def get_manageable_chapter(db, user_id: int, project_id: int, chapter_id: int) -> Chapter

# backend/app/services/admin_project_service.py
async def list_projects(db, *, q, owner_user_id, status, page, page_size) -> AdminProjectList
```

`GET /api/v1/admin/projects` 使用 `AdminUser`，内容接口继续使用真实 `CurrentUser`。

## 3. 契约

### 列表与归属投影

| query | 约束 |
|---|---|
| `q` | 可省略、最多 200 字，trim 后按项目标题或用户名大小写不敏感子串搜索；`%`、`_`、反斜杠转义为字面量 |
| `owner_user_id` | 可省略、正整数；不存在的筛选用户得到空列表 |
| `status` | 可省略，`planning` / `writing` / `completed` |
| `page` / `page_size` | 默认 1 / 20；page ≥1，page_size 1–100 |

响应为 `{items: AdminProjectOut[], total}`。每项只有 `id`、`title`、`genre`、`status`、`owner_user_id`、`owner_username`、`chapter_count`、`total_words`、`created_at`、`updated_at`。同一组条件用于 count 和分页；排序 `updated_at DESC, id DESC`，空项目统计为 0，统计以分组子查询完成，不能逐行调用 `project_out`。

`ProjectOut.owner` 为可空 `{id, username}`。跨用户详情和 PATCH 响应提供 owner；调用 `project_out(..., actor_user_id=user.id)` 使跨用户 `reading=null`，不查询所有者阅读进度。个人 `GET /projects` 继续只返回当前账户自己的书架，平台密钥授权也不因此获得全量项目。

0024起个人书架、此正常管理目录及count均排除回收站/删除中作品；已删除作品只在管理员[回收站](./recycle-bin.md)中展示。Task、直接章节/回写ID、开放API、备份独立流会话也要检查生命周期，不能把行仍存在当作可访问。

### 人工操作与严格拥有权

- `get_owned_project` / `get_owned_chapter` 保持严格语义。`get_manageable_project` 仅对所有者或数据库中当前启用管理员放行；任意子资源仍验证 `project_id`，不能跨路径混用 ID。
- 人工项目资料/状态/删除、世界观/人物/组织/成员/关系/类型/职业/分配/伏笔、大纲/细纲/建章/快照、章节/选角/版本内容与恢复、手工封面、已有知识库文档/分块/人工设置、剧情记忆维护、阅读目录与正文可使用管理权限。
- 共享的 `story_service` 入口默认严格，仅人工路由显式选择 `manage=True`；不能让 AI 等调用者随人工 CRUD 一起获得访问权限。
- AI、智能体、任务、生成日志提示词、私人风格/模板/Skill/MCP、模型配置、平台 Key、阅读进度、导入导出/备份、知识库上传/重建/向量检索保持原有归属限制。
- 跨用户项目 PATCH 不接受 `style`、`default_style_id`、`auto_analyze`；章节 PATCH 不接受 `style_id`。普通人工保存省略这些字段，保留已有引用，不绑定管理员的私人资源。
- 剧情记忆 API 的 `_enqueue_vectors` 调用前检查 `memory_service.may_enqueue_vectors`：管理他人内容时不创建所有者向量任务。业务 service 仍更新关键词检索块、失效旧向量；自动记忆不可直接编辑正文/删除的原规则继续生效。
- 章节修改/恢复仍遵守生成中状态、字数、版本和关联编号规则。写入口显式请求父项目生命周期锁，不能仅凭先前读取授权后无锁提交。普通作品DELETE改为停止目标任务执行链后移入回收站，保留30天；只有管理员purge或系统到期清理才物理级联目标数据，不删除用户级配置。已发出的外部模型请求不能通过删除撤回，管理员不因此获得通用他人任务操作权。

### 前端工作区

`utils/projectAccess.ts` 按当前 pid、服务器 owner 和真实用户派生 `ready/managingOther/canUseAI`，只控制 UI。管理列表由 RequireAdmin 保护；ProjectLayout 和独立 Reader 显示归属与返回入口。未加载正确项目时不得先挂载 AI/任务副作用。

跨用户模式必须同时关闭按钮和自动行为：AgentDock、批量任务查询、编辑器任务接续、useChapterAnalysis 请求与订阅、生成日志、向导、私人风格选择及模型型知识库操作。Reader 不采用 owner reading，不发送普通或 keepalive/unload 阅读进度。自己的项目保留原功能。

项目 store 的加载/刷新响应按请求序列失效；`reset()` 还要清除目标。`setProject` 的保存回调只能更新目标 ID 与实际已加载 ID 都一致的项目，不能在 reset 后仅因新会话再次开始加载相同 pid 就接纳旧保存响应。对应退出/同 pid 重载回归在 `stores/project.test.ts`。

删除/恢复/purge成功只作废目标项目和任务缓存；明确404刷新会撤下已不可访问项目，临时网络失败仍静默保留。普通删除的书架/管理目录/工作区共用确认组件，不得再提示立即永久删除。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 未登录请求管理列表或内容 | 401 |
| 普通用户调用 `/admin/projects` | 403 `需要管理员权限` |
| 普通用户/已降级管理员访问他人项目内容 | 404 `项目不存在` |
| 禁用或会话过期 | 401，沿用既有会话失效机制 |
| 不存在项目或混用他人项目子资源 ID | 404，关联写入继续遵守原 400/404 校验 |
| 非法 status、q 超限、非正 owner/page 或 size 越界 | 422 |
| 跨用户修改风格绑定或 AI 设置 | 403 |
| 管理员调用他人 AI/日志/进度等未开放动作 | 沿用严格归属 404 |
| 生成中章节人工保存或版本恢复 | 沿用冲突规则，不因管理员身份绕过 |

## 5. 正例 / 基准 / 反例

- 正例：管理员修改 Alice 章节正文，Alice 重新读取得到新正文、正确字数及手工版本，Bob 请求同一章节仍 404。
- 基准：管理员查看自己的项目，仍能使用自己的模型和写作功能；个人书架不混入 Alice 项目。
- 反例：为了复用所有页面，把 `get_owned_project` 放开管理员，或把请求 `user.id` 换为所有者；这会使 AI、私有配置与日志沿共享调用链意外开放。

## 6. 必需测试

- `backend/tests/test_admin_projects.py`：列表身份、组合查询/分页/字面搜索/统计；跨用户人工模块读写；所有者可见与第三人隔离；跨项目 ID；降级即时撤权；软删保留/普通访问拒绝及明确purge后目标级联；风格/AI/日志/进度拒绝；记忆更新和 FakeLLM 零调用。
- `backend/tests/test_projects_and_story.py::test_cross_user_isolation` 原断言保留；不得通过放宽断言或删除端点检查实现通过。
- 前端 `api/admin.test.ts`、`utils/projectAccess.test.ts` 及相关管理页面/能力测试：参数、Cookie、owner/pid/user 派生、严格名称确认、个人功能保留。
- 实际浏览器验证管理资料/正文/设定、保存后所有者读取、刷新归属、无任务/阅读进度请求、取消/确认删除和桌面/375px明暗主题。

## 7. 错误写法 vs 正确写法

错误：

```python
# 管理员被冒充为所有者，后续任务和模型配置也跟着切换。
await content_service.update(db, project.user_id, project.id, body)
```

正确：

```python
# 保留真实 actor，只为人工内容入口授予管理员例外。
project = await get_manageable_project(db, actor_user_id, project_id)
# 项目内资源查询继续限定 project.id；AI/账户接口仍调用严格 owner helper。
```

错误的界面实现只隐藏“AI生成”按钮，却继续挂载读取 active_task 的 effect；正确实现使用同一服务端归属派生能力控制 effect、订阅和动作，同时保留后端严格拒绝。
