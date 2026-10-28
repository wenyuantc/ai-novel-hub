# 项目备份与导入（`ainovel.project`）/ EPUB 导出

> 来源任务：`10-23-project-io-reader`（不涉及迁移）。表结构与外键见 [database-guidelines.md](./database-guidelines.md)，
> 知识库分块 / 检索单元写入点见 [retrieval.md](./retrieval.md)，风格表见 [prompts-styles.md](./prompts-styles.md)，
> 快照结构见 [outline.md](./outline.md)。

---

## 场景：表规则注册表——新增与项目相关的表 / 列必须登记

### 1. 范围 / 触发条件

任何**新增表**（直接或经外键间接引用 `projects`）、**给已登记表新增外键列或 JSON 列**、或改变 JSON 列内容结构（尤其是开始在 JSON 里存 id）的改动。

### 2. 签名

```python
# backend/app/services/project_transfer/registry.py
@dataclass(frozen=True)
class TableRule:
    table: str; model; scope: Callable[[int], ColumnElement[bool]]   # 选出属于项目 pid 的行
    option: "versions" | "snapshots" | "generation_logs" | "inspiration" | None = None   # None = 核心数据
    fill: dict[str, "project" | "user"]       # 不导出，导入时填新项目 id / 导入者 id
    fk: dict[str, str]                        # 列 → 被引用表（导入时按「旧 id → 新 id」改写）
    deferred: frozenset[str]                  # fk 中先插 null、全部插完再回填的列（自引用 / 互相引用）
    custom: tuple[str, ...]                   # 由 exporter / importer 专门处理（风格 ref、全局关系类型）
    exclude: tuple[str, ...]                  # 不导出，导入时取默认 / 重算（注释写理由）
    json_refs: dict[str, JsonRemap]           # 含 id 的 JSON 列 → 重映射函数（remap.py）
    json_plain: tuple[str, ...]               # 不含 id 的 JSON 列
    str_refs: dict[str, Callable]             # 含 id 的字符串列（analysis_changes.field_key）
PROJECT_RULE: TableRule; RULES: tuple[TableRule, ...]   # RULES 的顺序就是导入顺序
NOT_EXPORTED = {"agent_conversations": 理由, "agent_messages": 理由, "agent_changes": 理由,
                "tasks": 理由, "kb_chunks": 理由, "retrieval_chunks": 理由}   # 智能体三张表是运行时数据，不跨实例迁移
```

### 3. 契约

- **注册表还被智能体撤销复用**（`app/services/agent/effects.py`，见 [agent.md](./agent.md)「效果快照与撤销」）：`scope` 用于差异快照，`RULES` 的顺序是撤销时重新插入的顺序、
  逆序是删除顺序，`deferred` 列先插 null 再回填。调整 RULES 顺序或 deferred 时要同时跑 `tests/test_agent_effects.py`。
- 列的处理方式互斥并覆盖整张表：`id`（导出原值，导入由数据库分配新 id）/ fill / fk / custom / exclude / 其余原样。
- 当前登记：`world_settings`、`characters`、`organizations`（`parent_id` deferred）、`organization_members`、`relationship_types`
  （本项目自定义 + 本项目关系用到的全局预置，后者带 `global: true`）、`relationships`（`pair_low/high` 由 a/b 重算）、`relationship_history`、
  `careers`、`career_stages`、`character_careers`、`career_history`、`outlines`、`chapters`（`plan_id` deferred，`style_id` → `style_ref`）、
  `chapter_plans`、`chapter_characters`、`foreshadows`、`foreshadow_characters`、`foreshadow_history`、`generation_logs`（可选；`ai_config_id` /
  `task_id` 不导出）、`chapter_versions`（可选）、`chapter_analyses`（`task_id` 不导出）、`analysis_changes`、`story_memories`、`kb_settings`、
  `kb_documents`（`status` / `chunk_count` 重算）、`outline_snapshots`（可选）、`inspiration_sessions`（可选；`project_id` / `converted_project_id`
  指向本项目的改写为新项目，指向其他项目的置空）。
