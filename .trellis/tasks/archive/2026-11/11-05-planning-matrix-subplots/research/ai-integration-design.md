# Research: 规划矩阵的 AI 上下文接入设计

- Query: 在章节与未建章细纲均可规划的前提下，如何让 POV、支线与手动设定影响生成／重写，且不泄露后续节点或把规划当事实？
- Scope: internal
- Date: 2026-11-05

## Findings

### 1. 真实入口与共同边界

| 入口 | 现有代码证据 | 接入结论 |
| --- | --- | --- |
| 单章生成 | `backend/app/services/generation_service.py:275` 调用 `build_chapter_context`，`:285` 调用 `build_chapter_messages`；job 在运行开始时按最新资料组装（`:249`） | 组装 job 时读取一次目标章节的规划投影，随 messages 固定本轮规划 |
| 按建议整章重写 | `generation_service.py:299` 与 `:313`，同样使用 `build_chapter_context` | 与单章生成复用相同投影和目标章过滤 |
| 选区改写／扩写／缩写／去 AI 味／光标续写 | `backend/app/services/rewrite_service.py:68`；`:110` 读 `build_setting_context`，`:117` 进入 `build_rewrite_messages`；操作集见 `backend/app/ai/prompts/rewrite.py:12` | 必须单独接入，不能只修改全章上下文；保留当前 op 的选区约束 |
| 自动续写 | `generation_service.py:473`；`backend/app/ai/prompts/chapter.py:102`、`:120` 原 messages + assistant 正文 + 新 user 指令 | 初次 messages 已含规划即可；不需重复读库或重复注入，否则中途修改规划会造成同一任务前后不一致 |
| 批量生成 | `backend/app/services/batch_service.py:464` 逐章复用 `build_chapter_job` | 每章开始时按该章最新规划构建；无需第二套批量投影逻辑；重试会重新构建，应在记录中反映实际使用版本 |
| 整章润色 | `generation_service.py:326` 只有正文、风格、补充要求 | 此次用户明确的是生成和重写；润色不强加“完成支线节点”，维持保留情节语义 |
| 模板编辑器预览 | `backend/app/services/prompt_service.py:269` 使用 `sample_values`，锁定变量不可被请求体替换；路由 `backend/app/api/v1/prompt_templates.py:70` | 这是模板样例预览，并非某章真实提示词。新增变量需补样例和锁定元数据，不能把它描述成真实章节预览 |
| 章节设定预览 | `backend/app/api/v1/chapters.py:50`，只读调用 `build_setting_context` | 现接口仅是章初有效设定。规划面板宜单列“本章 AI 规划内容”投影，复用生成使用的过滤器；不要默默把纯设定预览改成完整生成提示词 |

### 2. 推荐的首版数据语义

1. **只有目标章节自身的 POV、支线推进节点与手动关联进入新增规划块。** 节点是“本章计划／本章推进目标”，不是已经发生的事实。生成用作本章目标；整章重写在保持显式保留项的前提下遵循；局部操作仅作为一致性背景。
2. 首版不额外注入早期支线的规划节点。已有前情、正文和剧情记忆已经承担必要历史上下文；从“章节有正文”不能证明某个规划节点实际发生。这样无需发明“已兑现节点”的新事实状态，也能避免把早期未落实计划冒充剧情。
3. 未建章细纲支持保存和展示规划，但不直接执行正文生成。现有生成／重写都是以真实 `chapter_id` 读取与校验；建章衔接完成后再走现有入口。其规划不能通过估算章号插入另一章的 AI 上下文。
4. 支线总述／作者备注默认不注入 AI，尤其不能默认带入终局、未来节点列表或整条支线摘要。节点内容及名称仍是作者填写的文本，系统只能保证排除后续**节点记录**，不能宣称自动识别并清除自由文本中的所有剧透。
5. 该保证仅针对本次新增规划数据。既有全卷大纲仍由 `context_builder.py:697` 注入；不能把此次验收夸大成“整个提示词绝无任何未来信息”。

### 3. 共用投影与锁定变量

建议新增只读服务边界（名称可在实现时随目录规范确定）：

`load_chapter_planning_context(db, project_id, chapter_id) -> ChapterPlanningContext`

