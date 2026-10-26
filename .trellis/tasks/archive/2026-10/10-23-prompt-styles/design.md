# 技术设计：提示词模板与写作风格

## 1. 模板引擎（`app/ai/templating.py`）

- 依赖：新增 `jinja2`。
- 环境：`SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)`，`trim_blocks` 和 `lstrip_blocks` 都开启。是否开启以默认模板能否逐字复现为准，实现时用快照测试来定。
- 移除全局的 `range`、`lipsum`、`cycler`、`joiner`、`namespace`，只保留少量安全过滤器：`join`、`length`、`trim`、`default`、`enumerate` 风格的 `loop.index`。
- 校验（`validate_template(key, system, user)`）在保存、导入和预览时执行：
  1. 语法：调用 `env.parse`。出错时返回 `{part: system|user, line, message}`。
  2. 变量：用 `jinja2.meta.find_undeclared_variables` 找出模板引用的变量，不在该 key 声明的变量表里就报"未知变量 x"。
  3. 必需变量：声明为 required 的变量必须出现在模板中。
  4. 锁定变量（如 `output_schema`、`json_rule`）：必须出现且只能出现一次，并且不能放在 `if` 或 `for` 块内（遍历 AST 检查）。这样锁定的内容一定会输出，位置由模板决定。逐字复现现有提示词需要这一点，因为现在的 JSON 规则并不总在末尾（例如大纲续写里它是第 4 条要求）。
  5. `for` 只能遍历声明为 list 的变量，不能遍历字面量或函数调用。这样防止 `range` 一类的拒绝服务写法。
  6. 每段长度上限 20000 字。
- 渲染：`render(key, override|None, variables) -> list[ChatMessage]`。
  - 渲染后的每段超过 200000 字时报错，作为兜底。
  - 运行时还有一道防线：如果锁定内容没有完整出现在结果里，就把它追加到 user 段末尾，并记一条 warning 日志。

### 1.1 模板注册表（`app/ai/prompts/registry.py`）

```python
@dataclass(frozen=True)
class TemplateVar:
    name: str; label: str; kind: Literal["text", "int", "bool", "list"]
    required: bool = False; locked: bool = False; sample: Any = ""

@dataclass(frozen=True)
class PromptTemplate:
    key: str; name: str; category: str; description: str
    system: str; user: str          # 系统默认（Jinja 源码）
    variables: tuple[TemplateVar, ...]
```

模板 key 共 19 个，按分类列出：

- 章节：
  - `chapter.generate`
  - `chapter.continue`（字数不足时的续写指令，只有 user 段；system 段为空，渲染时不产出 system 消息）
  - `chapter.polish`
  - `chapter.revise`
  - `chapter.rewrite`（局部改写，`op` 作为变量，各操作的规则在模板里用 if 分支区分）
- 分析：`analysis.chapter`
- 向导：`wizard.world`、`wizard.characters`、`wizard.outline`、`wizard.careers`
- 实体：`entity.character`、`entity.organization`、`entity.career`、`relations.parse`
- 大纲：`outline.continue`、`outline.expand`
- 风格：
  - `style.block`：风格字段渲染成风格段，结果作为变量 `style` 注入章节类模板；
  - `style.extract`：从样文提取风格，新增，JSON 输出；
  - `style.trial`：试写，新增。

默认模板放在 `app/ai/prompts/templates/<key>.system.j2` 和 `<key>.user.j2` 两个文件里，由注册表读入。

- 原来的 `build_*_messages` 函数签名保持不变，内部改为"把数据拼成变量 → 调用 `render(key, override, vars)`"。新增一个关键字参数 `override`，调用方传入当前用户的覆盖（取不到就传 None）。
- 数据块，例如参考资料、角色卡和 `_block()` 拼好的各段，仍在 Python 里拼成字符串后传入模板。模板只负责排布和措辞，不用为每个"有内容才输出标题"的块写 if。

### 1.2 逐字一致的保障

- 在改造之前，先把现有的 `build_*_messages` 复制到 `tests/prompt_legacy.py`，作为对照实现，只给测试用。
- 新增参数化测试 `test_prompt_parity.py`：每个 key 准备多组输入，覆盖每个条件分支，比如有没有细纲、追加或全新、有没有伏笔、局部改写的 5 种操作。新旧两种实现产出的 messages 必须 `==`。
- 这份对照实现只在本任务中用来守住一致性。之后提示词有意修改时，同步更新对照实现或删掉对应用例，在 spec 里写明这一点。

## 2. 用户覆盖

新建两张表，迁移为 `0013_prompt_styles`。

- `prompt_overrides`
  - 字段：`id`、`user_id`（CASCADE）、`key`（String 64）、`system`、`user`（Text）、`enabled`（bool，默认 true）、`base_fingerprint`（String 16）、`base_system`、`base_user`（Text，保存时系统默认的全文，用于三方 diff）、时间戳。
  - 唯一约束：`(user_id, key)`。
- `prompt_override_versions`
  - 字段：`id`、`override_id`（CASCADE）、`system`、`user`、`note`（String 200）、`created_at`。
  - 每个覆盖保留最近 20 个版本。