- **JSON 内的 id**（`remap.py`，对应不上的置 null / 去掉，计数后汇总为一条警告）：
  | 列 | 处理 |
  |---|---|
  | `chapter_plans.character_ids` | 角色 id 列表，去掉对应不上的 |
  | `analysis_changes.target / before / after` | 递归改写 `ANALYSIS_ID_KEYS`（`character_id` / `a_id` / `b_id` → 角色、`relationship_id`、`type_id`、`career_id`、`stage_id`、`organization_id`、`foreshadow_id`）与 `character_ids` 列表；以 `writeback_service` 实际写入的键为准 |
  | `analysis_changes.field_key` | `char:{cid}:x`、`rel:{rid}:x`、`relpair:{low}:{high}`（改写后重新取 min/max）、`career:{cid}:{career_id}`、`org:{org_id}:{cid}`、`fs:{fid}:x`；对应不上 → 空串 |
  | `chapter_analyses.foreshadow_candidates` | 每项 `ref_id`（伏笔 id） |
  | `projects.reading_progress` | `chapter_id` → 章节；对应不上 / 结构不对 → 整个进度清空为 `{}`（计入警告「阅读进度…」），`ratio` 限制在 [0, 1]。项目先于章节插入，所以 importer 的 `_project` 先跳过 `PROJECT_RULE.json_refs` 的列，全部表插入并回填后由 `_project_refs` 改写（显式写回 `updated_at`）；预览的 `_dry_run_missing` 也会检查项目行 |
  | `outline_snapshots.data` | outlines / plans 的 `id`、plans 的 `outline_id` / `character_ids`、links 的三个 id；快照里已不存在的大纲 / 细纲分配**大于导入时该表最大 id** 的新 id（同一旧 id 得到同一新 id），指向已不存在章节的 link 去掉 |
- 历史表的 `changes`（关系 / 职业 / 伏笔）、`changes_raw`、记忆的 `characters` 等都按**名称**记录，不含 id（`json_plain`）。
  以后若在这些 JSON 里存 id，必须把它移到 `json_refs` 并写重映射函数。

### 4. 必需的测试

`tests/test_project_transfer.py::test_registry_covers_all_project_tables`（沿外键扩散找出所有与项目相关的表，必须等于 RULES ∪ NOT_EXPORTED ∪ {projects}）、
`test_registry_handles_every_fk_and_json_column`（每个外键列在 fk / fill / custom / exclude 之一；每个 JSON 列恰好在 json_refs 或 json_plain 之一；
fk 声明的被引用表与模型一致）、`test_registry_import_order`（非 deferred 的外键只能引用排在前面的表）。

### 5. 错误写法

```python
class NewThing(TimestampMixin, Base):        # 新表引用 projects / characters，却没登记
    character_id = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"))
# → 备份里丢数据；反射测试失败
```

---

## 场景：备份格式 v1 与导出

- `GET /projects/{pid}/backup?versions=1&snapshots=0&logs=0&inspiration=0` → `application/json` 附件，
  `书名-备份-YYYYMMDD.ainovel.json`（RFC 5987）。他人项目 404。
- 结构：`{format: "ainovel.project", version: 1, exported_at, app_schema: "0018"（仅供参考，新增迁移时同步 exporter.APP_SCHEMA）,
  options, styles: [{ref, preset_key, name, 全部风格字段}], project: {…, default_style_ref}, tables: {表名: [行]}}`。
  `kb_settings` 也是数组（0 或 1 行）；行里保留原 `id` 与外键原值，`project_id` / `user_id` 不导出，`created_at` / `updated_at` 原样导出。
- 被引用表因可选项未勾选而不导出时，外键导出为 null（如不导出生成记录时 `chapter_versions.generation_log_id`）。
- 流式：路由校验归属后 `db.close()`，生成器用自己的 session，按注册表逐表查询，有 id 的表按 id 键集分页（每批 500 行）。
- 估算 `GET /projects/{pid}/backup/estimate` → `{core, versions, snapshots, generation_logs, inspiration}`，每项 `{rows, chars, bytes}`
  （chars = 文本 / JSON 列长度之和；bytes = chars × 3 + rows × 150，粗估）。

## 场景：导入

### 契约

