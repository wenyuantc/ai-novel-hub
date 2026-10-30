# 调研：管理员项目管理的验收基础

- Query：如何验证管理员跨用户项目管理，同时保护普通用户归属隔离及用户私有资源。
- Scope：internal
- Date：2026-10-30

## 已有验证设施

- `backend/tests/conftest.py:50`：每个测试使用临时数据库与 `FakeLLM`，可用独立 Cookie 的 `make_client` 构造管理员、所有者、第三名用户，数据库隔离不依赖外部服务。
- `backend/tests/test_admin.py:35`：现有 `admin_client` fixture；管理员引导、降级、禁用、401/403 等已有验收模式。
- `backend/tests/test_projects_and_story.py:60`：`test_cross_user_isolation` 覆盖普通用户对他人项目/章节/设定/知识库/剧情记忆等接口的拒绝访问。这一断言必须保留；新增管理员例外不能把普通用户检查改为放行。
- `backend/tests/helpers.py:39`：`FakeLLM` 记录 `calls`、`embed_calls`、`tool_calls`、`image_calls`，可直接证明管理员人工编辑未消耗所有者的模型配置或触发生成。
- `frontend/src/api/admin.test.ts:24`：stub fetch 检查查询参数、方法、请求体、Cookie 与中文错误传播，可扩展项目列表/删除接口契约测试。
- `frontend/src/utils/adminUsers.test.ts:23`：已有管理员路由守卫及输入确认规则测试模式。
- `frontend/src/components/agent/agentUI.test.ts:1`、`frontend/src/components/bookImport/mobile.test.ts:1`：node 环境已有 `react-dom/server` 组件渲染验证模式，不必引入新 DOM 测试库。

## 需要证明的边界

1. 管理列表接口：未登录 401；普通用户 403；当前启用管理员可以查询。角色被撤销后，同一会话下一次管理请求立即失去权限。
2. 资源边界：管理员编辑 Alice 项目成功，Alice 随后读到结果，Bob 仍然 404；不存在项目以及不属于该项目的子资源仍然 404。
3. 私有资源边界：项目可管理不等于可以读取所有者的模型配置、平台密钥、私有 Skill 或 MCP 插件。
4. 模型副作用：人工章节保存、剧情记忆保存等不会自动以所有者身份创建 AI 或向量任务。请求失败也不能产生任务或修改内容。
5. 数据完整性：字数随正文修改正确重算；单项删除不影响其他用户/其他项目；列表统计、过滤总数、分页顺序一致。
6. UI：管理员列表可进入内容编辑，持续显示所属用户并能返回管理列表；普通用户无管理入口；跨用户人工编辑保留保存、未保存离开保护及正确错误反馈。

## 质量命令

后端工作目录 `backend/`：

```bash
uv run ruff check .
uv run pytest
```

前端工作目录 `frontend/`：

```bash
npm run lint
npm run build
npm test
```

实施后还需真实浏览器验收管理员/普通用户、桌面/375px、亮色/暗色；构建产物保持 1500 kB chunk 上限，不调低质量门禁。

## 注意事项

- 此文件记录验收方案，规划阶段未运行新增功能测试，也不声称现有质量门禁已通过。
- 后端测试已有的普通用户跨项目拒绝访问必须保留，不能通过删改原隔离断言获得绿色结果。
