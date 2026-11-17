# Research: 设定图片默认提示词接入模板库

- Query: 复用现有提示词模板，让用户为作品和系列设定图片自定义、保存、预览及恢复默认提示词，同时保留用户当前修改。
- Scope: internal
- Date: 2026-11-17

## Findings

### 现有入口与归属

- `backend/app/ai/prompts/setting_thumbnail.py:8`：当前默认由类别和已投影字段拼装，按字段限制 400 字并有 2000 字总预算；用户刚手改结尾为“画面中不要出现除了当前角色以外的人物、文字、字母、数字或水印。”，迁移到默认模板时须逐字保留。此文字当前适用于四类主体，不能默默只保留给角色。
- `backend/app/services/project_thumbnail_ai.py:75`：作品提示词字段先经 AI 可见性与实体范围投影；禁止跨条目引用字段和隐藏字段自动带入。来源指纹与访问凭据在 `default_prompt` 中生成（约第 165 行）。
- `backend/app/services/series_thumbnail_ai.py:93`、`:151`、`:232`：系列先验证接受的系列权限并过滤隐藏字段，再构建同一个默认提示词；`never` 禁止自动生图。
- `backend/app/api/v1/setting_images.py:91`、`:116`：两条获取提示词的路由均传当前登录用户 `user.id`，已有 2000 字生成入参边界（`backend/app/schemas/setting_image.py:23`）。
- `backend/app/services/prompt_service.py:1`、`:354`：覆盖按 `(user_id, key)` 隔离，`get_override(db, user_id, key)` 只读取当前用户的启用覆盖，并正确展开该用户的提示词组件。调用时应传真实发起人 `actor_id`，包括作品协作者借用作者授权图片模型的情况；不能读取作品或系列作者的私人模板。

### 注册、编辑与渲染

- `backend/app/ai/prompts/registry.py:21`、`:106`、`:527`：添加 key、名称、`entity` 分类、说明和声明变量即可注册；必须提供 `templates/<key>.system.j2` 和 `templates/<key>.user.j2`。注册后，列表、详情、校验、保存、启停、恢复默认、历史版本、导入导出和工坊现有能力按 key 复用；不需要新表或迁移。
- `frontend/src/router.tsx:52`、`frontend/src/pages/PromptTemplatesPage.tsx:21`、`:132` 和 `frontend/src/utils/prompts.ts:66`：模板列表和分类由后端返回值动态分组，无固定 key 或分类清单。放在已有“设定生成”分类即可显示；生图弹窗可直接使用 `/prompt-templates?key=<新注册key>` 深链，建议新标签打开以保留弹窗草稿，保存模板后重新打开弹窗读取最新值。
- `frontend/src/components/prompts/PromptEditor.tsx:78`、`:98`、`:194`：默认 system 为空时不展示系统段，通用页面已有保存、停用和恢复。尽管 UI 隐藏空系统段，API、导入或工坊仍可提供非空系统段；`backend/app/ai/templating.py:289` 会分别返回 system 与 user 两段。
- 图片模型调用 `backend/app/services/project_thumbnail_ai.py:206` 与 `backend/app/services/series_thumbnail_ai.py:268` 接收**单个字符串**。推荐默认 system 置空，渲染时将 registry 的非空 system/user 按顺序用换行合并成单字符串，避免用户通过导入等方式保存的 system 被静默丢弃。常规模板预览仍显示独立段，应在模板说明或 UI 明确它们最终合并。
- 系统默认模板应保存用户刚编辑的固定起始和结尾原文；Python 构造 `subject`（角色／世界观／组织／职业）与 `fields`（仅允许的字段）作为变量，字段为空、很长和常规四类输入的无覆盖渲染应与当前行为一致。Jinja 参数值不会再次当模板执行。
- `backend/app/ai/templating.py:220`：`required=True` 只要求变量在任意段的 AST 中出现；`locked=True` 要求恰好一次且位于顶层。设定字段是否必需/锁定属于产品选择：原批准行为是默认根据当前设定生成且弹窗可自由编辑；强制锁定会限制用户个性化，不应误当成权限防护。变量无论声明方式都只能提供已完成 AI 可见性过滤后的值。
- `backend/app/services/prompt_service.py:317`：模板预览用示例变量；提供有代表性的 subject/fields 示例。通用模板校验支持自身组件、未知变量检查及版本；避免绕过 `get_override` 直接取数据库原文。

### 需要防止的长度与一致性问题

- 通用模板单段可达 20000 字、渲染上限 200000 字（`backend/app/ai/templating.py:27`、`:270`），但生图请求 `prompt` 限制非空且最多 2000 字（`backend/app/schemas/setting_image.py:26`）。自定义固定文字、组件展开、重复变量或 system/user 合并都可能超出 2000；应在获取默认提示词时对最终合并字符串统一校验，报可理解的错误以指导缩短模板／设定，而不是返回一个无法提交的默认值或静默裁掉用户自定义内容。
- 当前字段预算以固定 intro/ending 计算，模板化后固定指令可能更长；应保留默认字段截取的兼容行为，并对实际渲染结果再检查。若改为先按模板剩余空间预算，应考虑模板可重复引用字段、条件分支和组件展开，仍必须检查最终长度。Python 字符数与前端 textarea 的 UTF-16 限制并不总是相同，建议覆盖 emoji 场景。
- `prompt_token` 目前绑定原发起人、授权、设定可见性与字段来源；模板在生成弹窗展示后属于用户可编辑的本次提示词快照，变更模板不应绕过既有实体来源复验；若设计要让模板变更本身令旧提示词失效，须明确增加并验证版本合同，不能假装现有 token 已绑定模板。

### 验证与受影响文件

- 必改／可能改动：`backend/app/ai/prompts/registry.py`，两份新 Jinja 文件，`backend/app/ai/prompts/setting_thumbnail.py`，`backend/app/services/project_thumbnail_ai.py` 和 `series_thumbnail_ai.py`；具体运行时渲染 helper 可与现有构建函数合并。模板说明、README、后端提示词／缩略图规范同步更新。
- `backend/tests/test_prompt_templates.py:48` 将 key 数量硬编码为 31，添加一个 key 后需维护为 32 或采用有意义的 key 断言；`test_prompt_components_expand.py:63` 会扫描所有内置模板不得含组件引用。
- 针对性测试应覆盖：未覆盖四类与系列默认文字（含用户刚改的结尾）一致；本人保存 / 停用 / 恢复 / 版本与组件展开；作品、系列协作者只读取本人覆盖且即使作者提供图片模型也不借用作者模板；隐藏字段／never 与撤权、来源变化防护仍生效；两段合并、空提示词、动态字段和 emoji 超 2000 的清晰错误；模板库真实页面能找到并保存新条目。

## Related Specs

- `.trellis/spec/backend/prompts-styles.md`：默认提示词应在 Jinja 文件；registry 声明、本人覆盖、组件、校验、版本和运行时渲染。
- `.trellis/spec/backend/setting-thumbnails.md`、`setting-ai-visibility.md`：设定投影、授权和缩略图生图保护。
- `.trellis/spec/frontend/component-guidelines.md`：通用模板编辑 UI。
- `.trellis/spec/guides/cross-layer-thinking-guide.md`：跨前后端、数据库与权限边界。

## External References

- 无；本次为现有仓库内部约定和代码研究。

## Caveats / Not Found

- 未发现既有可直接管理的“设定图片”模板 key；目前默认文案由 Python 函数拼接。
- 未执行测试，也未修改产品文件。是否在生图弹窗增加模板设置深链以及字段变量必需性，需主任务协调者决定；两者均不是复用模板库本身的前置条件。
