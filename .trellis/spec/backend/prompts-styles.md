# 提示词模板与写作风格

> 来源任务：`10-23-prompt-styles`（迁移 `0013`）；提示词组件来自 `11-08-prompt-components`（迁移 `0042`）。任务 / SSE 契约见 [error-handling.md](./error-handling.md)，
> 表结构与外键规则见 [database-guidelines.md](./database-guidelines.md)，章节上下文见 [story-entities.md](./story-entities.md)。

---

## 场景：提示词模板（系统默认 + 用户覆盖）

### 1. 范围 / 触发条件

任何新增或修改 AI 提示词的代码：`app/ai/prompts/*.py` 的 `build_*_messages`、`app/ai/prompts/templates/*.j2`、
`app/ai/prompts/registry.py`、`app/ai/templating.py`、`prompt_service.py`、`/api/v1/prompt-templates`。
**提示词文本只能写在 `.j2` 默认模板里**；Python 只负责把数据拼成变量。

### 2. 签名

```python
# app/ai/templating.py
PART_LIMIT = 20000; RENDER_LIMIT = 200000; MAX_NAME_REFS = 200
@dataclass(frozen=True) class TemplateVar: name, label, kind: text|int|bool|list, required=False, locked=False, sample=""
@dataclass(frozen=True) class PromptOverride: system, user, version_id: int | None
def validate(decls, system, user) -> list[TemplateIssue]           # TemplateIssue(part: system|user|None, line, message)
def render_messages(decls, system, user, values, *, key="") -> RenderResult   # TemplateRenderError(BadRequestError)
# app/ai/prompts/registry.py
TEMPLATES: dict[str, PromptTemplate]; TEMPLATE_KEYS
def render(key, override: PromptOverride | None, values) -> list[ChatMessage]   # values 必须覆盖全部声明变量
def fingerprint(system, user) -> str       # sha256(system + "\x00" + user)[:16]
def default_fingerprint(key); def sample_values(key); def prompt_meta(key, override) -> {key, source, override_version_id}
# build_* 签名不变，新增关键字参数 override: PromptOverride | None = None
# app/services/prompt_service.py
MAX_VERSIONS = 20
async def get_override(db, user_id, key) -> PromptOverride | None     # 只返回启用的覆盖
async def save_override / patch_override / delete_override / list_versions / restore_version / import_overrides
def preview(key, PromptPreviewRequest) -> PromptPreviewOut
```

### 3. 契约

**模板 key（31 个；批量关系生成新增 `relations.generate`，`test_prompt_templates` 的计数同步为 31）**

| 分类 | key | 锁定变量 |
|---|---|---|
| 章节 | `chapter.generate`、`chapter.continue`（只有 user 段，结果只取 user）、`chapter.polish`、`chapter.revise`、`chapter.rewrite`（`op` 在模板里 if 分支） | generate/polish/revise/rewrite：`planning_context`、`scene_context`；rewrite另有`setting_context` |
| 分析 | `analysis.chapter` | `output_schema`（有伏笔清单时是带 ref_id 的结构）、`json_rule` |
| 向导 | `wizard.world` / `wizard.characters` / `wizard.outline` / `wizard.careers`；`wizard.continue`（从中断处继续的续写指令，只有 user 段，变量 `generated_chars`，接在原对话 + assistant 已输出内容之后） | `json_rule`、`output_format`（`wizard.continue` 无） |
| 实体 | `entity.character` / `entity.organization` / `entity.career`、`relations.parse` / `relations.generate` | `json_rule`、`output_format` |
| 大纲 | `outline.continue` / `outline.expand` | `json_rule`、`output_format` |
| 风格 | `style.block`（风格段，只取 user 段并 strip）、`style.extract`（JSON）、`style.trial` | extract：`json_rule`、`output_format` |
| 灵感模式 | `inspiration.title` / `synopsis` / `theme` / `tags` / `selling_points` / `audience`（同一组变量 `idea / notes / decided / history / feedback / knowledge`，步骤约束写在各自模板里，见 [inspiration.md](./inspiration.md)） | `json_rule`、`output_format`（`{"options": [...]}`） |
| 拆书 | `book_import.digest` / `book_import.synthesize`（见 [book-import.md](./book-import.md)） | `json_rule`、`output_format` |
| 项目智能体 | `agent.system`（只有 system 段；变量 `safety_rules` / `project_info` / `page_hint` / `skills_catalog`，后三者由服务端用【不可信数据】标记包裹）、`agent.summarize`（压缩较早对话；变量 `summary` / `history`（必需），输出纯文本摘要） | `agent.system`：`safety_rules`（= `app/ai/prompts/agent.py::SAFETY_RULES`）；`agent.summarize` 无 |