返回类型化数据：目标规划单元身份与 revision、POV 角色 id／名字、目标章支线节点（支线 id／名称、节点 id／内容／序号）、手动关联的实体 ID、缺失引用提示。访问权限由上层入口校验，查询仍需限定同项目，读取全过程 `db.no_autoflush`；不物化 ORM 当前状态。

- 查询以稳定章绑定为依据，不用显示序号或相邻章节推断关联。已绑定 plan 和 chapter 的同一单元只投影一次；后续节点即使在 UI 排序中临近也不匹配该 chapter。
- 单章、整章重写与局部重写共享读取／过滤器，按 `scope=chapter|selection` 形成不同用途的文案；UI 预览消费同一投影。
- 推荐在 `chapter.generate`、`chapter.revise`、`chapter.rewrite` 上新增**独立锁定变量** `planning_context`，空规划返回空串；用模板文件维护约束文案，Python 负责数据读取、归一化和预算。
- 不建议只塞进现有 `context`：它在 `backend/app/ai/prompts/registry.py:139`、`:182` 只是 `required=True`，不是锁定变量，用户覆盖模板仍可通过条件或过滤器截掉部分内容。仅修改默认模板不能保证旧覆盖生效。
- 锁定变量已有完整基础：`backend/app/ai/templating.py:289`、`:299` 在旧模板漏掉非空锁定内容时原样追加；`backend/tests/test_prompt_templates.py:244` 有旧 rewrite 覆盖补入 `setting_context` 的回归范例。新增变量必须在默认模板顶层直接引用，不能放在条件／循环内。
- `planning_context` 应是自足的带用途标签文本；旧模板兜底追加时也要能表达“本章计划，不是既成事实”“局部改写不得强制完成全章节点”。固定措辞仍放 `.j2`，可通过专用只供代码调用的固定片段渲染，避免在 service 中散落自然语言指令。
- 自动续写沿用最初的锁定块；批量每章重新生成。生成记录可保存规划单元／节点的 id、revision、scope 和截断摘要，实际 messages 已记录最终文本，避免另造不可核对的假快照。

### 4. POV、选角与有效设定

- 视角人物使用角色 ID，且必须属于当前作品。POV 不等于“手动出场角色列表”：建议在 AI 选角投影中把 POV 角色作为优先涉及角色加入并去重，而不偷偷改写作者维护的手动关联。
- 角色详情仍经 `backend/app/services/context_builder.py:510` 的 `resolve_effective_cast` 解析目标章初设定，不能直接拼最新 `Character.current_state`。角色名称可用于 POV 标题，完整历史状态由有效角色卡承担。
- 手动世界观关联（含地点／物品）只保存条目 ID；`context_builder.py:485` 已批量读取世界观时间线并在目标章初求值（`:495`）。可扩展该投影以先排列手动关联条目并去重，其余世界观保留原逻辑；不要另读当前 `WorldSetting.content` 作为“优先资料”。
- 关联条目优先级应在预算裁剪之前落实，不要只加一串名称却声称相关设定已确保进入 AI。
- 写作风格解析已有请求 > 章覆盖 > 项目默认优先级（`backend/app/services/style_service.py:354`）；风格的 `pov` 是自由文字，经 `templates/style.block.user.j2:4` 输出，不是角色绑定。
- 建议产品优先级：本次作者明确额外指令 > 本章绑定 POV > 风格中的泛化视角描述；其余文风不变。POV 绑定只指定观察中心，不擅自决定第一／第三人称。必须把该优先级写进固定规划文案，不能仅靠消息出现顺序；无需覆盖或修改持久风格对象。

### 4.1 全部现有设定类型的人工关联（用户后续确认）

主会话传入的最新确认：手动关联覆盖角色、所有世界观分类、组织、职业；不扩大正文提及索引的匹配类型。

