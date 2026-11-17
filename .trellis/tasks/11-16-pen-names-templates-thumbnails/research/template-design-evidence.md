# Research: 个人小说模板的独立快照与单事务建书

- Query: 如何将配置、可选设定和大纲保存为个人模板，并创建正文为空、引用有效、与来源独立的新书。
- Scope: internal；仅静态阅读当前代码与规范，未实现、未执行功能测试或数据库迁移。
- Date: 2026-11-16
- Task: `.trellis/tasks/11-16-pen-names-templates-thumbnails`
- 用户确认：D13.1 配置＋可选设定／大纲且新书正文为空；D13.2 作者笔名与协作可见边界；D13.3 四类设定仅本地上传缩略图。

## Findings

### 1. 结论与建议边界

建议增加独立、有版本、只归当前用户所有的模板快照，保存时把**当前档案可见的有效值**冻结，使用时生成新项目、新条目、新引用身份和新的 confirmed 基线。不要复制源作品的演变事件、旧基线身份、历史、正文、章节／场景、运行任务或系列绑定。

当前值冻结可沿用已有设定卡语义：卡片导出当前实体值与 `resolve_entry_fields(...).value`，导入以其初始化新主体基线。证据为 `backend/app/services/card_service.py:196`、`:260`、`:331`，以及 `backend/tests/test_cards.py:510`、`:670`。模板仍须自己定义四类条目和配置的完整载荷，因为现有 cards v6 不足以表达本次范围。

「当前」有明确的现有含义：`setting_progression_resolver.py:317` 定义 `position=None` 为全部有效事件之后的最新投影，**包含 planned 变化**，忽略 inactive／孤儿事件。模板预览应直说保存当前档案值，而非假称故事最初状态；新书把该值作为起点，不重放任何旧章节。这个行为应出现在最终规划摘要，供用户评审。

以下为技术建议，除上述 D13.1–D13.3 外不冒充用户已逐项确认。

### 2. 现有机制的可复用边界

| 机制与证据 | 可复用部分 | 不能直接套用的原因 |
| --- | --- | --- |
| `card_service.export_cards`，`backend/app/services/card_service.py:108` | 当前状态快照、typed 字段解码、局部 ref、AI links、标签、当前关系／成员／职业的处理思路 | `CardExportRequest` 仅有角色和组织选择（`schemas/card.py:337`）；职业随角色挂接带入；`CardFile:214` 没有 world 容器。只导出实际绑定字段定义（`card_service.py:281`），且没有完整分类规则，不能满足仅配置模板 |
| `_export_typed_fields`，`card_service.py:260` | `definition.N`、`option`、`entry_ref` 的局部身份，包外引用显式映射；同时保留字段隐藏属性 | 私有函数固定三类输入；不能通过把世界观伪装成角色来复用。适合提取无状态 codec／局部 ref 构建器，不把整个卡片协议改成模板 |
| `outline_transfer_service.export_outline`，`:48` | 卷／章纲／细纲层次和字段白名单 | `:71` 将角色 ID 转名字；导入 `:109` 按名字／别名匹配且未匹配会忽略。同名角色会丢身份；模板必须改用局部角色 ref |
| `project_transfer` v9 | 字段／选项／句柄／关系类型重映射及全图校验的模式 | 完整备份含正文、场景、时间线事件等；不能只清 `chapters.content`，会从版本、场景、记忆、演变、向导／阅读状态带回旧书内容 |
| `_Importer.run`，`project_transfer/importer.py:1562` | 内部只 flush 的整图导入结构、导入后验证顺序 | 私有 API 与备份注册表紧密耦合，可能继续恢复时间戳、进度、历史及检索数据；若提取通用原语，应保留原备份行为，模板另有显式白名单，不能传任意 backup 伪装模板 |
| `setting_field_service.resolve_entry_fields`，`:994` | 四类条目批量当前投影、字段与 AI 版本检查，返回 base_value 和 value | 要取 `value`，不能误拿 `base_value`；不应用 AI 上下文过滤结果代替人工快照，否则隐藏字段会丢失 |

### 3. 首版选择粒度与完整载荷

建议界面提供三个可理解的分组：常用配置、所选设定、所选大纲。配置可整体复用，设定按四类条目勾选，大纲按章纲勾选并带其细纲；卷标题跟随选中章纲。大纲不创建章节／场景，创建完成 `chapter_count=0`、`total_words=0`。