- **检索块变量**（`10-23-knowledge-base`，见 [retrieval.md](./retrieval.md)）：`knowledge`（知识库参考资料正文，`KNOWLEDGE_VAR`）声明在
  `wizard.*`（4 个）、`entity.character` / `entity.organization` / `entity.career`、`relations.generate`、`outline.continue` / `outline.expand`、`inspiration.*`（6 个）；
  `memories`（相关旧情节，`MEMORIES_VAR`）声明在 `outline.continue` / `outline.expand`。默认模板写
  `{% if knowledge %}\n\n【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】\n{{ knowledge }}\n{% endif %}`（一般放在
  「作者额外要求」之前；续写放在「发展方向」之前，`memories` 放在「前文进展」之后 / 展开放在「相邻章纲」之后），**变量为空时整段不输出，parity 不变**。
  build 函数新增关键字参数 `knowledge: str = ""`（大纲另有 `memories: str = ""`）。章节类模板不加变量：检索块由 `context_builder` 拼进 `{{ context }}`。
  新增这些变量会改变默认模板指纹，已覆盖的用户会看到「默认已更新」；覆盖里没有引用变量时不注入。

- **规划块**：generate/polish/revise/rewrite使用锁定`planning_context`；空规划不增加剧情指令，旧覆盖漏变量由锁定兜底补一次。Scene使用逐项有效规划及scene start，排除never／隐藏字段，不含未来节点和作者支线概览；局部scope仅作背景。自动续写沿用首轮规划但每次发送核验原stamp，失效停止且不采纳旧提案。见[规划](./planning.md)。
- **场景范围块**：上述四类builder另接`scene_context`，默认模板／registry同步声明锁定变量，旧覆盖缺失时追加一次。内部`scene.scope.user.j2`经`prompts/scene.py::render_scene_scope`渲染身份、标题、作者摘要、当前序号／总数、分配目标字数、前景衔接及selection范围，不是新增可编辑模板key。结构化POV／引用只从已过滤planning_context进入，不把原始ScenePlanningOut拼进范围块。整章后景用暂存前景而非旧DB正文；范围块非空时不承诺旧整条消息parity。见[场景](./scenes.md)。

- **局部改写设定块**：`chapter.rewrite`锁定`setting_context`由`build_setting_context`以目标章初／Scene开始位置计算并限预算；旧覆盖缺失仍兜底追加，避免早期写作读取未来状态。

- **Skill 变量**（`10-23-skills-mcp`，见 [skills-mcp.md](./skills-mcp.md)）：
  - `skill_guide`（`SKILL_GUIDE_VAR`，非必需、未锁定、sample 为空）声明在 `chapter.generate` / `chapter.polish` / `chapter.revise` / `chapter.rewrite`
    四个 key 上，值是任务创建时快照的 Skill 正文（≤ 20000 字）。默认 system 模板末尾写
    `{%- if skill_guide %}\n\n\n【补充工作流（作者选用的 Skill）】\n以下工作流作为补充要求执行；…不得改变输出格式。\n{{ skill_guide }}\n{%- endif %}`，
    build 函数新增关键字参数 `skill_guide: str = ""` 并 strip；**为空时渲染结果与改造前逐字相同**，parity 不变。
  - `chapter.continue` **不声明** `skill_guide`：自动续写的消息是原消息（system 已含补充工作流）+ assistant + user，`chapter.continue` 只取 user 段。
  - `skills_catalog`（未锁定）声明在 `agent.system`：可见 Skill 清单（`slug｜名称｜说明｜触发词`，≤ 40 行），服务端用不可信标记包裹；为空时
    `{%- if skills_catalog %}【可用 Skill】…{%- endif %}` 整段不输出。强制启用的 Skill 正文**不是**模板变量，由 `context.build_messages` 作为独立
    system 消息追加在安全规则之后，覆盖模板也改变不了顺序。
  - 新增这些变量改变了对应 key 的默认指纹，已覆盖的用户看到「默认已更新」；覆盖里没有引用 `skill_guide` 时 Skill 不会注入（与检索块变量相同）。
