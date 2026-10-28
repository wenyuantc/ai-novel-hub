# 执行计划：项目智能体

## 开工前

- `ls backend/alembic/versions`：当前最新是 `0017_book_imports`，本任务计划用 `0018_project_agent`。
- `git status`：`.trellis/tasks/10-23-project-cover/` 和 `pelican-bicycle.html` 不属于本任务，不要动。
- 阅读 spec：
  - `backend/error-handling.md`：任务契约，以及 paused / resume
  - `backend/batch-generate.md`：断点续跑的写法
  - `backend/prompts-styles.md`：模板注册、锁定变量
  - `backend/story-entities.md`、`backend/outline.md`：实体服务、章节号平移
  - `backend/project-transfer.md`：表规则（用于差异快照）
  - `backend/retrieval.md`：检索与派生数据维护
  - `backend/database-guidelines.md`、`backend/quality-guidelines.md`：SSRF、FakeLLM
  - `frontend/component-guidelines.md`、`frontend/state-management.md`、`frontend/type-safety.md`、`frontend/quality-guidelines.md`
- 用 context7 查三家 SDK 当前版本的流式工具调用写法（`openai`、`anthropic`、`google-genai`），以仓库 `uv.lock` 中锁定的版本为准。

## 有序清单

### 后端

1. [x] **Provider 工具调用**：
   - 在 `base.py` 加入接口和 `TOOLS_UNSUPPORTED`；
   - 实现三家的 `chat_tools_stream`：参数累积、历史消息协议转换、usage、不支持工具时的错误映射、Gemini 的 schema 清洗；
   - 用各 SDK 的假流做单元测试：单个和多个工具调用、文本与工具调用交错、tool 结果回传格式、usage、错误映射；
   - FakeLLM 增加 `tool_script`。
2. [x] **迁移 `0018_project_agent`**：
   - 新建三张表，`VersionSource` 加 `agent`；
   - 在备份表规则中登记 `NOT_EXPORTED`；
   - 补迁移测试，跑通反射测试。
3. [x] **工具层**：
   - 实现协议和注册表；
   - 读工具（含分页和检索）；
   - 写工具的 preview / execute，复用现有服务；
   - `replace_chapter_text`：唯一匹配、章节锁、`agent` 版本；
   - `start_task` 的 kind 白名单。

   测试：
   - 每个写工具的 preview 对比和 execute 结果；
   - 传入他人项目或他人用户的 id 时返回"不存在"；
   - 参数校验错误作为工具结果返回，不抛异常；
   - 原文出现 0 次或多次时被拒绝。
4. [x] **`effects.py`**：
   - 差异快照、单条和整轮撤销、冲突检测、外键顺序还原、派生数据维护。

   测试：
   - 删除角色：关系、组织成员和职业挂接一起被级联删除，撤销后全部恢复，id 不变；
   - 新增大纲导致章节号平移，撤销后恢复；
   - 执行后再手动修改，撤销时返回冲突，数据不变；
   - 整轮撤销全部成功或全部失败；
   - 正文按版本撤销。
5. [x] **`context.py` 与提示词**：
   - 实现 `agent.system`（锁定 `safety_rules`）和 `agent.summarize` 两个模板，以及注册表；
   - 预算裁剪与压缩摘要、不可信数据包装、单条结果截断、页面上下文校验。

   测试：
   - 超出预算时触发摘要，之后的请求只包含摘要和新消息；
   - 不属于本项目的页面上下文被丢弃；
   - 覆盖系统提示词后，安全规则仍然存在。
6. [x] **runner、handler、service、API**：
   - 循环与 8 次上限（最后一次请求 `tool_choice="none"`）、提案分级与自动执行、50 项上限；
   - paused / resume 续跑、断点、重试时不重复写消息；
   - 生成记录与 token 用量、事件；
   - 会话增删改查、确认、拒绝、全部确认、全部拒绝、撤销接口，确认时数据变化返回 409。

   用 FakeLLM 测试完整场景：
   - 读工具完成后直接作答；
   - 建角色加关系：暂停，确认后续跑，模型拿到新角色 id 后提出关系提案，再次确认，整轮完成；
   - 拒绝后续跑，模型收到"用户拒绝了该修改"；
   - 自动执行：≤ 3 条小修改直接执行；4 条或含删除、启动任务时暂停；
   - 达到 8 次上限后被强制作答；
   - 同一会话发第二条消息返回 409；
   - 中断后重试；
   - 启动任务后，任务出现在列表中；
   - 跨用户访问会话和提案返回 404。
7. [x] **全量后端验证**：
   ```bash
   cd backend && uv run ruff check . && uv run pytest -q
   ```
   新增和修改的文件需通过 `ruff format --check`。

### 前端

8. [x] **类型、API 与 store**：镜像类型、`api/agent.ts`，`TaskKind`、版本来源、生成记录 kind 的标签。
9. [x] **`AgentPanel`**：
   - 懒加载、可折叠、可拖拽调宽，手机上用抽屉；
   - 会话管理、自动执行开关；
   - 时间线：流式文本、工具步骤、提案卡片（对比、确认、拒绝、撤销、全部确认）；
   - 冲突提示；
   - 附带页面上下文：路由参数和编辑器选区；
   - 相关数据失效后刷新。
10. [x] **前端测试**：
    - 事件归并成时间线；
    - 提案按钮的显示条件（proposed、executed、undone、task 各状态）；
    - 409 时刷新预览；
    - 宽度范围限制；
    - 移动端渲染；
    - 禁止写死颜色的检查。
11. [x] **前端验证**：
    ```bash
    cd frontend && npm run lint && npx vitest run && npm run build
    ```
    入口 chunk 不能明显增大。

### 收尾

12. [x] **spec**：
    - 新增 `.trellis/spec/backend/agent.md`：Provider 工具协议、工具层约定（新增工具的检查清单）、效果快照与撤销、续跑契约、安全规则；
    - 更新 `backend/index.md`；
    - 在前端 component-guidelines 中补充面板约定。
13. [x] **trellis-check**：
    - 变异验证至少覆盖：自动执行阈值 3、50 项上限、8 次上限、冲突检测、撤销时的外键顺序、原文唯一匹配、跨项目 id 返回"不存在"、确认时的预览比较；
    - 安全审查：提示注入、越权访问、`start_task` 白名单。
14. [x] **浏览器冒烟**：
    - 准备一个能识别工具调用的假 OpenAI 兼容服务，按脚本返回 tool_calls；
    - 依次跑通：读取角色，修改性格并确认，撤销；
    - 新建角色加关系，验证续跑；
    - 删除组织，撤销后成员恢复；
    - 局部替换正文后撤销；
    - 启动分析任务后取消；
    - 打开自动执行，验证小修改直接执行；
    - 刷新页面，确认这一轮仍在进行，过程完整显示；
    - 375 宽度下无溢出。

    冒烟产物放在 `/tmp/ag-smoke`，经用户确认后再删除。

## 回滚点

- 每一步完成都要通过测试，可以单独回退。
- 迁移的 downgrade 会删除三张表。