| 内容 | 建议快照语义 |
| --- | --- |
| 作品配置 | 显式白名单包含常用立项信息、`auto_analyze` 及默认风格快照。创建时要求新的书名，可覆盖立项字段；不继承源作品状态、阅读进度、向导进度、时间戳和项目版本 |
| 风格 | 保存实际可见的风格内容，不能只有可失效的 `style_id`。复用／重建规则见下文；不包含模型配置、API Key、Skill、MCP 或提示词覆盖库 |
| 字段目录 | 保存配置中全部所选定义及选项，即使当前没有任何绑定；保留类型、适用范围、引用类型、排序、停用与 `ai_hidden`。空配置模板不能因没有角色而丢字段定义 |
| 整理分类 | 保存所选配置的分类及 tag／dropdown 规则；规则指向快照局部 definition/option ref。分类不会携带实体成员快照 |
| 四类设定 | 显式字段白名单，身份以局部 ref 表达；角色／世界观的当前内置值、自定义字段 `value`、标签、别名和 AI 策略；组织／职业当前静态值及字段，职业包含阶段定义 |
| 当前关联 | 人物关系、组织成员、角色职业挂接仅取保存时存在的关联；不包括过去已结束的关系载体或演变历史 |
| 大纲 | 章纲及其细纲的标题、摘要、关键事件、角色 refs、情绪、目标、预计字数与顺序；不复制章节链接和任何正文。重建时按选中顺序连续编排，与现有大纲导入的行为一致（`outline_transfer_service.py:177`） |
| 缩略图 | 复制选中条目实际使用的规范化图片 bytes／MIME 至模板自己的冻结资源。不能保留源图片 URL、源条目 ID 或源共享条目图片作为使用时依赖 |

复制 `StoredAiSettings` 与字段隐藏标记属于保留资料，不发生模型调用。必须保留 `never`、`manual_only`、排除词、隐藏字段等原限制；包内 AI links 重映射，不能变回默认 `detected`。现有规范为 `.trellis/spec/backend/setting-ai-visibility.md`，实现参照 `backend/app/services/card_service.py:1059` 与 `backend/app/services/card_service.py:1065`。

### 4. 必要依赖闭包与可省略关联

不得悄悄加入用户未选资料，也不得为了成功而清空引用。预览先返回已选条目、缺失依赖、关联包含／省略计数、冲突和数据量；作者明确点击补选依赖，再重新预览。可递归求闭包，但需要 visited 集和条目数上限，循环引用本身是合法图。

| 类型 | 处理建议 |
| --- | --- |
| 自定义 reference 字段 | 必要闭包：带入字段值就必须带入目标条目；预览说明「角色 A 的故乡引用地点 B」。不提供同名猜测或静默 null |
| AI links | 必要闭包：保留源策略就保留作者显式链接目标。允许一跳出站不等于快照只处理一层：整个保存图的链接目标都须有局部 ref |
| 组织父级 | 必要闭包：所选子组织的完整父链必须显式补选；不把未选父级静默变成顶层。继续使用既有无环／深度校验 |
| 角色当前职业 | 建议作为角色必需依赖：保留当前挂接所需职业及阶段；预览展示补选职业，角色来源需取消时同步重算闭包。职业的阶段定义一并复制，不携带角色以前的等级变化 |
| 分类规则 | 配置内部闭包：dropdown 规则需要对应定义与选项，不能用显示文字寻找或匹配 |
| 大纲细纲角色引用 | 必要闭包：细纲有 `character_refs` 时必须补选该角色，或取消该章纲；不能沿用旧交换格式的“未匹配即忽略” |
| 人物关系 | 可省略关联：仅在两端角色均进入最终选择时携带。不会因为选 A 就自动把其所有关系角色拉入；预览列出省略的关系数量 |
| 组织成员 | 可省略关联：仅在组织与成员角色均进入最终选择时携带。选组织不意味着自动复制其全部成员；预览给出省略计数 |
| 系列绑定 | 一律不复制绑定；对作者有权读的本书投影冻结当前值，创建独立条目。新书不会自动加入系列或修改共享库。现有单书备份的相同边界见 `project_transfer/registry.py:94` |

最终 snapshot 应自行通过完整引用图验证：ref 唯一且类型合法，选项归属正确，同一作用域、同一职业阶段、不自关联 AI link、组织无环、关系角色对规范化。保存之后使用模板不应再出现外部 unresolved refs。

### 5. 当前值转初始基线的落库顺序