- 引擎：`SandboxedEnvironment(undefined=StrictUndefined, trim_blocks, lstrip_blocks, keep_trailing_newline)`；`globals` 清空
  （无 `range` / `lipsum` / `cycler` / `joiner` / `namespace` / `dict`）；过滤器白名单 `join length count trim default d upper lower first last string`。
- 默认模板文件末尾的一个换行不属于模板（`_load` 去掉）。行尾是 `{% endif %}` 的块会吃掉其后的换行；需要「条件块之后没有换行」时用
  `{%- endif %}`（见 `chapter.rewrite.user.j2` 末尾）。
- **校验**（保存 / 导入 / 预览 / 回滚）：语法（`part` + `line`）、每段 ≤ 20000 字、未知变量（带行号）、必需变量出现、锁定变量恰好出现一次且只能以
  `{{ name }}` 直接写在顶层（不在 if / for 内、不加过滤器、不参与条件表达式）、`for` 只能遍历声明为 `list` 的变量且不能嵌套 / recursive、
  禁止 `set` / `macro` / `call` / `filter` / `include` / `import` / `extends` / `block` / `with`、禁止函数 / 方法调用、下划线属性、`*` / `**` / `%`（`'%0999999999d' % 1` 可耗尽内存）、`self`、
  无 else 的条件表达式、变量引用 > 200 处。失败 → 400，`detail = "模板校验失败：用户段第 3 行：未知变量：x；…"`，响应体另带
  `errors: [{part, line, message}]`（`AppError.extra`）。
- **渲染**：沙箱环境拦截 `*` / `**` / `%` 二元运算（运行时兜底，覆盖绕过校验的旧数据）；用 `generate()` 边生成边计数，每段 > 200000 字立即报错；
  合法模板也可能运行时失败（如访问不存在的属性 `{{ x.nope }}`），自动续写指令渲染失败按「续写失败」处理（warning，首轮正文照常落库）；锁定内容未完整出现在结果里时追加到 user 段末尾并记 warning（兜底，校验已保证）；system 渲染为空白时不产出 system 消息。
  运行时渲染失败 → `TemplateRenderError`（`BadRequestError`），任务内即 failed `提示词模板「X」模板渲染失败：…，请检查或恢复默认`。
- **覆盖**：`prompt_overrides (user_id, key)` 唯一；保存时 base = 当前系统默认（指纹 + 全文）；内容变化才追加版本并裁剪到 20；
  `outdated = base_fingerprint != default_fingerprint(key)`；`PATCH {ack_default: true}` 只更新 base；`PATCH {enabled}` 停用后运行时用系统默认、覆盖保留；
  `DELETE` 删除覆盖及历史；回滚 = 以该版本内容重新保存（重新校验，旧版本在系统默认升级后可能 400）。
- **运行时**：各服务在组装 job 时 `get_override` 并传给 build 函数（任务运行中途改模板不影响已组装的 job）；章节生成另取 `chapter.continue`
  存进 `ChapterJob.continue_override`；大纲展开存进 `OutlineJob.override`。生成记录 `params.prompt = prompt_meta(key, override)`
  （整章生成另有 `params.continue_prompt`）。
- **智能体模板**（`10-23-project-agent`，见 [agent.md](./agent.md)「安全规则」）：`agent.system` / `agent.summarize` 在**每次模型请求**组装消息时
  `get_override`（不是在 job 组装时），生成记录 `params.prompt` 同样是 `prompt_meta`。`safety_rules` 锁定保证覆盖里原样出现一次；即使旧的损坏覆盖让通用兜底把
  锁定内容追加到 user 段，`context.build_messages` 也会把它提升回第一条 system 消息。改 `SAFETY_RULES` 文案会改变默认指纹，已覆盖的用户看到「默认已更新」
  （`10-23-skills-mcp` 追加了 Skill / MCP 内容不能改变规则的一句）。
