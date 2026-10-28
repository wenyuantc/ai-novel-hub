# 执行计划：Skill 与 MCP 插件

## 开工前

- `ls backend/alembic/versions`：当前最新是 `0018_project_agent`，本任务计划用 `0019_skills_mcp`。
- `git status`：`.trellis/tasks/10-23-project-cover/` 和 `pelican-bicycle.html` 不属于本任务，不要动。
- 按 implement.jsonl 阅读 spec，重点是 `backend/agent.md`：工具层、提案、checkpoint、不可信包装。
- 用 context7 查 `modelcontextprotocol/python-sdk` 1.x 的客户端写法：streamable HTTP 和 SSE 客户端、`ClientSession`、自定义 httpx 客户端、超时。然后 `uv add "mcp>=1.2,<2"`，以 uv 解析出的版本为准，并确认与 httpx 0.28 兼容。
- 内置 Skill 的正文要自己编写，写作前不要打开 MuMu 的 `skills/` 目录。

## 有序清单

### 后端

1. [x] **迁移 `0019_skills_mcp` 与模型**：
   - 新建 `skills`、`skill_references`、`mcp_plugins` 三张表；
   - `agent_conversations` 增加 `mcp_plugin_ids`，`agent_messages` 增加 `skill_id` 和 `skill_snapshot`；
   - 全局 slug 用部分唯一索引；
   - 写入 4 个内置 Skill，正文在 `app/skills/builtin/*.md`；
   - 登记 `NOT_EXPORTED`；
   - 增加配置项 `mcp_enabled`。

   测试：迁移 up/down、反射测试、部分唯一索引在 SQLite 上生效。
2. [x] **`skill_service` 与 API**：
   - 可见性和读写权限（404/403 的区分）、复制、上限；
   - SKILL.md 和 zip 的导入导出；
   - 触发词匹配；
   - 恢复内置内容。

   测试：
   - 跨用户访问返回 404；普通用户写全局 Skill 返回 403；管理员可以写；
   - 导入导出往返一致；
   - 非法包：路径穿越、超大、解压炸弹、缺少 frontmatter、数量超限；
   - 触发词只匹配开头的 `/词`，用户自己的 Skill 优先；
   - slug 冲突时复制自动追加后缀。
3. [x] **Skill 接入生成任务**：
   - 相关请求增加 `skill_id` 并校验；
   - handler 快照到 `params["skill"]`；
   - 5 个 system 模板增加 `skill_guide`；
   - 生成记录 `meta` 记录所用的 Skill；
   - `validate_references` 支持 Skill，并按用户可见性校验。

   测试：
   - 快照之后修改 Skill，重试时仍使用旧内容；
   - 他人的私有 Skill 返回 404；
   - 渲染结果包含补充工作流段，`skill_id` 为空时渲染结果与改动前完全一致；
   - parity 和模板计数同步。
4. [x] **MCP 适配层与 `mcp_service` / API**：
   - `client.connect`：SSRF、不跟随重定向、超时、错误脱敏；
   - `to_text` 结果转换、工具 schema 校验和 hash；
   - 增删改、上限、导入 JSON（拒绝 stdio）、刷新、工具偏好、试用调用；
   - 请求头加密存储，只回显请求头名称；
   - `mcp_enabled=False` 时返回 403。

   测试：
   - 用 FakeMcpConnector 覆盖：刷新存库、工具偏好、试用、参数 schema 校验；
   - 请求头在所有响应中做负向搜索；
   - 内网开关关闭时连接 127.0.0.1 被拒，且假服务收不到请求；
   - 遇到 302 时不跟随；
   - 超时被映射为错误；
   - 集成测试：用 FastMCP 在进程内起 Streamable HTTP 服务，测真实的 `list_tools` / `call_tool`；如果 SSE 服务能在进程内启动，也测一遍。