1. 验证模板归属、模板 revision／格式及总尺寸，检查原认证身份；在任何创建副作用之前调用 `quota_service.check_project_creation`。
2. 创建新 `Project`（只 flush）；状态 `planning`，空阅读进度，新时间戳和新版本，用户自己选择新书笔名。笔名不是模板中固定的源作品绑定。
3. 创建字段定义／选项、分类规则和类型目录；先建全部四类实体与职业阶段，再处理任何局部引用。组织父级可以分两阶段回填，但必须全程同事务并在提交前验证。
4. 建立四类条目新基线、legacy 文本字段和 typed 字段基线；`resolve_entry_fields(...).value` 先映射新 option／handle，再写入新 binding。所有条目的 `baseline_status=confirmed`，不写旧 revision 或历史事件。
5. 创建两端已选的关系／成员、角色当前职业；语义均为新书起点的当前关联。创建新的关联 timeline，不复制来源的时间线 ID。
6. 写 AI links、标签、所选大纲／细纲及新图像 bytes。新图像身份由本书分配，不共享模板图像可变行。
7. 执行 `validate_project_field_graph`、`validate_project_category_graph`、`validate_project_links`、必要的关系／职业／成员与规划校验，统一 flush；只有全部通过才 commit。
8. 失败（包括取消）整个新项目、风格副本、图像和配置回滚；不得留下空壳书或半套设定。成功打开概览，比直接进入会重新生成资料的向导更合适；当前 `ProjectListPage.tsx:101` 的统一向导跳转需要对模板建书单独处理。

可直接组合的已存在原语：

- `story_service.new_character`（`:90`）、`new_world`（`:102`）：只 flush，并调用 `ensure_entry_timeline`。
- `setting_catalog_service.ensure_entry_timeline`（`:288`）：只 flush；角色／世界观初始化当前字段，组织／职业定义初始化空内置 baseline。
- `setting_progression_service.create_character_timeline`（`:194`）、`create_world_timeline`（`:173`）：新建默认 confirmed，不修改已存在主体状态。
- `organization_service.new_org`（`:261`）、`new_member`（`:424`）：只 flush，后者无章锚时建立基础成员。
- `career_service.new_career`（`:264`）：只 flush 并建阶段；`assign_new`（`:802`）不 commit，经 `apply_assignment` 建当前挂接。
- `relationship_service.create_relation`（`:652`）：不 commit，但会写本次创建的关系历史；`career_service.apply_assignment:788` 同样会写本次创建记录。**这不是复制源历史**。若最终合同要求历史表严格为零，需单独提取 bootstrap 原语，不能删掉既有公共服务的审计语义来迎合断言。
- `setting_field_service.apply_definition`（`:219`）、`encode_field_value`（`:678`）和 `card_service._apply_typed_fields`（`:1075`）展示无提交配置／绑定写法。后者不能作为跨模块永久私有 API 直接依赖，应提取必要 codec 或在模板服务中使用公开原语。

特别注意：`career_progression_service.get_or_create_career_timeline:60` 在已有 CharacterCareer 行时会标为 `legacy_unverified`。不能先裸插入挂接，再无条件调用它并宣称新书全是 confirmed；应复用正常新建路径或明确构造已验证的初始基线。

### 6. 明确不应直接拼接的提交边界

| 服务 | 内部提交／回滚位置 | 对模板的影响 |
| --- | --- | --- |
| `project_service.create_project` | `backend/app/services/project_service.py:549` | 项目先提交后补资料会产生半成品；如复用，抽出只 flush 的内部创建原语，保留原 HTTP 提交行为 |
| `card_service.import_cards` | `:1047` commit；`:765` 外包装异常整体 rollback | 不能创建模板项目后再串联公开导入入口；不得通过绕开装饰器／猴补 commit 来组合 |
| `outline_transfer_service.import_outline` | `:217` commit | 会提前提交已有图，而且名字映射不符合模板稳定引用 |
| `project_transfer.import_backup` | `importer.py:1601` commit，失败 rollback，后续还可排索引任务 | 不应作为模板内部调用；若抽共享导入层，模板必须显式禁止所有运行态副作用 |
| `setting_field_service.save_definition/save_option/write_baseline` | `:301`、`:642`、`:829` 审计 commit | 使用其无提交 apply／codec 层，而不是 HTTP 层服务 |

配额路径在 `quota_service.py:41` 先锁 User，再计数全部未物理删除作品（`:37`，包含回收站），持锁直到外层 commit；这正是新模板创建入口必须参加的串行点。保存模板和预览本身不占作品额度；实际使用才检查。现有 `test_user_quotas.py:69` 的四入口断言应增加第五入口。

### 7. 保存源快照的一致性与生命周期