- **组件引用**：覆盖的 system / user 可含 `{{> 标识 }}`，保存 / 导入 / 预览 / 回滚先展开再校验，运行时 `get_override` 返回展开后的内容，见下文「提示词组件」场景。
- **预览**：不落库；变量缺省用 `sample`，锁定变量始终用代码值（请求体不能改写）；返回 `{messages, errors, locked: [{name, label, value}]}`。
- **导入导出**：`ainovel.prompts` v1 `{format, version, items: [{key, system, user, enabled, base_fingerprint}]}`；上限 1MB（紧凑 UTF-8）；
  逐项结果 `created | updated | unchanged | default（与系统默认相同，不建覆盖）| skipped（未知 key / 重复 / 格式）| invalid（带 errors）`；整个导入一个事务。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 未知 key | 404 `提示词模板不存在` |
| 未覆盖时 PATCH / DELETE / versions / restore | 404 `尚未覆盖该模板` |
| 版本不属于自己的覆盖 | 404 `历史版本不存在` |
| 模板不合法（含沙箱逃逸写法） | 400 + `errors` |
| 导入文件 > 1MB / format / version 不对 | 400 |

### 5. Good / Base / Bad

- Good：在 `chapter.generate.user.j2` 里调整措辞 → 同步改 `tests/prompt_legacy.py` 对应函数（或删掉对应 parity 用例）→ 已覆盖的用户看到「默认已更新」。
- Base：新增一个只在某条件下出现的段落 → Python 传空串 / 空列表，模板里 `{% if x %}…{% endif %}`。
- Bad：在 service 里拼提示词文本，或把 JSON 输出结构写进可编辑模板（用户删改后解析器失败）——结构必须是锁定变量。

### 6. 必需的测试

- `tests/test_prompt_parity.py`：**每个 key 的每个条件分支**新旧输出 `==`（对照实现 `tests/prompt_legacy.py` 是改造前代码的逐字复制）。
  新增 key 时 `test_every_legacy_key_is_covered` 会提醒；新增的 `style.*` / `inspiration.*` / `book_import.*` / `agent.*` / `wizard.continue` 没有对照。
- `tests/test_templating.py`：每种校验错误、沙箱逃逸、range 禁用、渲染上限、锁定兜底、全部默认模板合法且能用示例值渲染。
- `tests/test_prompt_templates.py`：覆盖生效 / 停用 / 启用 / 恢复默认、位置信息、预览、outdated + ack（monkeypatch `registry.TEMPLATES`）、
  版本裁剪与回滚、导入导出、用户隔离。
- `tests/test_skill_generation.py::test_skill_guide_only_rendered_when_present`：`skill_guide` 为空 / 空白时四个章节类模板与不传时 `==`，非空时只在 system 段末尾追加补充工作流段。

---

## 场景：提示词组件（`{{> 标识 }}` 引用与组件库）

> 来源任务：`11-08-prompt-components`（迁移 `0042`）。Skill 侧见 [skills-mcp.md](./skills-mcp.md)「Skill 正文中的组件引用」，工坊分享见 [workshop.md](./workshop.md)。

### 1. 范围 / 触发条件

修改 `app/ai/components.py`（解析 / 展开纯函数）、`app/services/component_lookup.py`、`app/services/prompt_component_service.py`、
`/api/v1/prompt-components`、`prompt_components` 表，或在任何会读写模板覆盖 / Skill 正文的地方（保存、导入、导出、预览、回滚、运行时、工坊分享）时。
**凡是把覆盖或 Skill 正文交给 `validate` / Jinja / 他人的地方，都必须先展开组件。**

### 2. 签名