规则：

- 指纹：`sha256(system + "\x00" + user)` 的前 16 位十六进制。`outdated = override.base_fingerprint != fingerprint(当前系统默认)`。
- 保存（PUT `/prompt-templates/{key}`）：
  1. 校验；
  2. upsert，写入当前系统默认的指纹和全文作为 base；
  3. 追加一个历史版本并裁剪到 20 个。

  内容与当前覆盖完全相同时，不新增版本。
- "确认已阅新默认"：内容不变，只把 base 更新为当前系统默认，`outdated` 就消失了。
- 停用、启用（PATCH enabled）：停用时运行时使用系统默认，覆盖内容保留。
- 恢复默认（DELETE）：删除覆盖及其历史。
- 回滚（POST `.../versions/{vid}/restore`）：把该版本内容作为一次新保存，同样要经过校验。系统默认升级后，旧版本可能已经不合法，这时返回 400。
- 运行时解析：`prompt_service.get_override(db, user_id, key)` 只返回启用的覆盖。生成类服务在构建 job 时取出覆盖，写入 job。任务运行中途修改模板，不影响已经组装好的 job。
- 生成记录：`params` 中增加 `prompt = {key, source: "default"|"override", override_version_id}`。

API（`/api/v1/prompt-templates`，都按 user_id 隔离）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `` | 列出全部 key：元信息、是否覆盖、enabled、outdated |
| GET | `/{key}` | 系统默认、覆盖内容、变量表（含示例值）、锁定变量的只读内容，以及 outdated 时的 base |
| PUT | `/{key}` | 保存覆盖（校验失败返回 400，带出错位置） |
| PATCH | `/{key}` | 修改 enabled，或执行 `ack_default` |
| DELETE | `/{key}` | 恢复默认 |
| GET | `/{key}/versions` | 历史版本列表 |
| POST | `/{key}/versions/{vid}/restore` | 回滚到该版本 |
| POST | `/{key}/preview` | 用请求体中的 system/user 和变量渲染（变量缺省时用示例值），返回 messages 和校验错误，不落库 |
| GET | `/export` | 导出 `{"format": "ainovel.prompts", "version": 1, "items": [{key, system, user, enabled, base_fingerprint}]}` |
| POST | `/import` | 导入：上限 1MB；未知 key 跳过并返回提示；每项都要通过校验，逐项返回结果 |

导入时，如果某一项与当前系统默认完全相同，就不创建覆盖。

## 3. 写作风格

### 3.1 数据（同在迁移 `0013`）

- `writing_styles`
  - 字段：`id`、`user_id`（为空表示预设，CASCADE）、`preset_key`（String 32，预设才有，唯一）、`name`（≤ 100）、`description`（≤ 200）、`pov`、`rhythm`、`dialogue`、`description_density`、`tone`、`diction`（每个 ≤ 500）、`taboos`（≤ 1000）、`sample`（≤ 2000）、`extra`（≤ 5000）、`order_index`、时间戳。
  - 叙事视角、句式节奏、对白比例和描写密度存自由文本。前端给出常用选项的下拉，也允许自己填写。
- `projects.default_style_id`：FK 指向 writing_styles，删除时 SET NULL。
- `chapters.style_id`：FK 指向 writing_styles，删除时 SET NULL。

预设：

- 6 个预设由代码常量 `PRESET_STYLES` 定义，内容是我们自己写的，不照搬 MuMu。
- 迁移时插入；应用启动时按 `preset_key` 做一次 upsert 同步，代码里改了预设，库里会跟着更新。
- 预设对所有用户可见，只读，可以复制。

迁移 `projects.style`：

- 对 `style` 非空的项目，为项目所有者创建一个自定义风格：
  - 名称为 `《{title}》原风格`，长度截断到 100；
  - 一句话描述为原文前 100 字；
  - `extra` 为原文全文，超过 5000 字时截断（原字段上限就是 5000）。
- 把这个风格设为项目默认。
- `projects.style` 列保留但不再读写，ProjectOut 去掉该字段，下一个大版本再删。downgrade 时，把项目默认风格的 `extra` 写回 `style`。

### 3.2 解析与注入

- `style_service.resolve(db, user_id, project, chapter, choice)`：
  - `choice` 取值为 `"inherit"`（默认）、`"none"` 或风格 id；
  - 返回 `ResolvedStyle | None`，即 `{id, name, description, block}`。
  - 优先级：临时指定的 id > `chapter.style_id` > `project.default_style_id` > None。
  - 临时指定的风格必须可见（预设或本人所有），否则返回 400。
- `block` 由 `style.block` 模板渲染，变量是风格的各个字段；用户可以覆盖这个模板。
- 章节类提示词的 `style` 变量改为渲染后的 block，原来是 `project.style` 的原文。默认的 `style.block` 模板在只有 `extra` 一个字段时，输出 extra 原文。所以迁移过来的旧风格，生成出来的提示词与改造前一致。
- 向导和实体类提示词通过 `ProjectInfo.style` 取项目默认风格的 `description`，即一句话描述。
- 接入点：
  - 生成、润色、按建议重写、局部改写的请求体新增 `style`，取值是 inherit、none 或 id；
  - 批量请求新增 `style`，按章节逐个解析；它保存在 `input.request` 中，续跑和重试时沿用。