- 用户级模板建议仅由作者从自己的活跃作品保存，个人模板列表／详情／使用严格 `user_id`；管理员人工管理权和协作者内容编辑权不自动给予作者私人配置／模板权。此为首版建议，最终摘要应明确。
- 预览不能在多个默认 READ COMMITTED 查询之间拼接前后版本的数据。可借鉴 `setting_category_query_service.category_read_snapshot:97`：独立 session，在任何 SQL 前配置 PG REPEATABLE READ＋READ ONLY，SQLite 显式 BEGIN，同时重验原 actor／活跃状态，finally rollback。
- 该 helper 默认授权不是严格 owner（`:125`），不能原样调用后省略拥有权验证。应提取通用一致快照基础或在独立上下文中补严格 owner。
- 保存确认按用户屏障 → 源项目生命周期锁顺序重验，重算选中内容的规范化指纹（包含字段／分类／AI／时间线／大纲／图片和风格快照），与预览的 `source_stamp` 对比；有变化返回 409 并保留选择，请作者复核新预览。
- 不只比较 `Project.updated_at`：子条目、风格库和系列投影有各自修改边界。外部可变风格应保存完整内容快照并在确认时校验该版本／内容。
- 在源项目写锁下读取已采用的系列本书投影即可冻结单书值；不要持有项目锁后反向拿系列写锁。保存不能把来源系列的共享身份一起拷走。
- 一旦模板保存成功，来源改名、删除、回收甚至彻底删除均不影响模板使用；模板仅保留用于说明的来源标题／时间，不把来源项目 FK 当授权或运行时依赖。
- 模板修改／重新保存只影响以后创建的新书；已经创建的作品应完全独立。模板自身使用 revision CAS 防止覆盖另一个页面的更新，删除不删除已有新书。

### 8. 风格的特殊可变性

现有备份 `_plan_styles`（`project_transfer/importer.py:1202`）可以复用同内容私有风格（`:1221`），并按 `preset_key` 直接复用当前预设（`:1212`）；这适合备份兼容，但不等于严格的历史冻结。

模板应保存风格字段快照。使用时如果同内容个人风格仍存在，可以依既有风格库语义复用；不存在或已经变更则重建私有风格并显示名称。若承诺「连以后编辑风格也互不影响」，就必须始终新建项目专属风格副本，不能复用可变风格行。建议最终合同采用“建书时恢复保存的风格内容；之后风格库仍遵循现有共享编辑语义”，避免未经需求授权引入新的项目专属风格系统。

### 9. 建议数据模型、API 和版本边界

推荐逻辑模型（最终命名由主设计确定）：

- `NovelTemplate(id, user_id FK users CASCADE, name, description, format_version, snapshot JSON, revision, created_at, updated_at)`；SQLite AUTOINCREMENT、防止旧身份复用。
- `NovelTemplateImage(id, template_id FK CASCADE, local_ref, mime_type, data, digest)`；`(template_id, local_ref)` 唯一。或等价的受真实 FK 保护的冻结图片归属机制，必须独立于源图片生命周期。
- 来源标题／保存时间可作展示元数据，避免模板表以 `source_project_id FK CASCADE` 导致源作品删除连带模板。若确需保留 FK，只能可空 SET NULL，并主动处理备份反射集合扩散。
- JSON 使用独立 `ainovel.novel-template` v1 的严格 DTO，拒绝未知字段及未来版本；写明可以出现的配置／条目／关联字段，不能 `model.__table__.columns` 自动复制以后新增列。局部 ref 不使用源数据库 ID 当新库身份。
- 现有 project backup v9 不因新增纯用户级模板库而强行升级；缩略图和笔名的单书兼容由对应子任务决定。模板自身和其图片需进入整库备份的迁移／目录核对。若未引用 projects 的用户级表，无须放进项目 `NOT_EXPORTED`（反射测试的约束见 `test_project_transfer.py:68`）。
- 当前代码目录已有 `0053_agent_summary_source` → `0054_agent_conversation_round`，依次 parent `0052`、`0053`。`project_transfer.exporter.APP_SCHEMA` 仍是 `0052`；实施前核对真实 migration head，再分配本任务迁移，不固定抢占 0053。

建议入口：

| API | 作用与关键条件 |
| --- | --- |
| `GET /novel-templates`、`GET /novel-templates/{id}` | 仅本人列表、详情和内容摘要；列表不加载 binary／整份大快照 |
| `POST /projects/{pid}/novel-template-preview` | 所选配置／条目／大纲 → 依赖、包含／省略统计、源指纹、体积、警告；只读，无作品额度占用 |
| `POST /novel-templates` | source project＋选择＋source stamp＋模板名；服务端重算后存完整冻结快照；不接收浏览器自报的任意 ORM 数据 |
| `PATCH /novel-templates/{id}`、`DELETE /novel-templates/{id}` | revision CAS 的名称／说明维护和删除；是否提供“从作品更新快照”可以作为同一预览／确认流程，不直接暴露原始 JSON 编辑 |
| `POST /novel-templates/{id}/projects` | template revision＋新书名／配置覆盖＋本人 pen_name；从服务端快照单事务建书，返回 ProjectOut |