```python
# app/ai/components.py（纯函数，不访问数据库）
REF_RE = re.compile(r"\{\{>\s*([a-z0-9][a-z0-9-]{0,47})\s*\}\}")   # 标识规则与 Skill slug 相同（check_slug）
def has_refs(text) -> bool                     # 含 "{{>" 即为真（快速判断，不含时一切逻辑短路、原样返回）
def find_refs(text) -> list[(slug, line)]; def ref_slugs(text) -> list[str]   # 去重保序
def bad_ref_lines(text) -> list[int]           # `{{>` 开头但不匹配 REF_RE 的行号
@dataclass Expansion{text, source, pieces, errors: [(原文行号, slug, 说明)], used}; Expansion.map_line(line) -> (原文行号, slug | None)
def expand(text, contents) -> Expansion        # 单次替换、不递归；不存在 → 替换为空并记错；组件内含引用 → 记错
def expand_strict(text, contents) -> str       # 有错误 → ComponentRefError(slug, line, message)
def expand_lenient(text, contents) -> str      # 只替换解析得到的引用，其余原样保留（仅 Skill 运行时用）
# app/services/component_lookup.py（只读，按 user_id 过滤）
async def contents(db, user_id | None) -> {slug: content}     # user_id 为空（全局资源）→ {}
async def refs(db, user_id | None, *texts) -> [{id, slug, name}]   # 文本中引用且本人确实存在的组件，按首次出现顺序
# app/services/prompt_service.py
def check(tpl, system, user, contents=None) -> (issues, 展开后 system, 展开后 user)   # 先展开再 validate，行号映射回原文
def ensure_valid(tpl, system, user, contents=None) -> (展开后 system, 展开后 user)   # 失败 400（与原契约同）
async def preview(db, user_id, key, data) -> PromptPreviewOut                     # 原为同步 preview(key, data)
# app/services/prompt_component_service.py
MAX_COMPONENTS = 200
async def list_components(db, user_id, q="") / get_component / to_out / create_component / update_component / delete_component
async def export_components(db, user_id) / import_components(db, user_id, data)
```

| 方法与路径 | 说明 |
|---|---|
| `GET /prompt-components?q=` | `list[ComponentBrief]`，按 slug 排序；`q`（≤100）在 slug / 名称 / 说明 / 内容中 `ilike` 搜索（转义 `% _ \`） |
| `POST /prompt-components` | 201 `ComponentOut` |
| `GET /prompt-components/export` | `ainovel.prompt-components` v1 |
| `POST /prompt-components/import` | `{data}` → `{items: [{slug, status, message, failures}]}` |
| `GET \| PUT \| DELETE /prompt-components/{id}` | 详情（含 `content`、`usages`）/ 带 `revision` 的整体更新 / 删除 204 |

固定路径 `/export`、`/import` 注册在 `/{component_id}` 之前。

```sql
-- 0042：prompt_components
id PK; user_id FK users.id ON DELETE CASCADE（索引 ix_prompt_components_user_id）;
slug VARCHAR(48); name VARCHAR(100); description VARCHAR(500) DEFAULT ''; content TEXT DEFAULT '';
revision INT DEFAULT 1 CHECK (revision >= 1); created_at / updated_at; UNIQUE(user_id, slug)
```

### 3. 契约

- **语法**：`{{> 标识 }}`，空白可有可无，标识为 `^[a-z0-9][a-z0-9-]{0,47}$`。在 Jinja 中 `{{>` 本是语法错误，所以旧覆盖不可能含引用，迁移无需回填；
  **没有引用时所有路径与改造前逐字相同**（`has_refs` 短路）。前端 `utils/promptComponents.ts::REF_RE` 与后端同口径。
- **不嵌套**：组件内容含 `{{>` → 保存 / 导入 400 `组件内不能引用其他组件`；展开时组件内容里的引用不再展开并记错。
- **库内存原文**：`prompt_override_versions` / `prompt_overrides` / `skills.body` 一律保存带引用的原文，展开只发生在读取时。
- **覆盖保存 / 导入 / 预览 / 回滚**：先用 `component_lookup.contents(db, user_id)`（只取本人的组件）展开，再对展开结果走原有全部 `validate`（锁定变量、未知变量、
  长度 20000 等都按展开后计算，所以组件内容可以使用该模板已声明的变量）。引用错误（不存在 / 嵌套 / 格式不对）先于模板校验返回，`TemplateIssue(part, 原文行号, 说明)`，
  例如 `引用的组件不存在：x`、`组件 x：组件内不能引用其他组件`、`组件引用格式不正确，应为 {{> 标识 }}（…）`。模板校验问题经 `Expansion.map_line`
  映射回原文行号；落在组件展开范围内的问题行号取引用所在行，`message` 加前缀 `组件 {slug}：`。
- **运行时**：`get_override` 返回**展开后**的 `PromptOverride`（`expand_strict`），各 build 函数与 `render` 无需感知组件。保存后组件因竞态消失 →
  `TemplateRenderError("提示词模板「{名称}」引用的组件 {slug} 不存在，请检查")`；嵌套错误同样以「提示词模板「名称」+ 说明 + ，请检查」报出。任务内即 failed，不把 `{{>` 交给 Jinja。
- **导出覆盖**：`export_overrides` 写展开后的 `system` / `user`（离开作者本人；展开失败时保留原文，理论上不会发生）。
- **响应字段**：`PromptTemplateDetail.components` = 已保存覆盖原文引用的本人组件；`PromptPreviewOut.components` = 请求体模板引用的本人组件；均为 `[{id, slug, name}]`，
  只列存在的组件。
- **组件库**：只属于作者本人，他人的组件一律 404 `组件不存在`，展开也只查本人组件（所以引用他人标识 = 引用不存在的组件）。每人上限 `MAX_COMPONENTS = 200`
  （超出 400 `最多保存 200 个组件`）；`content` ≤ 20000 字（`CONTENT_LIMIT`）、名称 1–100（strip 后非空）、说明 ≤ 500（strip）。用户级资源，不进项目备份。
- **使用关系**：不建关系表，查询时扫描本人 `Skill.body` 与覆盖 `system` / `user` 中含 `{{>` 的行，得到 `ComponentUsage{kind: skill|override, id(Skill), key(覆盖), name, enabled(覆盖启用状态，Skill 为 null)}`；
  `ComponentBrief.usage_count` = 引用方数量，`ComponentOut.usages` 为完整列表。
- **引用完整性**：
  - 删除或修改标识时有引用方 → 409，`detail = 组件 {slug} 正被 Skill「A」、模板「B」引用，不能删除|修改标识；请先移除这些引用`，`code = component_in_use`，`usages = [...]`。
  - 修改内容时用新内容展开并 `check` 每个引用它的覆盖（**停用的覆盖也算**），有失败 → 400，`code = component_breaks_usages`，
    `failures = [{key, name, errors: [{part, line, message}]}]`，`detail` 逐条列出模板与原因。Skill 正文不参与影响校验（正文不经 Jinja）。
- **并发**：`ComponentUpdate.revision`（≥1）与当前不符 → 409 `code = stale_revision`（`组件已被修改，请加载最新版本后再保存；当前草稿可继续保留`）；
  UPDATE 以 `revision` 为 CAS 条件，rowcount ≠ 1 同样 409。内容完全没变时不增加修订号。
- **标识冲突**：新建 / 改标识撞到本人已有标识 → 409 `标识已被使用，请换一个`（先查询，`IntegrityError` 兜底）。
- **导入导出**：`{format: "ainovel.prompt-components", version: 1, items: [{slug, name, description, content}]}`（不含 id / 修订号）；上限 1MB（紧凑 UTF-8）；
  逐项 `created | updated | unchanged | invalid`：按标识新建或更新，字段校验失败 / 文件内重复（只取第一项）/ 内容含引用 / 超过 200 → invalid；
  更新内容同样做影响校验，失败时 invalid 并带 `failures`；更新时 `revision + 1`。整个导入一个事务。
- **迁移 0042**：只建表、不动已有数据。`downgrade` 时表中有数据 → `RuntimeError("仍有提示词组件，拒绝有损降级；…")`，空表才删表。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 读 / 改 / 删他人的组件或不存在的 id | 404 `组件不存在` |
| slug 不合法、名称空、超长（内容 > 20000、说明 > 500） | 422 |
| 组件内容含 `{{>` | 400 `组件内不能引用其他组件` |
| 超过 200 个 | 400 `最多保存 200 个组件` |
| 标识冲突 | 409 `标识已被使用，请换一个` |
| 更新时 `revision` 过期 | 409 `code=stale_revision` |
| 删除 / 改标识时被 Skill 或覆盖引用 | 409 `code=component_in_use` + `usages` |
| 改内容让引用它的覆盖校验失败 | 400 `code=component_breaks_usages` + `failures` |
| 覆盖保存 / 导入 / 回滚引用不存在或格式不对的组件 | 400 + `errors`（原文行号）；导入为该项 invalid |
| 运行时覆盖引用的组件已不存在 | `TemplateRenderError`，任务 failed `提示词模板「X」引用的组件 x 不存在，请检查` |
| 导入文件 > 1MB / format / version 不对 / items 非数组 | 400 |
| 降级 0042 时仍有组件 | `RuntimeError`，拒绝降级 |

### 5. Good / Base / Bad

- Good：覆盖 user 段写 `{{> ban }}`，组件内容 `{{ title }}` 是该模板已声明变量 → 保存通过，运行时 `get_override` 返回展开文本，渲染结果与手写相同。
- Base：模板覆盖没有任何 `{{>` → `check` 等同 `validate`，`get_override` / 导出 / 预览与改造前逐字相同。
- Bad：组件内容写 `{{ unknown }}` → 保存覆盖 400，`message` 为 `组件 ban：未知变量：unknown`，`line` 是 `{{> ban }}` 所在原文行；
  若是修改已被引用的组件内容造成的 → 400 `component_breaks_usages`。

### 6. 必需的测试

- `tests/test_prompt_components_expand.py`：`test_find_refs_and_slugs`、`test_expand_without_refs_is_identity`（无引用逐字不变）、
  `test_expand_replaces_in_place_and_maps_lines`、`test_expand_unknown_nested_and_bad_format`、`test_check_maps_issue_to_component_source`（行号 + 「组件 x：」前缀）、
  `test_defaults_and_builtin_skills_have_no_refs`（默认模板与内置 Skill 不含 `{{>`）、`test_expand_lenient_keeps_unresolved_refs_literal`。
- `tests/test_prompt_components.py`：`test_crud_revision_search_and_validation`（409 冲突、422、嵌套 400、stale_revision、搜索）、`test_quota`、
  `test_other_users_components_invisible_and_unreferenceable`、`test_override_validation_reports_component_source`、
  `test_override_and_skill_use_components_at_runtime_and_snapshot`、`test_integrity_and_usages`（component_in_use + usages、component_breaks_usages + failures）、
  `test_exports_and_workshop_share_are_expanded`、`test_global_skill_cannot_reference_components`、`test_agent_load_skill_reads_expanded_body`、
  `test_component_library_import_export`、`test_runtime_missing_component_is_readable_error`（断言错误含 `引用的组件 ban 不存在，请检查`）。
- `tests/test_migrations.py::test_0042_prompt_components`：`(user_id, slug)` 唯一、删用户级联、有组件时降级抛错。

### 7. 错误 vs 正确

```python
# 错误：直接校验 / 渲染原文——`{{>` 在 Jinja 中是语法错误，组件内容里的变量也不会被检查
issues = validate(tpl.variables, data.system, data.user)
return PromptOverride(system=row.system, user=row.user, version_id=version_id)
# 正确：先用本人的组件展开，再校验 / 返回展开后的内容；问题行号映射回原文
issues, system, user = prompt_service.check(tpl, data.system, data.user, await component_lookup.contents(db, user_id))
system, user = expand_strict(row.system, contents), expand_strict(row.user, contents)   # get_override

# 错误：把展开后的文本写回库——之后修改组件不再生效，使用关系也查不到
row.system = exp_system
# 正确：库内存原文，只在读取（运行时 / 导出 / 分享）时展开
```

---

## 场景：写作风格库与解析

### 1. 范围 / 触发条件

`style_service.py`、`/api/v1/styles`、`projects.default_style_id`、`chapters.style_id`、所有章节类生成入口（generate / polish / revise / rewrite / batch）。

### 2. 签名

```python
StyleChoice = Literal["inherit", "none"] | int    # schemas/style.py
async def resolve(db, user_id, project, chapter | None, choice="inherit") -> ResolvedStyle | None   # {id, name, description, block, source}
async def validate_choice(db, user_id, choice) -> None      # 创建任务时同步校验临时指定的风格（400）
async def ensure_assignable(db, user_id, style_id | None)   # 项目 / 章节 PATCH（400）
async def default_description(db, project) -> str           # ProjectInfo.style
async def sync_presets(sessionmaker)                        # lifespan；按 preset_key upsert
def style_log(resolved) -> {id, name, block, source} | None # 生成记录 params.style
```

### 3. 契约

- 预设 6 个（`app/models/style_presets.py`，迁移 0013 内冻结副本，`test_0013_prompt_styles` 断言一致；`create_all` 由 `after_create` 写入）：
  `user_id` 为空，所有用户可见、只读（PATCH / DELETE → 403 `预设风格不能修改或删除，可以复制后修改`），可复制（`名称（副本）`、`（副本2）`…）。
- 可见性：预设或本人所有；他人风格一律 404。项目默认 / 章节覆盖 / 临时指定只能指向可见风格（400）。
- **优先级**：请求 `style`（整数）> `chapter.style_id` > `project.default_style_id` > 无；`"none"` 直接不用风格。风格段 = `style.block` 渲染结果，
  作为章节类模板的 `{{ style }}`；默认 `style.block` 在只有 `extra`（且 description 是 extra 的子串）时原样输出 extra，所以迁移来的「原风格」提示词与改造前逐字一致。
- **向导 / 实体 / 分析 / 大纲**以及章节上下文的【作品信息】：`ProjectInfo.style` = 项目默认风格的 `description`（`project_service.project_info(db, project)`，异步）。
- **【作品信息】的结构化字段**（`0014`）：`format_project_info` 依次输出 `书名`、`题材`、`一句话设定`、`写作风格`，之后按
  `类型标签：A、B` → `主题` → `简介` → `核心卖点` → `目标读者` 追加；**每项去空白后非空才输出**，所以字段全空时与改造前逐字一致
  （parity 测试照常通过，`tests/prompt_legacy.py` 不改）。`target_words` 不进提示词（只用于概览进度与向导参数建议）。
  新增注入字段时同样「有内容才输出」，并在 `tests/test_inspiration.py::test_format_project_info_each_field` 补一行。
- **批量**：`BatchGenerateRequest.style` 存在 `input.request`，逐章 `GenerateChapterRequest(style=...)` 解析（inherit 时各章按自己的覆盖），续跑 / 重试沿用；
  `validate_start` / `revalidate` 校验临时指定。
- **生成记录** `params.style = {id, name, block, source: request|chapter|project} | null`，另有 `params.style_choice`；选用 Skill 时另有
  `params.skill = {id, name}`（不含正文，见 skills-mcp.md）。
- **删除**：有引用且没有 `?force=true` → 409，`references: {projects, chapters}`（只统计本人数据）；force 后外键 SET NULL，章节回落到项目默认、项目回落到无。
- **提取 / 试写**：同步 SSE（不抢 generating、不写生成记录）。提取：样文 200–10000 字 → `delta`… → `done {draft, warnings}`（字段截断到列长度，不落库）/
  解析失败 `error`；试写：`style_id` 或未保存的 `style` 字段 + 场景 ≤500 + 字数 200–1000 → `delta`… → `done {text}`。
- **导入导出**：`ainovel.styles` v1，items 不含 id；无 ids 时导出自己的全部风格；导入上限 1MB / 200 项，同名加「（导入）」「（导入2）」。
- **兼容**：`ProjectCreate/Update.style`（已废弃，保留一个版本）→ `apply_legacy_style` 创建或更新「《书名》原风格」并设为默认，响应 `notice` 提示；
  空串 = 当前默认是原风格时取消默认。`ProjectOut` 不再返回 `style`，改为 `default_style_id` + `default_style`（`StyleBrief`）。

### 6. 必需的测试

`tests/test_styles.py`（预设只读 / 复制、四种优先级 + 生成记录、polish / revise / rewrite、批量逐章与续跑沿用、删除引用回落、提取 / 试写不落库、
向导只带描述、旧字段兼容且提示词与对照实现相等、导入导出、跨用户、预设同步）；`test_cross_user_isolation` 含 `/styles/{id}` 四个路由。
