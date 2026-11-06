# 现有字段与分类边界

调研时间：2026-11-05。任务处于规划阶段，本文区分源码事实、官方参考与待用户确认的产品行为。

## 源码事实

| 位置 | 已核实行为 | 本任务影响 |
| --- | --- | --- |
| `backend/app/models/entities.py:231`、`:244`、`:269`、`:390` | 角色、世界观、组织、职业是不同主体；世界观细分地点、势力、物品、规则、历史、其他。 | 字段适用类型需覆盖这些入口，不能只挂接角色表单。 |
| `backend/app/schemas/story.py:7`、`:32`、`:63` | 世界观子类型固定；世界观和角色的创建结构没有条目标签。 | 统一标签是分类的基础能力；作品的 `Project.tags` 不能代替条目标签。 |
| `frontend/src/utils/world.ts:3`、`:19` | 世界观列表按固定分类分组，未知分类归入其他。 | 新自定义分类应明确与现有类型的关系，不能悄悄把自定义分类值写入旧枚举。 |
| `frontend/src/utils/characterList.ts:10`、`:26` | 角色列表已有定位筛选和文本搜索，未提供保存分类。 | 自动分类可作为新组织入口，与现有筛选组合。 |
| `backend/app/services/setting_progression_service.py:1050`、`:1072`、`:1098` | 文本字段属于单条时间线，按时间线读取、去重；只允许角色和世界观。 | 目前没有作品级按类型复用的字段定义，组织和职业定义也未覆盖。 |
| `backend/app/services/setting_progression_service.py:1120`、`:1143` | 修改字段使用时间线修订号；字段有有效变化引用时禁止删除。 | 字段改名、迁移、类型变更及删除必须保留现有历史与并发保护。 |
| `frontend/src/components/progressions/TextFieldsPanel.tsx:18` | 字段入口位于剧情变化面板，维护故事起始时的基础文本。 | 新的类型配置和值编辑需明确与起始文本、按章变化的关系。 |

## 已有任务决策

`.trellis/tasks/archive/2026-11/11-03-setting-progressions/prd.md`、`design.md` 已明确：第 1 项只实现角色／世界观的条目级最小文本字段；完整字段配置、下拉、引用、分类交由第 4 项。旧字段的稳定 ID 与剧情变化关联，不能因新建类型模板而丢失历史。现有资料已覆盖相关历史，本轮无需再次检索会话记忆。

## 官方参考（2026-11-05 查阅）

- [Codex Details](https://www.novelcrafter.com/help/docs/codex/codex-details)：四种字段形态为多行文本、单行文本、下拉和设定引用；字段可配置适用类型。引用字段用于组织资料，不自动引入被引用条目的完整提示词上下文。AI 可见性、系列归属在本项目另有第 5／6 项待办。
- [Codex Categories](https://www.novelcrafter.com/help/docs/codex/codex-categories)：分类通过适用类型、标签和下拉字段条件组织条目；同条目可以命中多个分类。删除分类保留设定条目。
- [Codex 功能](https://www.novelcrafter.com/features/codex)：分类按标签和字段组织资料，字段包含文本、快捷信息、标准选项和设定引用。
- [Progressions on Codex Details](https://www.novelcrafter.com/help/docs/codex/progressions-codex-details)：2026-11-05 再核实，只有多行文本支持剧情变化；单行、下拉与引用不支持。该参考可用于推荐首版边界，不能替用户选择 aiNovel 的行为。

上述是参考产品的行为，不能替代用户对 aiNovel 分类交互和范围的决定。

## 产品决定清单

1. **分类语义（已确认）**：用户在 2026-11-05 明确选择“采用自动归类，按标签或下拉字段配置分类规则”。分类成员由规则计算；新真正设定类型会影响规划矩阵、提及、AI 与备份注册，需与规则分类区别。
2. **字段与时间线（已确认）**：2026-11-05 用户回复“都要支持”，明确四类字段全部支持按章回溯。此前静态字段建议已被否决，不得沿用。`backend/app/models/progressions.py` 中的自定义字段值为文本，`schemas/progression.py` 的 `EffectiveOut.custom_fields` 为 `dict[int, str]`，`setting_progression_resolver.py:180` 起仅按文本校验；本期必须增加类型化事件、引用／选项历史保护与相应上下文解析，不可把最新值冒充历史值。
3. **旧字段提升为类型配置**：不能仅按显示名称自动合并字段，因为不同条目可能使用同名字段表达不同含义。需在设计阶段定义保留旧字段、复用配置及显式映射的规则。

## 当前改动边界

- 仅创建任务及需求／调研文档，未修改产品代码、数据库或 `TODOS.md` 勾选状态。
- 创建任务时工作区已有未跟踪文件 `pelican-cycling.html`，与本任务无关，不纳入后续实现与提交。