必须同时有条目数、文本 JSON 字节、图片总数与累计字节上限，不能只限制单图。使用前验证缓存／数据库存储快照也不能跳过图校验。大字段序列化和图片处理遵循既有异步 CPU 工作边界；此方案本身不需要新的模板引擎或外部网络请求。

### 10. 有价值的验证矩阵

下列是后续实施应完成的验收，不是本研究已运行的测试。

1. 仅配置模板：零设定、零大纲仍保存完整定义／选项／分类和风格；使用后正文／章节／场景为空，旧向导／阅读／任务／聊天／历史哨兵均不出现。
2. 当前值冻结：源角色基础“练气”、计划变化“金丹”、已撤销“化神”；模板预览／新书基线为当前“金丹”，新书无源变化事件。source 后改值不影响模板及旧新书。
3. 四类×四字段类型：引用自环／环、同名角色不同 ref、停用选项仍有合法当前值、legacy 文本、AI never／隐藏标记、分类 dropdown 均维持语义。
4. 依赖预览：未选地点引用、父组织、职业、AI link、细纲角色能定位原因；作者明确补选才进入快照。两端未齐的关系／成员有省略计数，不自动拉全图。
5. 新书独立：所有源 ID／句柄／选项／关联／图片 ID 重建；source、template、新书 A／B 任意一方后续变更互不改写实体／图片内容；无系列绑定传播。
6. 一致性：真实 PG 双连接在预览后修改源条目／字段／AI link／图片／风格，确认返回 409；旧会话／禁用／源回收后保存失败，没有模板半成品。
7. 原子性：在写到最后的图片／图验证处注入失败，项目、配置、风格副本、图片、关联全部回滚；取消同样不留空壳。禁止用 mock commit 得到假通过。
8. 配额：0 额度时保存／预览模板可用，使用拒绝且零副作用；最后一个额度并发两次使用只能一个成功，计数仍含回收站。
9. 权限：他人模板 CRUD／使用统一 404；协作者、纯管理员不能借源项目权限读作者模板和私有风格；使用时只能绑定本人笔名。
10. UI：保存选择、补选依赖、冲突后保留草稿、空模板库、创建成功进入概览、375px／键盘和切换账号丢弃旧响应。

已有回归入口：`tests/test_cards.py`（`:510` 当前值、`:702` 失败回滚、`:973` 显式引用、`:1227` 同名身份）、`test_card_ai_visibility.py`（真实出站与保护）、`test_outline_transfer.py`、`test_setting_fields_categories_roundtrip.py`、`test_project_transfer.py`（注册表与 ID 映射）、`test_user_quotas.py`、`test_migrations.py`、`test_series_transfer.py`。

建议实施中的命令（按受影响面最终裁定）：

```sh
cd backend
uv run ruff check .
uv run pytest tests/test_cards.py tests/test_card_ai_visibility.py tests/test_outline_transfer.py tests/test_project_transfer.py tests/test_setting_fields_categories_roundtrip.py tests/test_user_quotas.py tests/test_migrations.py tests/test_series_transfer.py
uv run pytest tests/test_novel_templates.py tests/test_novel_templates_migration.py
```

最后两个为建议新增文件，不是当前已存在。并发测试需新增专用 PostgreSQL fixture 与隔离库守卫，遵循 `tests/pg_support.py`，缺环境时只能报告未运行，不能用 SQLite 通过替代；已有字段 PG 参考为 `test_setting_fields_postgresql.py`，卡片授权竞争参考 `test_collaboration_cards_postgresql.py`。前端运行 `npm run lint`、`npm run build`、`npm test` 并完成真实浏览器路径。

### 11. 相关规范

- `.trellis/spec/backend/setting-progressions.md`：当前投影／基线／事件与无提交原语。
- `.trellis/spec/backend/setting-fields.md`：四实体字段、option／handle 身份、保存合法停用值和图校验。
- `.trellis/spec/backend/setting-categories.md`：分类配置依赖、独立读快照和字段／分类双版本。
- `.trellis/spec/backend/setting-ai-visibility.md`：策略与隐藏字段保留、链接和许可边界。
- `.trellis/spec/backend/series-settings.md`：本书投影与共享绑定、锁顺序及单书独立快照。
- `.trellis/spec/backend/project-transfer.md`：注册表反射、ID 重映射与单事务导入。
- `.trellis/spec/backend/rbac-quotas.md`：User 屏障、作品额度与副作用顺序。
- `.trellis/spec/backend/database-guidelines.md`、`quality-guidelines.md`：迁移、隔离测试、功能说明和真实权限测试。

### 12. 补充验证：建书幂等收据的生命周期与锁序

本节验证主设计提出的“用户隔离 UUID 创建意图＋请求摘要＋结果作品”的可行边界，仅为方案建议，未实施或运行测试。

