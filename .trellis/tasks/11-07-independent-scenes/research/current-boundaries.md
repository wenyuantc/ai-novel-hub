# 章内独立场景：首轮证据与范围决策

研究日期：2026-11-07。此文记录已核实事实与待选方案，不代表用户已批准技术设计。

## 1. 来源需求与历史决定

- `TODOS.md` 第 7 项要求独立正文、摘要、顺序、场景 POV／标签／关联设定和卡片／网格排序。
- 第 1 项明确将独立场景位置留给第 7 项；第 3 项明确未交付场景与场景 POV。不能只因三个勾选项提到卡片就忽略前两项留下的接入边界。
- 已归档的 `11-05-planning-matrix-subplots/prd.md` 用户确认了章级规划同时进入 AI 生成和重写、覆盖全部现有设定类型、规划与正文提及分开呈现；其范围外保留独立场景。该决定描述已有章级行为，不自动授权本任务的场景级 AI 范围。
- `11-03-setting-progressions/prd.md` 的旧资料策略是保留当前档案为待校准基础，不自动重放缺失历史；其范围外保留章内精确位置。本任务不得把旧章／新场景自动拆分当成历史事实。

## 2. 正文不是可以随意增加的第二份字段

| 证据 | 当前行为 | 对规划的约束 |
|---|---|---|
| `backend/app/models/entities.py:520`、`:531`、`:533` | `Chapter` 保存单份 `content` 和字数，没有场景。 | 需决定场景正文与章正文的唯一事实来源；不能双向独立保存再猜测同步。 |
| `backend/app/services/chapter_service.py:182`、`:230` | 旧更新接口直接改章正文、重算字数并记录手动版本。 | 仅增加场景表和卡片，不改变旧写入口，会留下覆盖或不同步风险。 |
| `backend/app/services/generation_service.py:415`、`:421` | 整章 AI 保存直接覆盖 `Chapter.content`。 | 场景化后的整章 AI 结果如何落回场景，需要明确产品行为与受控写入口。 |
| `backend/app/services/rewrite_service.py:240`、`:256` | 局部改写基于整章字符串位置，采纳时拼接替换。 | 需定义场景内选区与整章偏移的关系，以及跨场景选区是否支持。 |
| `backend/app/services/agent/tools/write_text.py:51`、`:60` | Agent 局部替换使用旧正文条件更新和版本保护。 | 不能只修编辑页而遗漏智能体工具和撤销。 |
| `backend/app/services/chapter_version_service.py:73`、`:89`；`backend/app/models/entities.py:614` | 章节版本只保存正文和字数，恢复直接写章正文。 | 需定义新版场景结构快照、旧纯文本版本恢复与已有结构的关系。 |

技术倾向（未批准）：优先研究统一正文写入边界和可验证的整章投影；无论最终选哪种编辑体验，安全兼容不能靠前端各自拼字符串。

## 3. 已有章级规划与场景的位置语义

- `backend/app/models/planning.py:10`–`:29`：稳定 `PlanningUnit` 绑定章／未建章细纲，持有可空 POV、绑定代次和脱离状态。`PlanningTag` 是作品级标签目录。标签可研究复用，不应默认与设定条目的 `setting_tags` 混用。
- `frontend/src/api/types.ts:3742`：来源只有 `chapter|plan|detached`；`frontend/src/pages/project/PlanningPanel.tsx:46`：只有矩阵和交织两种视图。因此场景行、场景卡片不是已有能力的开关。
- `.trellis/spec/backend/planning.md`：角色／世界观／组织／职业／关系引用各有真实身份，关系使用时间线 ID；章选角是已有事实来源。需研究场景关联与章选角、章规划的继承／覆盖语义，不制造两套章级权威。
- `backend/app/schemas/progression.py:14`–`:29`：`AnchorKind` 只有章和计划章，`StoryPosition` 只有章号与章初／章末。
- `backend/app/services/setting_progression_resolver.py:260`、`:305`：排序与截点以章号和章边界比较，没有场景顺序。若首版支持场景演变，必须端到端扩展位置，而不只是给表单新增场景选择器。
- `backend/app/models/progressions.py:135`、`:172`：数据库约束和持久锚点同样只有章级。场景重排、删除、旧事件、同号重建与当前投影必须共同设计。

## 4. 阅读、提及与备份是必要兼容面