- **类型与权限边界**：关联编码使用明确的 `character | world | organization | career` + ID，不用仅有整数的混合列表。每类批量查询须含 `project_id == 当前作品`，先验证全部引用再写入；本章 AI 查询再次按项目过滤，不信任前端旧快照。已有组织/职业读取 helper 证据为 `backend/app/services/organization_service.py:145` 与 `backend/app/services/career_service.py:141`；二者均经 `get_manageable_project`，真正 AI 执行仍沿用原有 owner-only 入口。
- **独立注入组织／职业**：当前 `resolve_effective_cast` 在无涉及角色时直接返回（`context_builder.py:515`），组织来自成员遍历（`:566`—`:590`），职业来自角色挂接（`:592`—`:614`）。因此只把手动 ID 交给现有函数不会生效。建议给投影增加显式关联集合，在角色历史求值后合并同作品 Organization/Career；无角色时也必须执行显式合并步骤。
- **去重**：隐式组织、手动组织在 `organizations[id]` 合并；职业同理在 `careers[id]` 合并，统一一次读取 `career_service.stages_of`（`:614`）。世界观按 world ID 去重并保留全部分类，角色按角色 ID 去重。跨类型相同整数 ID 不能相互去重。显式关联条目优先排列，并保留来源标识供预览解释。
- **注入内容**：手动关联组织带名称、类型、宗旨和已有说明等有界静态资料；现 `format_organization`（`context_builder.py:407`）只含名称/类型/宗旨，若要体现作者手动选择的完整相关资料，应扩展为可选的有界详细格式，避免悄悄丢弃 `description`。职业带类型、描述、条件、能力、规则和有序阶段；现 `format_career_ladder`（`:432`）只含阶梯和 200 字规则，可沿用其有界格式但应在最终设计列清哪些字段进入。字段存储证据：`backend/app/models/entities.py:267`（Organization）、`:388`（Career）、`:409`（CareerStage）。
- **绝不产生角色事实**：关联组织只是把组织设定列为本章参考，不意味着任何角色加入该组织；关联职业只是引用职业体系，不给 POV 或任意角色授职、不默认第一阶段、不建立 `CharacterCareer`。角色已有成员状态及职业阶段仅来自该章的历史求值；不调用 `apply_membership`／`apply_assignment` 等写服务。
- **历史边界必须诚实**：`backend/app/schemas/progression.py:8`、`:60`—`:61` 明确 `career` 时间线身份是 `{character_id, career_id}`，`membership` 是 `{character_id, organization_id}`；不存在独立组织说明或职业体系本身的时间线。角色字段、世界观正文、关系、职业挂接／阶段与成员状态按目标章求值；组织基础档案与职业规则／阶梯仍按现有当前设定读取。不得把它们描述为“已完整回溯到目标章的组织／职业档案”。如果用户要求连基础档案都按章变化，那是额外的设定时间线扩展，不能由此次手动关联悄悄推导。
- **删除／改名**：查询用当前有效引用解析名称；失效引用省略并给预览提示，不能保留旧 ID 然后错误读到跨作品条目。领域删除通过关联 FK／清理事务消除引用；规划文案不得从临时旧名称反向匹配重建引用。
- **预算**：显式关联组织/职业不能仍落在会优先被整块清空的“附带组织／职业”位置，应以显式来源在同一预算内优先保留，至少保留身份及截断提示。没有显式关联时保持原有组织／职业格式与裁剪顺序。

### 5. 局部操作与预算

- 局部模板已要求改写／缩写等保留原情节，见 `backend/app/ai/prompts/templates/chapter.rewrite.user.j2:20`。新增规划应说明“用于保持 POV 与本章方向一致；只处理当前片段／光标续写，不把全部节点强塞进本段，也不提前补齐未发生内容”。
- 独立锁定规划块仍需计入原预算。全章入口预算在 `generation_service.py:278`；局部设定块上限在 `rewrite_service.py:115`。若将规划另加变量但不从总预算预留，会绕过现有字符控制。
- 推荐先按目标作用域形成有界规划块，再从现有 context/setting_context 预算中预留其渲染长度。保留 POV 身份和节点身份；节点正文按确定顺序公平分配配额，截断有标记，并在预览／生成记录提示使用了截断内容。不要无提示地只保留第一条支线。
- 无规划时：变量空串、预算不变、默认模板新增引用不引入空行，原有 prompt parity 保持逐字一致。参数默认空串确保旧调用方兼容。
- 当前 `assemble_context` 会先裁检索、旧情节、早期摘要、近期正文，再裁世界观、角色等（`context_builder.py:270`—`:339`）；新增规划不能误并入一个早期会被整块清空的字段。

### 6. 计划章只读上下文的可用边界