**最小持久结构。** 建议 `NovelTemplateCreationReceipt(user_id FK users CASCADE, intent_key, request_version, request_digest, result_project_id FK projects SET NULL nullable, created_at)`，数据库唯一约束为 `(user_id, intent_key)`，结果外键建索引。UUID 先规范化；摘要覆盖稳定版本的规范化请求，包括模板 ID／所请求的模板 revision、新书名、全部有效覆盖项和笔名选择。不要用重试时变化后的模板内容重新计算请求身份。模板来源 ID／revision 如需展示，保存不可变标量快照；不以模板的 `ON DELETE CASCADE` 外键保存收据。只在成功建书事务中插入收据，不预提交 pending 收据；因此“收据存在、结果 FK 为 NULL”明确表示曾经成功、结果后来已彻删，不能解释为尚未创建。

**准入及重放顺序。** 统一 owner User 屏障内，重验原登录身份，再按 `(当前 user_id, intent_key)` 读取收据。有收据先比摘要：不同即 409；相同则返回原作品身份，跳过模板存在性／revision 和新增配额检查。原作品已回收、正在删除或结果 FK 已为 NULL 时，返回明确的原结果不可用错误（建议 409 `creation_result_unavailable`），保留收据且不重建。正常结果可按现有 `ProjectOut` 返回当前内容，幂等保证的是相同作品身份，不是永久冻结第一次响应的内容。没有收据才校验本人模板／预期 revision，检查作品额度，写入完整新书及收据，一次提交。任何失败整体回滚；未知提交结果通过新 session、同一 key 查询确认，不能换 key 自动补建。前端只在作者发起新的创建意图时产生新 UUID；超时／断网重试沿用原 key。

现有 `quota_service.check_project_creation` 在 `backend/app/services/quota_service.py:43` 锁 User，`:47` 在锁内计数；`project_count:37` 包括回收站。同一用户不同 key 的并发也必须共享这条屏障直到全部提交，不能先查额度后排队，也不能先提交项目再补收据。同 key 的后到请求须在取得 User 屏障后重新读收据；唯一约束是兜底，不能 `ON CONFLICT DO NOTHING` 后仍提交刚建的重复作品。不同用户可使用相同 UUID，各自的读取和唯一键均包含 user_id；返回结果还须核对作品 owner，不能仅按全局 UUID 或结果 ID 命中。

**认证和业务锁序。** `authorization_service.lock_users:129` 使用升序 no-op UPDATE；本人建书可先取得该屏障，再以 `require_authenticated(..., write=False)` 重验原 actor 的启用／token_version（`:220`），随后操作模板／结果项目。若采用 `write=True` 的完整授权 helper，必须在任何 User／业务资源锁之前调用，因为它在 `:227` 先取授权 gate；禁止持 User 锁后再反向取 gate。配额 helper 可在已持有同一 User 锁时调用。此处没有新增全站配额或后台任务，无须自行引入另一套进程锁。

尤其不能在重放时先 `SELECT ... FOR UPDATE` 锁收据，再锁结果 Project：人工 purge 先授权并锁 User（`recycle_bin_service.py:321`），系统到期 purge 则跳过该步骤，但两者都先锁 Project（`:333`），最后 `db.delete(project)`／commit（`:351`、`:352`）；`SET NULL` 随删除会更新收据行。若重放反过来持 receipt 等 Project，会与 purge 的 Project→receipt 形成死锁。推荐 User 下普通读取收据，之后按现有 `project_lifecycle.lock_record:50` 锁结果项目、重验 owner 和生命周期；不要给成功收据追加会在项目锁之前执行的更新。若等待期间项目已被彻删，按原结果不可用结束，绝不回到无收据创建分支。新建路径在新 Project 完成后才插入 receipt，遵循 Project→receipt；模板删除／改名与使用同取 owner User 后再锁模板，且不改动历史收据。

| 生命周期动作 | 收据合同与实现边界 |
| --- | --- |
| 删除来源作品 | 模板已有独立快照，收据也不依赖来源作品；均保持可读 |
| 删除模板 | 不删除成功收据；重放先查收据，原作品仍正常即可返回。真正的新 key 使用已删模板返回 404 |
| 结果作品软删／恢复 | 收据和结果 FK 保留；软删期间不返回正常作品内容、不重建，恢复后同 key 仍指向原作品 |
| 结果作品彻删 | FK `SET NULL` 留下持久成功事实；不能配置 Project／模板 ORM `delete` 或 `delete-orphan` 级联到收据。现有 Project 已有 SQLite AUTOINCREMENT（`models/entities.py:245`），但仍不能拿裸旧 ID 查找未来另一部作品 |
| 清理收据／删除账号 | 不设会让已消费 key 重新变为“未使用”的 TTL 或普通删除接口；若日后压缩，只保留 user＋key＋摘要＋结果已删标记也须维持永久去重。账号最终删除可随 User CASCADE，账号已不能认证 |