- `POST /projects/import/preview`（multipart `file`）→ `ImportPreview {token, title, final_title, version, exported_at, app_schema, options,
  counts, styles: [{name, preset_key, action: preset|reuse|create|copy}], warnings, expires_in}`；不写库。
- `POST /projects/import`：JSON `{token, title?}`，或 multipart（`file` + 可选 `title`）直接导入 → 201 `ImportResult {project: ProjectOut, counts,
  warnings, task_id}`。成功后令牌失效；失败时令牌仍可用。
- 上传：`UploadLimitMiddleware` 规则 `POST /api/v1/projects/import(/preview)?`，上限 100MB + multipart 开销（detail `备份文件过大，上限为 100MB`），
  路由再 `read(MAX_BYTES + 1)` 兜底。解析在线程中进行。
- 令牌：进程内缓存（单进程前提，同 tasks），按用户隔离，`TOKEN_TTL = 600` 秒，每个用户最多 2 份（新的挤掉旧的），全实例最多 `MAX_TOKENS_TOTAL = 6` 份（超出时淘汰最早到期的，被淘汰的确认时按过期处理）；
  过期 / 他人 / 不存在 → 400 `导入凭据已过期，请重新选择备份文件`。
- 校验（一律 400；文件内问题响应体带 `errors`，前 20 条）：

| 条件 | detail |
|---|---|
| 非 UTF-8 / 非 JSON / 非对象 | `备份文件不是 UTF-8 编码的 JSON` / `备份文件不是合法的 JSON：…` / `备份文件格式不正确：应为 JSON 对象` |
| format 不符 | `不是有效的项目备份文件（format 应为 ainovel.project）` |
| version 非整数或 < 1 / 大于当前 | `不支持的备份版本：N` / `备份来自更新的版本（vN，当前支持 v1），请升级后再导入` |
| 嵌套 > 40 层 | `备份文件嵌套层级过深（上限 40 层）` |
| 缺少 project / tables | `备份文件格式不正确：缺少 project` / `…缺少 tables` |
| 行字段缺失（NOT NULL 且无默认）/ 类型不符 / 超长 / id 重复 | `备份文件校验失败：…`，errors 如 `角色第 1 行缺少字段 name`、`角色第 1 行字段 name 超过长度上限 100` |
| 外键在文件内找不到 / 风格 ref 不在 styles | errors 如 `章节第 1 行的 outline_id 引用了不存在的大纲 #99999` |
| NaN / Infinity（`json.loads` 默认接受） | `备份文件不是合法的 JSON：不支持的数值 NaN` |
| 整数超出列范围（Integer ±2³¹、BigInteger ±2⁶³）/ 浮点非有限 | errors 如 `世界观第 1 行字段 sort 超出整数范围` |
| JSON 列结构与模型注解不符（`Mapped[list[str]]` → 字符串数组、`Mapped[dict]` → 对象；`Mapped[Any]` 不查） | errors 如 `项目第 1 行字段 tags 应为数组`、`角色第 1 行字段 aliases 应为字符串数组` |
| 枚举列取值不在 `importer.ENUM_COLUMNS`（对应 Out schema 的 `Literal`）中 / `wizard_progress` 不符合 `WizardProgress` | errors 如 `章节第 1 行字段 status 取值 weird 不合法`、`项目字段 wizard_progress 结构不正确` |

> 这几类值能写进库，但会让导入响应或之后的列表接口 500（Out schema 校验失败 / PG 溢出 / JSON 编码 NaN 失败），所以在预览阶段就拦下。
> 新增 `Literal` 取值的字符串列时同步登记 `ENUM_COLUMNS`。

- 导入（单事务）：风格 → 项目 → 按 RULES 顺序插入 → deferred 回填（显式写回 `updated_at` 原值）→ 知识库 `kb_index_service.chunk_document`
  （从 text 重新分块 + 关键词检索单元，文档状态 `ready_keyword_only`，更新时间改回原值）→ `memory_service.index_memories`（保留钉住 / 屏蔽 / content_key）
  → commit → 导入者有默认向量模型时 `kb_service.start_rebuild`（`task_id`，失败只记警告）。
