# 提示词组件与自定义输入表单

## Goal

落地 TODOS 第 10 项：作者把常用提示词片段沉淀为可复用组件，在 Skill 与模板覆盖中引用并查看使用关系；为自己的 Skill 定义输入项，使用时填写表单、预览填入结果。本任务为父任务，负责需求来源、子任务映射与集成验收，本身不直接实现。

## 子任务映射与顺序

| 顺序 | 子任务 | 交付 |
|---|---|---|
| 1 | `11-08-prompt-components` | 组件库、`{{> 标识 }}` 引用与展开、引用完整性、使用关系、分享／导出展开、组件库导入导出 |
| 2 | `11-08-prompt-inputs` | Skill 输入项定义、`{{ 标识 }}` 占位、六个入口的表单与预览、服务端校验与快照、智能体代填、SKILL.md 往返 |

子任务 2 依赖子任务 1 的「快照为展开后正文」（写在其 `design.md`／`implement.md`）。

## 现状（代码证据）

- **系统提示词模板**：31 个固定 key（`backend/app/ai/prompts/registry.py` `_DEFS`），变量由代码声明与提供，作者不能新增。
- **用户覆盖**：`PromptTemplateOverride`（`backend/app/models/entities.py:815`）按「用户 + key」唯一，对作者全部作品生效；有启用开关、outdated 三方对比、历史版本、`ainovel.prompts` v1 导入导出、预览接口；无 RBAC 动作，仅本人资源。
- **模板引擎**：沙箱 Jinja2 + AST 白名单（`backend/app/ai/templating.py`），禁止 include／macro／函数调用。
- **Skill**：作者自写 Markdown 工作流（`backend/app/models/skills_mcp.py`），本人 + 全局两级可见；任务创建时快照 `id/name/body` 并以 `skill_guide` 注入（`skill_service.snapshot_of`）。
- **Skill 使用入口**：作者手动选择——章节生成／续写（`ChapterEditorPage`）、改写（`RewritePanel`）、批量（`BatchGenerateModal`）、场景 AI（`SceneWritingPanel`）、区块改写（`RichSectionRewriteModal`）、智能体对话（`AgentConversationView`）；智能体自行调用 `load_skill`、触发词强制启用、`start_task` 带 `skill_id`（`services/agent/tools/skills.py`、`tools/references.py:69`）。
- **分享**：工坊只分享 `style` 与 `prompt`（`backend/app/schemas/workshop.py:8`）；Skill 有独立 SKILL.md／zip 导入导出。作品备份（`services/project_transfer/`）不含 Skill 与模板覆盖。

## 已定决策（2026-11-08）

- **D1** 输入表单挂在 Skill 上；系统模板覆盖不新增输入项。
- **D2** 组件可在 Skill 正文与模板覆盖中引用；覆盖先展开再执行原有沙箱校验；组件修改即时影响新任务。
- **D3** 组件归属作者个人，对其全部作品生效。
- **D4** 首版不支持组件嵌套。
- **D5** 内容离开作者本人（Skill 导出、覆盖导出、工坊分享）时展开为正文；全局 Skill 禁止引用组件；组件库独立导入导出。
- **D6** 六个手动入口都展示表单（批量整批一份）；智能体自行加载或触发词启用时由智能体按对话代填，必填不确定时追问；`start_task` 可传输入值并校验。

## 跨子任务验收

- [ ] 一个 Skill 同时引用组件并声明输入项：组件内容中的占位也能被表单值填充；预览、实际注入与生成记录一致。
- [ ] 带组件与输入项的 Skill 导出 SKILL.md 后导入到另一账号：正文已展开、输入项保留、可正常使用。
- [ ] 不使用组件与输入项时，全部模板 parity 测试与现有 Skill 行为不变。
- [ ] 后端、前端质量门禁全部通过；README 与相关 spec 已同步。

## Out of Scope

- 系统模板覆盖声明输入项；新建独立「自定义提示词／动作」实体。
- 组件嵌套、组件参数、管理员级全局组件库、工坊分享组件或 Skill。
- 其他输入类型（数字、复选、多选、日期）与条件显示；记住上次填写值。
- 表单中预览含完整上下文的最终提示词。