**单书注册表。** 推荐的结果 Project 外键会使收据进入项目相关表集合：`backend/tests/test_project_transfer.py:53` 沿引用方向反射，`:68` 要求相关表完整登记。因此收据应加到 `project_transfer/registry.py:90` 的 `NOT_EXPORTED`，理由为“用户创建请求幂等凭证，不随单书备份迁移”；不可放入 `RULES` 或重映射到导入的新书。模板／模板图片若自身仍无直接或间接指向 Project 的 FK，保持纯用户级，不因 receipt 引用模板而反向变成项目表。单书恢复是新的创建意图，不能移植旧 key 或恢复旧收据；整库恢复则应保留收据表，避免恢复后重复执行已经成功的请求。

建议追加后续验收：真实 PostgreSQL 同用户同 key 并发只得一个结果；不同 key 抢最后额度只成功一次；不同用户同 UUID 互不命中；同 key 不同请求 409；成功后额度降为 0／模板被删仍可重放正常结果；软删／恢复／彻删后重试始终不新建；重放与系统 purge 并发无死锁；最后一步失败时项目与收据均回滚；模拟提交成功但响应丢失后能找回原结果；反射测试确认收据仅列入 `NOT_EXPORTED`。

## Caveats / Not Found

- 未发现现有小说模板模型／API；提示词模板是另一类用户资源，不能复用其 Jinja2 逻辑。
- 本报告只读取源码和规范；没有运行任何产品测试、性能测量、浏览器或迁移命令。
- 新缩略图表和笔名关联表尚未实现，本报告仅定义模板需要的独立资源合同，具体存储接口须与相应子任务设计对齐。
- 模板保存当前值、新书独立、模板仅本人可用、风格后续编辑沿用既有风格库语义，应进入最终产品摘要；它们不是本研究替用户新增的授权。
- 不需第三方库研究，因此无外部技术引用；所有判断以本轮读取的本地代码为依据。迁移和格式值可能被并行开发改变，实施开始前核对拥有源。

## 规划对照检查（2026-11-16）

范围：只读父任务及 `../11-16-novel-templates/` 的 `prd.md`、`design.md`、`implement.md` 共六份文档，对照本报告已完成的代码调查；不重新扩展源码调查、不实施、不运行产品测试。发现 **2 项实施前需补齐的合同**，未凑足五项。

1. **保留关系，但未明确关系类型的快照与重建。** 子任务 `design.md:17` 保留两端已选人物的关系，`:32` 必要依赖列表与 `:78` 落库顺序却没有关系类型目录；配置组 `:15` 也只列字段／选项和分类。已有 `backend/app/services/relationship_service.py:652` 的关系创建依赖类型身份，`project_transfer/importer.py:1427` 对关系类型另有映射步骤；来源的本书自定义类型 ID 不能直接给新书使用。请在严格 DTO、依赖／source_stamp 与落库顺序补入“实际保留关系所需的全局／自定义关系类型定义及局部 ref”，明确何时复用全局类型、何时新建本书类型，不按名称猜测身份；配置组未勾选时仍带此最小必要目录。验收增加“自定义关系类型＋关系两端入选，建书后类型语义正确且无源 ID”。这不要求新增用户可选实体种类。

2. **模板身份必须禁止删除后的 ID 复用，不能只靠 revision。** 子任务 `design.md:41` 仅写“自增 id、revision≥1”，`:61`／`:62` 的写入条件仅使用模板 ID＋expected_revision；未明确 SQLite AUTOINCREMENT 或等价永不复用身份。如果删除最大 ID 的模板后新建，SQLite 普通整数主键可能复用旧 ID，新模板 revision 又从 1 开始，旧页面的替换／删除／创建请求就可能命中新模板，违反 `prd.md:26`／`:39` 的旧版本保护。请明确模板表使用 `sqlite_autoincrement=True`（或稳定不可复用 UUID），补真实迁移及“删除最后模板→新建→旧 ID／revision 请求不能命中新对象”的回归。模板图片按当前模板 revision＋local_ref 访问，不必因此扩大成新图片版本系统。

其余重点已对齐：本人活跃来源与本人模板库、协作／管理员不可越权；当前 planned 投影转 confirmed 起点；当前关联不复制源历史但允许本次合法创建记录；风格建书恢复内容且保留后续共享编辑例外；原 actor gate／User→Project 且不反向取 Series；成功收据先重放、Project SET NULL 后不重建、模板删除保留收据、purge 的 Project→receipt 与重放普通读 receipt 不形成反向行锁。证据集中于子任务 `design.md:9`、`:25`、`:30`、`:34`、`:69`、`:75`、`:76`、`:80`，父任务 `design.md:53`、`:57`、`:75`、`:80`；本结论仅为规划一致性检查，不代表这些行为已实现或验证通过。