- 风格：预设按 `preset_key`（缺失 → 复制为自定义风格 + 警告）；自定义风格在导入者库里有**名称与全部字段都相同**的 → 复用，否则新建
  （同名不同内容 → `名称（导入）`、`（导入2）`…）。
- 全局关系类型按 `(role_a, role_b)` 对应；找不到 → 新建为项目自定义类型 + 警告 `预置关系类型「A/B」在当前版本中不存在，已改为项目自定义类型`。
- 书名：请求 `title`（非空时）或备份书名；与导入者已有项目重名 → `（导入）`、`（导入2）`…（≤200 字）。
- 章节 `status = generating` 导入为 `draft`（没有对应任务）。
- 失败：插入阶段数据库报错 → 回滚，400 `导入失败：组织（organizations）第 a–b 行附近出错：…`，响应体带 `table` / `row_start` / `row_end`
  （每批 500 行 flush，行号精确到批）。
- 格式升级：`importer.migrate(data, version)` 逐级升级；新增表时登记注册表、提升 `exporter.VERSION`、补上旧版本到新版本的转换步骤。

### 必需的测试

`tests/test_project_transfer.py`：往返（全部实体、同名角色 / 章节、导出 → 导入 → 再导出按行序号逐表比对，含时间戳；每种分析回写类型的
target / before / after / field_key；快照自洽且可恢复）、可选项、风格复用 / 加后缀 / 预设缺失复制 / 跨用户复制、全局关系类型降级、校验矩阵（含 `test_validation_rejects_values_that_break_reads`）、
中间件规则与分块传输拦截、插入失败整体回滚（项目 / 风格 / 角色计数不变）、令牌过期 / 他人 / 用后失效、multipart 直接导入与书名后缀、跨用户、
估算、导入后关键词检索（知识库 + 记忆，屏蔽不召回）、有向量模型时排队重建并变为 hybrid。

---

## 场景：EPUB 导出（`epub_service`）

- `GET /projects/{pid}/export?format=epub&author=&include_synopsis=true&chapter_ids=…`：`author` ≤100 字（超长 422），缺省取用户名；
  章节选择与 TXT 一致（生成中的不导出）；正文为空的章节跳过，响应头 `X-Export-Warnings` = UTF-8 百分号编码的提示（多条以「；」分隔）；
  没有可导出章节 → 400。
- 结构：`mimetype`（第一个条目、`ZIP_STORED`）、`META-INF/container.xml`、`OEBPS/content.opf`（EPUB 3，`dc:identifier = urn:uuid:` +
  uuid5(项目 id + 导出时间)、title / creator / `zh-CN` / `dcterms:modified` / 每个类型标签一个 `dc:subject`（没有标签时用题材）/
  `dc:description`（简介，没有时用一句话设定））、`nav.xhtml`（`properties="nav"`）、`toc.ncx`（`playOrder` 连续、`dtb:depth` 1 或 2）、
  `title.xhtml`（书名 / 作者 / 类型标签）、`synopsis.xhtml`（可选，有简介时）、`vol-N.xhtml`、`ch-N.xhtml`、`style.css`。
  spine：扉页 → [简介页] → 每卷依次为卷标题页与该卷各章，`toc="ncx"`。manifest 与包内文件一一对应。
- 分卷（`group_volumes`）：章节经 `outline_id` 取大纲 `volume_no` / `volume_title`；没有关联大纲的章节归入前一章所在的卷；排在所有有卷章节之前的不归卷
  （作为顶层条目）；**只有一卷且没有卷名时视为不分卷**（不生成卷标题页、目录一层）。卷名已以「第…卷」开头时原样使用，否则 `第N卷 卷名`（1–99 用中文数字）。
- 正文：按换行切段，空行忽略，每段一个 `<p>`；全部文本 XML 转义并去掉 XML 1.0 不允许的字符（C0 控制字符除制表 / 换行 / 回车、代理区、U+FFFE/FFFF）。
  CSS：`p { text-indent: 2em; line-height: 1.6; margin: 0 0 .4em; }`，标题居中，不指定字体。
- 测试：`tests/test_epub.py`（结构、有卷 / 无卷、nav 与 ncx 层级、扉页与简介页开关、作者默认值与上限、控制字符、只导出选中章节、空章提示、跨用户、分卷规则）。