5. [x] **智能体接入**：
   - `ToolKind` 增加 `external`；
   - 本轮工具集与映射（名称规则、64 字截断与 hash、30 项上限），映射写入 checkpoint；
   - runner：免确认工具直接调用，其他生成 external 提案；
   - `McpSessions` 在一轮运行内复用连接，并统一关闭；
   - 确认时重新计算 guards，不一致返回 409，一致则同步调用；
   - external 不可撤销、不参与自动执行；
   - Skill 方面：`skills_catalog` 变量、`list_skills` / `load_skill` / `read_skill_reference` 读工具、消息 `skill_id` 与 `/触发词` 强制启用、`SAFETY_RULES` 追加一句、会话的 `mcp_plugin_ids` 校验。

   用 FakeLLM 加 FakeMcpConnector 测试：
   - 需要确认的 MCP 调用：暂停、确认、续跑，模型拿到文本结果；
   - 免确认工具直接返回结果；
   - 编造的工具名，或使用他人或未启用的插件，返回"工具不存在"；
   - 确认前停用工具或 schema 变化，返回 409；
   - 自动执行开启时，external 仍需确认；
   - 撤销 external 返回 400，整轮撤销跳过 external；
   - 同一轮两次调用同一插件只连接一次，暂停后连接已关闭；
   - MCP 超时作为工具错误返回，这一轮继续；
   - MCP 结果被包装为不可信数据并截断；
   - 强制启用的 Skill 正文排在安全规则之后，续跑时使用快照；
   - `load_skill` 读不到他人的私有 Skill；
   - 请求头不出现在事件和生成记录中。
6. [x] **全量后端验证**：
   ```bash
   cd backend && uv run ruff check . && uv run pytest -q
   ```
   新增和修改的文件需通过 `ruff format --check`。

### 前端

7. [x] **类型与 API**：`api/skills.ts`、`api/mcp.ts`；`AgentChange.kind` 加入 `external`；会话和消息类型增加新字段。
8. [x] **页面**：
   - 顶栏入口；
   - `SkillsPage`：我的 / 全局、编辑抽屉与参考资料、导入导出、复制、恢复内置；
   - `McpPluginsPage`：表单和 JSON 两种添加方式、请求头掩码、刷新、工具表开关、查看 schema、试用；
   - 两个页面都懒加载，MCP 被关闭时隐藏入口。
9. [x] **智能体面板与生成界面**：
   - 智能体面板：Skill 选择器、`/` 补全、会话插件多选、external 提案卡片（参数 JSON 和"不可撤销"标签，无撤销按钮，确认时显示加载状态）；
   - 生成界面：章节 AI 助手和批量生成弹窗增加 Skill 选择；生成记录展示所用的 Skill。
10. [x] **前端测试**：
    - JSON 导入的解析与 stdio 提示；
    - 请求头编辑时未修改则不提交；
    - 不同 kind 的提案卡片按钮；
    - `/` 补全；
    - 全局 Skill 对普通用户只读；
    - 375 宽度渲染；
    - 禁止写死颜色的检查。
11. [x] **前端验证**：
    ```bash
    cd frontend && npm run lint && npx vitest run && npm run build
    ```
    入口 chunk 不能明显增大。

### 收尾

12. [x] **spec**：
    - 新增 `.trellis/spec/backend/skills-mcp.md`：权限模型、导入格式、MCP 适配与安全、映射规则、external 提案；
    - 更新 `agent.md`（external kind、动态工具、Skill 工具）、`prompts-styles.md`、`database-guidelines.md`、`project-transfer.md`、`index.md`；
    - 前端 component-guidelines 补充新页面和卡片的约定。
13. [x] **trellis-check**：
    - 变异验证至少覆盖：403/404 区分、可见性过滤、zip 路径穿越、触发词前缀匹配、SSRF 连接时校验、不跟随重定向、请求头脱敏、30 项上限、64 字命名、映射表外的名称被拒、external 不自动执行、external 不可撤销、确认时 guards 比较、Skill 快照、安全规则排序；
    - 安全审查：提示注入（MCP 结果和 Skill 内容）、越权、密钥泄露、SSRF。
14. [x] **浏览器冒烟**：
    - 用 FastMCP 本地脚本起一个假 MCP 服务（开启内网开关），提供一个只读检索工具和一个"发送"类工具；再起一个支持工具调用的假 OpenAI 兼容服务；
    - 依次验证：
      - 新建、导入、导出 Skill；普通用户复制全局 Skill；
      - 在章节生成中选用 Skill，在生成记录中看到它；
      - 添加插件（表单和 JSON 各一次）、刷新、切换工具开关、试用；
      - 智能体中 `/触发词` 强制启用 Skill；
      - 模型调用 `load_skill`；
      - MCP 调用的提案、确认、续跑；免确认工具直接返回；
      - 停用工具后再确认返回 409；
      - 375 宽度下无横向溢出。

    冒烟产物放在 `/tmp/sm-smoke`，经用户确认后再删除。

## 回滚点

- 每一步完成都要通过测试，可以单独回退。
- 迁移的 downgrade 会删除新增的表和列。
- `MCP_ENABLED=false` 可以在运行时关闭 MCP。
