# 设计：提示词自定义输入表单

依赖：`11-08-prompt-components` 先完成（快照改为展开后正文、`snapshot_of` 已异步化），本任务在其上叠加输入值渲染。

## 1. 数据

`skills` 新增列 `inputs JSON NOT NULL DEFAULT '[]'`（迁移 `0043_skill_inputs.py`）：

```json
[{"name": "基调", "label": "基调", "help": "", "kind": "select", "required": false,
  "default": "轻松", "options": ["轻松", "沉重"]}]
```

- `name`：`^[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]{0,31}$`，Skill 内唯一；`label` ≤ 30；`help` ≤ 200；`kind ∈ {text, textarea, select}`。
- 值上限：`text` ≤ 200 字，`textarea` ≤ 5000 字，选项每项 ≤ 50 字、2–50 项；最多 20 项。
- Pydantic 模型 `SkillInputDef`（`extra="forbid"`），`SkillCreate/SkillUpdate/SkillOut/SkillBrief` 增加 `inputs`（Brief 也带，前端选择后无需再拉详情）。

## 2. 渲染（`ai/skill_inputs.py`，纯函数）

```python
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_一-鿿][A-Za-z0-9_一-鿿]{0,31})\s*\}\}")
def placeholders(text) -> set[str]
def resolve(defs, raw: dict[str, str]) -> dict[str, str]   # 校验 + 默认值；错误 → BadRequestError（汇总）
def fill(text, values) -> str                                # 只替换 values 中的 name，单次替换，值不再被解析
```

- 顺序：组件展开 → `fill`。输入值里的 `{{ }}`／`{{> }}` 不会被再次处理（单次 `re.sub` 回调）。
- 保存 Skill：计算 `undeclared = placeholders(展开后正文) - 声明名`、`unused = 声明名 - placeholders(...)`，作为 `SkillOut.input_warnings` 返回，不拒绝保存（旧正文兼容）；前端编辑器实时显示同样的警告。

## 3. 请求与快照

- 所有带 `skill_id` 的请求 schema 增加 `skill_inputs: dict[str, str] = {}`（≤ 20 项，总长 ≤ 60000）：`GenerateChapterRequest`、`PolishChapterRequest`、`ReviseChapterRequest`、`RewriteRequest`、`BatchGenerateRequest`、`chapter_version` 中的请求、`SceneModelOptions`、区块改写请求、智能体发送消息请求。
- `skill_service.snapshot(db, user_id, skill_id, inputs)` → `{id, name, body(展开+填充, ≤20000), inputs: 最终值}`；`skill_log` 增加 `inputs`。
- 重试／续跑／批量逐章：仍读 `input.skill` 快照，不重新渲染（沿用 `skills-mcp.md` 快照契约）。
- `skill_inputs` 非空但 `skill_id` 为空 → 422。

## 4. 智能体

- `load_skill` 结果增加 `inputs`（定义）与 `instructions`：「正文中的 {{ 名称 }} 是输入占位，按对话内容理解并代入；必填项不确定时先问作者」。正文为展开组件、未填充的文本。
- 手动指定 Skill（`AgentConversationView` 的 `SkillSelect`）：前端表单 → 请求 `skill_inputs` → 快照为已填充正文，与其他入口一致。
- 触发词命中：快照 body 为未填充正文；`forced_skill_message` 在有 `inputs` 时追加输入项定义清单与同样的代填说明。
- `start_task`：工具参数 `params.skill_inputs` 透传给任务请求；校验失败按工具层规则返回 `{"error": ...}`；预览 `skill` 增加 `inputs`，参与 `preview_changed` 哈希。

## 5. 接口

- `POST /skills/{id}/preview` `{inputs}` → `{body, missing_required: [...], errors: [...]}`；不落库，可见即可调用（全局 Skill 也可预览）。

## 6. 导入导出

- frontmatter `inputs`：列表，每项映射只允许 `name/label/help/kind/required/default/options`；标量限制同现有规则（str/int/float，`required` 允许 bool，`options` 为标量列表）；先做类型与数量检查再走 `SkillInputDef`，防 YAML 别名放大。
- 导出时 `inputs` 非空才写。

## 7. 前端

- `components/skills/SkillInputsForm.tsx`：受控组件 `{skill, value, onChange}`，按 kind 渲染 Input / TextArea / Select，带「预览」按钮（Modal 调用 preview 接口）。
- `useSkillChoice()` hook：封装 `{skillId, inputs, setSkillId, setInputs, valid, payload}`，切换 Skill 时用默认值重置；六个入口替换原 `skillId` 状态并展开 `payload` 到请求。
- `SkillEditDrawer`：输入项编辑器（增删、排序、类型切换、选项列表），正文编辑区下方提示未声明／未使用的占位。

## 8. 兼容

- `inputs` 默认 `[]`；旧 Skill 无占位替换，正文原样（PRD R2 兼容条款）。
- parity 测试不变（`skill_guide` 值只是字符串）。