- `backend/app/services/export_service.py:52`、`:66` 和 `backend/app/services/epub_service.py:158`：TXT／Markdown／EPUB 消费章正文。
- `backend/app/services/context_builder.py:897`：前章上下文消费章正文；`backend/app/services/mentions/sources.py:30`：正文追踪以章为来源、读取章正文。场景排序后不能让这些入口继续看到旧顺序。
- `backend/app/services/project_transfer/exporter.py:33`–`:35`：当前备份 v6／schema 0038。新增持久场景及其关联必须登记、保留稳定引用并处理旧备份；是否升级格式在设计时按注册表契约确定，不能悄悄忽略新增表。
- `.trellis/spec/backend/project-transfer.md`：项目表注册表同时用于 Agent 效果快照／撤销。因此备份和撤销不能作为相互无关的两个补丁处理。
- 本轮只列兼容面，不承诺额外新增 DOCX、场景级组合导出或全书搜索等其他 TODO 能力。

## 5. 当前写作交互

- `frontend/src/pages/project/ChapterEditorPage.tsx:161`–`:164`：本地草稿是一份章标题／章正文。
- `:288`–`:308`：保存章正文；`:339`–`:372`：未保存离页保护及 AI 前保存；`:555`–`:562`：重新生成提示现正文进入历史后被覆盖。
- `:902`–`:945`：正文文本框、章节选角、风格、设定预览、提及标记均以章为单位。
- `frontend/package.json` 未登记专门的排序库；这只能证明当前依赖列表，不决定应新增哪种实现。无论最终方案，触摸／键盘应有不依赖拖动的排序替代。

## 6. 官方参考（已实际读取）

以下是行为参考，不作为自动纳入本项目首版的授权：

- [功能总览](https://www.novelcrafter.com/features)：规划功能列出网格拖动、场景标签、POV 与手动关联；与本地 TODO 的基础三项一致。
- [Planning with the Matrix](https://www.novelcrafter.com/help/docs/plan/planning-with-the-matrix)：矩阵可维护场景 POV，并区分摘要、正文、手动关联的条目分布。
- [Organizing Your Acts, Chapters, and Scenes](https://www.novelcrafter.com/help/docs/plan/organizing-scenes-chapters-acts)：支持创建与移动场景，拖动以外提供操作菜单，移动端不只依赖拖动。本项目是否首版支持跨章移动仍待确认。
- [Progressions/Additions](https://www.novelcrafter.com/help/docs/codex/progressions-additions)：设定变化与工作场景的位置有关，回到更早场景时不把后来的变化加入上下文。
- [Generating Prose](https://www.novelcrafter.com/help/docs/write/generating-prose)：正文生成允许采纳、重试与丢弃。本文不由此推出本项目必须复制其斜杠菜单或全部生成交互。

## 7. 范围决定与后续问题

**D1：用户已选择 A（2026-11-07），按完整写作闭环继续规划。** 以下保留首轮方案依据，不再作为未解决问题。

- A（建议）：基础三项之外，同时接入场景级 AI 生成／重写、场景规划与按场景位置读取有效设定；阅读／备份／原有章节入口保持兼容。收益是 POV 和关联设定直接影响场景写作，同章后段变化不会提前进入前段设定输入；代价是改造和验证面更大。
- B：首版先交付手工场景正文／摘要／元数据和卡片排序，场景级 AI、矩阵及演变位置后续追加。范围更小，但必须在 UI 和 TODO 如实说明这些边界，不能声称完整接入。

两条路径都必须保护已有正文、版本与备份，不以缩小范围为由接受静默覆盖。

D2 旧章统一场景化、D3a 整章 AI 保留结构、D3b 旧纯文本保护结构与按场景提取、D4 默认继承／按项覆盖、D5 章内排序与手动拆并（暂不跨章）均已获确认，依据见各专题研究。技术结论集中在 `integration-contracts.md`，当前没有未解决的用户产品阻塞问题，最终设计仍须另行审阅。

## 8. 当前验证边界

本轮为只读研究与任务文档编辑，没有改动产品代码、数据库、依赖或 TODO 完成状态，没有启动实现或运行产品测试。最终设计与执行计划已写入任务的 `design.md`／`implement.md`，仍待用户最终审阅与后续实施批准；源码锚点均基于当前 `main`，不能直接复用归档任务的旧行号。