- `GET /setting-timelines/{id}/effective` 支持 `chapter_id` 或**数字** `chapter_no` 二选一（`backend/app/api/v1/progressions.py:130`；`setting_progression_service.py:1030`）。
- 底层 `resolve_timelines(..., StoryPosition)` 是批量只读求值（`setting_progression_service.py:246`），适合已确定章初位置的设定预览。
- 现 API **不接受 plan_id**，也不会为未建章细纲推算真实章号。`build_setting_context` 与世界观／角色帮助函数当前签名均需要 `Chapter`。
- 因而首版未建章细纲可预览“将传入 AI 的规划指令”，但不能冒充“该计划章的精确历史有效设定”。建章后以真实章号得到正式设定上下文。如要在建章前展示有效设定，只能明确要求数字位置并标为估算；本次没有必须新增该能力的需求。

### 7. 必须覆盖的测试与验证点

1. 第 5 章 POV／支线当前节点进入 generate、revise、五类 rewrite op；第 20 章节点、支线终局备注和未建章细纲节点不进入第 5 章规划块。
2. 同一角色同时是主角、POV、手动角色时只有一份有效卡；POV 的第 20 章新状态不会出现在第 5 章。关联地点／物品同样使用章初有效值。
3. `chapter.generate`／`chapter.revise`／`chapter.rewrite` 的旧已保存覆盖没有新变量仍获得一次完整规划块；新覆盖删变量保存失败；模板预览锁定样例不能被客户端伪造。
4. 首轮生成触发自动续写后仍只有同一份原始规划；中途编辑规划不会改变本轮消息。批量各章按本章规划注入，重试记录反映新 revision。
5. 局部重写提示词具有“保持片段范围”的语义保护；扩写／缩写／去 AI 味不会变成执行整章支线计划；不把模型生成内容当作可硬断言的确定行为，断言请求消息与替换范围契约。
6. 无规划／只有标签时提示词与旧输出逐字一致；长节点、多节点、极小预算下长度受控、POV 和节点身份保留或明确提示预算不足，不无声删除所有规划。
7. 未建章细纲的预览只返回规划信息；建章后统一读取同一规划；跨作品引用、孤儿引用不会产生其他作品的内容。
8. 沿用 `tests/test_context_builder.py`、`test_cast_context.py`、`test_rewrite.py`、`test_batch_generate.py`、`test_prompt_templates.py`、`test_prompt_parity.py`、`test_templating.py`；新增规划专属投影测试，并在集成测试捕获 FakeLLM 实际 messages。
9. 没有手动出场角色、没有 POV 的章节，单独关联组织／职业仍能看到相关设定；组织/职业同时经角色关系隐式出现时只注入一次。引用职业不会新增角色职业、阶段或成员关系，读请求不触发 flush／写库。
10. 跨类型同 ID 不互相覆盖；所有 world 分类都可注入，正文提及仍仅现有支持类型；跨作品组织/职业批量引用整笔失败；已删引用不能把新建同名或另一作品数据读进来。
11. 历史测试分别断言角色职业阶段/组织成员状态按目标章回溯、手动引用职业体系不把最新挂接阶段附给角色；UI 准确区分可回溯状态与当前基础档案。

## Related Specs / Files

- `.trellis/spec/backend/setting-progressions.md`：章初设定、无隐式写入、计划数字位置与稳定章节身份。
- `.trellis/spec/backend/prompts-styles.md`：模板锁定、覆盖兼容、预览样例、风格优先级、空变量 parity。
- `.trellis/spec/backend/story-entities.md`：章节选角与上下文预算。
- `.trellis/spec/backend/outline.md`：细纲建章与本章任务。
- `research/ai-context-boundary.md`：已确认用户选择与基础证据。

## Caveats / Not Found

- 未运行测试、未修改产品代码；本报告是基于真实代码的设计建议，不代表新增接口已存在。
- 未发现基于 plan_id 的精确章初设定预览；模板编辑器预览也不是实章提示词预览。
- 新规划块排除后续节点，不等于扫描并消除用户自由文本及既有大纲中的全部未来信息。
- 首版采用“当前节点目标＋已有真实历史上下文”最小方案，避免额外引入节点兑现状态或把建章／写过正文当作计划已发生的证明。
- 全类型手动关联可在现有有效设定投影中实现，但独立组织资料／职业体系尚无历史时间线；必须保持该能力边界，不宣称拥有不存在的历史版本。