### 关系类型快照与身份映射推荐

本次仅补读类型拥有模型、schema 和现有导入／类型解析片段。`backend/app/models/entities.py:403` 定义完整业务列为 category、role_a、role_b、sort，project_id 为空表示只读全局类型；当前没有稳定的 builtin_key。`schemas/relationship.py:14` 给出五种分类与称呼长度约束。推荐严格类型 DTO 为 `{ref, origin, category, role_a, role_b, sort}`：ref 是模板内唯一局部身份；origin 为 `global | project`，由服务端来源归属生成；category 只允许 `family/social/hostile/work/other`；role_a／role_b 为去除首尾空白后非空、最多 20 字符的原始称呼；sort 为严格整数并限制 PostgreSQL Integer 可表示范围。拒绝未知字段、源 id／project_id／时间戳，不截短、不把未知分类降为 other。仅保存实际保留关系引用的类型；关系用 `type_ref` 和有序 `a_ref/b_ref`，source_stamp 包含完整定义。

- **身份和方向：** 有序语义元组为 `(category, role_a, role_b)`，origin 保留全局只读／项目可编辑的来源区别；sort 是需要冻结的显示属性，不独立充当身份。不能仅按称呼、忽略分类、交换两端、忽略大小写或做近似／别名匹配。重建后只重算 pair_low／pair_high，不为排序而交换关系的 a／b 语义。
- **复用全局的唯一条件：** origin 为 global，且目标数据库恰有一个全局类型的 `(category, role_a, role_b, sort)` 与快照逐字段完全一致。此时复用该只读行；无候选、定义变化或存在多个候选时，不猜选任何一行，按保存字段创建新项目类型并在使用摘要说明已冻结为本书自定义类型。origin 为 project 时始终创建本书类型，即使全局有相同称呼／完整内容，也不把原可编辑类型变成全局只读类型。
- **落库和冲突：** 先规划 `type_ref → 新建／复用后的 type_id`，项目类型按完整快照值无提交插入，再将明确的 type_id 传入 `relationship_service.create_relation`。该显式 ID 分支不反转方向（`relationship_service.py:155`、`:677`）。不要复用只看称呼／允许反向匹配的 `find_type:101`，也不要直接复用备份 `_relationship_types` 的称呼对匹配与合并（`project_transfer/importer.py:1427`）。项目唯一约束实际为 `(project_id, role_a, role_b)`，不含 category（`models/entities.py:407`）；若两个不同 ref 因全局回退都必须成为同一项目称呼对，预检返回明确类型冲突并整体拒绝，不合并身份、改称呼或丢分类。正常的“复用全局＋新建同称呼项目类型”不触犯该约束。
- **最小验证：** 全局完整一致时复用；全局分类／称呼／sort 变化时恢复快照为项目类型；项目类型与全局同称呼仍独立可编辑；父亲／子女方向不反转；全局回退与项目类型唯一键冲突时零副作用。模板身份另按主设计采纳的 SQLite AUTOINCREMENT＋PostgreSQL sequence，明确禁止应用复用 ID 或重置序列，保留旧 ID／revision 不能命中新模板的验证。

以上为实现建议；未改产品代码，未运行测试。

### 两项规划问题关闭状态（2026-11-16）

仅复核主会话对模板子任务 `prd.md`／`design.md`／`implement.md` 和父任务 `design.md` 的对应修订，原两项问题均已在规划层关闭：

- **关系类型合同：已关闭。** 子任务 `design.md:29` 明确严格字段和有序引用，`:31`／`:32` 区分完整一致的全局复用与项目独立重建，`:33`／`:34` 明确方向和冲突全拒绝；`:41`／`:42`／`:89` 已接入依赖、source_stamp 和落库顺序。`prd.md:33`、`implement.md:41` 已列入验收，父 `design.md:55` 同步。
- **模板身份复用：已关闭。** 子任务 `design.md:50` 明确 SQLite 模型／迁移 AUTOINCREMENT、PG sequence 及禁止旧 ID 分配／序列重置，`:105` 要求真实迁移验证；`prd.md:39`、`implement.md:11`／`:43` 覆盖删除最大 ID 后新建、各旧请求不能命中新模板，并保留原成功意图重放。父 `design.md:81` 同步。

此处关闭的是文档合同缺口，不代表实现或验收通过；没有扩大审查范围，未运行产品测试。上述实际行为仍由后续实施与所列验证证明。