- 生成记录 `params.style = {id, name, block}`，没有使用风格时为 null。

### 3.3 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/styles` | 列出预设和我的风格，各带引用数（作为项目默认的次数、被章节覆盖的次数） |
| POST | `/styles` | 新建风格 |
| PATCH | `/styles/{id}` | 修改；预设返回 403 |
| DELETE | `/styles/{id}` | 删除；预设返回 403。有引用且没有带 `?force=true` 时返回 409，附带 `{projects, chapters}` 引用数；带上后再删，引用处由外键 SET NULL 处理，即回落到上一层 |
| POST | `/styles/{id}/copy` | 复制一份 |
| GET | `/styles/export` | 导出（可选 ids），格式 `ainovel.styles` v1 |
| POST | `/styles/import` | 导入：上限 1MB，同名时自动加后缀 |
| POST | `/styles/extract` | SSE：样文（200 到 10000 字）加可选的 ai_config_id。流式输出模型原文，done 事件带上解析出的风格草稿，不落库 |
| POST | `/styles/trial` | SSE：传 style_id 或未保存的风格字段，加场景描述（≤ 500 字）和字数（200 到 1000），流式输出试写正文，不落库 |

另外：

- 项目：PATCH 接口接受 `default_style_id`，ProjectOut 返回 `default_style`（摘要信息）。
- 章节：PATCH 接口接受 `style_id`，ChapterOut 返回它。
- extract 和 trial 照局部改写预览的模式实现：同步 SSE，不抢 generating 锁，不写生成记录（这两项没有项目归属）。

## 4. 前端

- 新页面 `/prompt-templates`，入口在用户菜单，与"模型配置"同级。
  - 列表：按分类分组，标出"已覆盖"、"已停用"、"默认已更新"。
  - 编辑页：
    - 左侧是 system 和 user 两个等宽字体的编辑框，下面是变量表（变量名、说明、是否必需、是否锁定，点一下插入 `{{ name }}`）；
    - 右侧是预览，防抖调用 preview 接口，锁定段灰底只读，校验错误定位到具体行；
    - 顶部操作：保存、恢复默认、停用或启用、历史版本（列表、对比、回滚）、与系统默认对比；
    - outdated 时提供三方对比：旧默认、新默认、我的版本；
    - 差异显示复用 chapter-versions 已有的 diff 工具。
  - 导入导出：导入后弹窗列出每一项的结果。
- 新页面 `/styles`，入口同在用户菜单。
  - 预设和我的风格分开展示，以卡片形式列出。
  - 编辑表单：维度字段的下拉允许自由填写；示例段落有字数提示。
  - "从样文提取"弹窗：粘贴样文后流式显示，提取结果可编辑，保存后成为新风格。
  - "试写"抽屉：填写场景描述，流式输出。
  - 删除被引用的风格时，确认框显示引用数。
  - 支持导入导出。
- 项目概览：原来的"写作风格"文本框换成默认风格选择器，并有"去风格库管理"的链接。
- 章节编辑器：在章节设置里选本章风格（继承或指定）。生成、润色、重写的弹窗中可以临时选择：继承（显示实际会用哪个风格）、不用风格，或指定一个风格。局部改写面板同样提供这个选项。
- 批量弹窗：新增风格选项，可选"按章节或项目默认"、不用风格，或指定一个风格。
- 新增 `types.ts` 和 `api/prompts.ts`、`api/styles.ts`。纯函数放在 `utils/styles.ts`（风格解析的展示文案）和 `utils/prompts.ts`（校验错误格式化、分类分组）。

## 5. 取舍

- **Jinja2 沙箱加 AST 白名单，而不是 `str.format`**：模板里的 `{}` 字面量不会出问题，并且支持 if/for；用 AST 检查限制 `for` 的数据源，杜绝资源耗尽写法。代价是多一个依赖。
- **锁定内容作为必需且唯一的占位变量，而不是追加到末尾**：只有这样，默认模板才能逐字复现。运行时缺失时会自动补到末尾作为兜底。
- **数据块在代码里拼好**：模板保持简单，用户改结构时不会碰到复杂的数据处理。代价是有些块内部的格式改不了，比如角色卡每一行的格式。
- **保存覆盖时同时存下默认的全文**：三方对比需要旧默认，只存指纹就无从显示。
- **预设入库并在启动时同步**：项目和章节可以统一用外键引用预设；代码更新预设后，库里会同步更新。

## 6. 兼容与回滚

- 迁移 `0013` 只新增表和可空列，并把旧风格迁移为新风格；downgrade 把风格写回 `projects.style`，然后删除新表和新列。
- 现有 API 里请求和响应带 `style` 的地方：ProjectCreate 和 ProjectUpdate 中的 `style` 字段，在一个版本内继续接受。传入时为项目创建或更新它的"原风格"，并设为项目默认，同时在响应里给出提示。前端同步改为不再发送这个字段。
- 提示词由逐字一致测试守护，所以不改动模板的用户，生成效果与之前完全一样。
