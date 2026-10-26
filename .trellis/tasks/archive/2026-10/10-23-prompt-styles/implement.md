# 执行计划：提示词模板与写作风格

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号。目前最新是 `0012_batch_generate`，本任务计划用 `0013_prompt_styles`。
- 阅读这些 spec：
  - `backend/error-handling.md`：SSE 预览模式与 400 错误格式
  - `backend/database-guidelines.md`
  - `backend/story-entities.md`：上下文组装
  - `backend/batch-generate.md`：批量请求的保存与续跑
  - `frontend/component-guidelines.md`
  - `frontend/type-safety.md`
- 用 `uv add jinja2` 添加依赖。

## 有序清单

### 后端

1. [ ] **对照实现**：在改动任何提示词之前，把 `app/ai/prompts/*.py` 中的 `build_*_messages` 原样复制到 `tests/prompt_legacy.py`。
2. [ ] **模板引擎**：`app/ai/templating.py`，包括沙箱环境、校验（语法、未知变量、必需变量、锁定变量出现且只出现一次并且不在控制块内、for 只遍历 list 变量、长度上限）和渲染（输出上限、锁定内容兜底）。补单元测试，覆盖每一种校验错误、沙箱逃逸（`''.__class__` 之类）和 range 被禁用。
3. [ ] **注册表与默认模板**：`app/ai/prompts/registry.py`，以及 `templates/*.j2` 下的 19 个 key（含 `style.block`、`style.extract`、`style.trial`）。把各个 `build_*_messages` 改为"拼变量 → render"，签名不变，新增 `override` 参数。
4. [ ] **逐字一致测试**：`tests/test_prompt_parity.py`，用参数化用例覆盖每个 key 的全部分支，确认新旧实现输出相同；同时确认原有的提示词相关测试全部通过。**这一步是关卡：parity 全绿之后才能继续。**
5. [ ] **迁移 `0013_prompt_styles`**：
   - 新建 prompt_overrides、prompt_override_versions、writing_styles 三张表，新增 projects.default_style_id 和 chapters.style_id；
   - 插入预设风格；
   - 把 projects.style 迁移为项目的"原风格"；downgrade 时写回。

   迁移测试要覆盖：数据迁移，以及 upgrade/downgrade 往返后 style 原文不丢失。
6. [ ] **`prompt_service`**：覆盖的增删改查、启停、ack_default、指纹与 outdated 判断、历史版本（保留 20 个）与回滚、预览、导入导出。路由为 `/api/v1/prompt-templates`。
7. [ ] **`style_service`**：
   - 增删改查，预设返回 403；
   - 引用计数，删除时 409 或 force；
   - 复制、导入导出；
   - `resolve` 解析优先级；
   - 渲染风格段；
   - 应用启动时同步预设。

   extract 和 trial 用同步 SSE 实现。路由为 `/api/v1/styles`；项目和章节的 PATCH 接口接入风格字段。
8. [ ] **接入**：
   - generation_service（生成、润色、按建议重写、续写）、rewrite_service、batch handler（请求加 `style`，按章节解析）、analysis、wizard、entity、outline 服务都在组装 job 时取用户覆盖并传给 build 函数；
   - 风格段注入章节类提示词；ProjectInfo.style 改为项目默认风格的一句话描述；
   - 生成记录的 `params` 中记录 prompt 来源和风格。
9. [ ] **测试**：新增 `test_prompt_templates.py`、`test_styles.py`，并扩展 generation、rewrite、batch、wizard、migration 和跨用户测试，逐条对应 PRD 的验收标准。必须包含以下用例：
   - 覆盖后下一次调用的 messages 用的是覆盖内容；
   - 停用后回到系统默认；
   - 系统默认改变（用 monkeypatch 改注册表）后 outdated 为 true，ack 之后变回 false；
   - 风格优先级四种情况；
   - 批量生成按章节使用不同风格；
   - 删除被引用的风格后回落到上一层；
   - 预设不能修改；
   - 导入非法模板时逐项返回错误。
10. [ ] **spec**：新建 `.trellis/spec/backend/prompts-styles.md`，写清模板 key 表、变量声明规则、锁定变量、新增或修改提示词时如何同步对照实现与 parity 测试、风格解析优先级和生成记录字段；同时更新 `database-guidelines.md` 的迁移列表和 `error-handling.md`。

### 前端

11. [ ] 更新 `types.ts`，新增 `api/prompts.ts`、`api/styles.ts`；Project 和 Chapter 类型加上风格字段；生成、润色、重写和批量的请求加上 `style`。
12. [ ] 提示词模板页：列表、编辑器、实时预览、校验错误定位、历史版本与对比、三方对比、导入导出。复用 chapter-versions 的 diff 工具。
13. [ ] 风格库页：卡片列表、编辑表单、复制、删除时显示引用数、导入导出、从样文提取的弹窗（流式）、试写抽屉（流式）。
14. [ ] 接入：
    - 项目概览改为默认风格选择器（去掉 style 文本框）；
    - 章节编辑器加上本章风格设置，生成、润色、重写和局部改写可以临时选择风格；
    - 批量弹窗加入风格选项；
    - 用户菜单加两个入口。
15. [ ] 纯函数测试：风格解析的展示文案、校验错误格式化、分类分组、导入结果汇总。
16. [ ] spec：更新 `frontend/component-guidelines.md` 和 `type-safety.md`。新页面按路由懒加载，包体积不超过上限。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收（用假 AI 服务）：
- 覆盖 `chapter.generate`，在 user 段里加一句标记，生成一章后，到假服务的日志里确认标记出现；停用后标记消失。
- 故意把 `{{ output_schema }}` 删掉，或者写一个 `{% for i in range(9) %}`，保存时都应提示错误。
- 新建风格并设为项目默认，在某一章覆盖成另一个风格，然后批量生成两章，在日志里确认两章分别注入了对应的风格段。
- 从样文提取风格并保存，再用它试写一段。
- 迁移前有 style 的项目，迁移后默认风格是"原风格"，生成出来的 prompt 与迁移前一致。

## 审查关卡与回滚点

- 第 4 步的 parity 测试是第一个关卡：不全绿，不进入第 5 步。
- 第 8 步之后跑一遍全部后端测试，再开始写前端。
- 回滚：执行 0013 的 downgrade，style 会写回 projects.style；模板改造可以按文件回退，对照实现能保证回退后行为不变。
