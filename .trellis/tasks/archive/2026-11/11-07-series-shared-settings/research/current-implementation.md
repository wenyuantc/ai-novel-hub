# 系列作品与跨书共享设定：现状调查

基线：`main`（`1564b06`），本轮静态阅读源码和测试，未运行功能或修改产品代码。

## 已确认的行为

| 范围 | 实现与定位 | 对首版的影响 |
| --- | --- | --- |
| 书库与鉴权 | `backend/app/models/entities.py:177–229` 的 `Project` 只有 `user_id` 所有权，无系列字段；`project_service.py:28–60,184–196` 区分严格 owner 与管理员单书人工读写，书库只列自己的活跃作品。 `frontend/src/pages/ProjectListPage.tsx:23–38,51–83` 是平铺书库，`pages/project/OverviewPanel.tsx:27,131,261` 维护单书信息。 | 系列是新的归属边界，不是已有的作品状态或普通标签。可以在书库加系列管理并在项目内显示归属；不能把单书管理权限等同于共享库管理权。 |
| 领域条目和目录 | `backend/app/models/entities.py:255–315,419–438`：角色、世界观、组织、职业有非空且级联删除的 `project_id`；地点是 `WorldSetting.category = 'place'`，不是单独的表。 `backend/app/services/story_service.py:46–60,103–117` 按同项目列出／取条目；`setting_catalog_service.py:17–25,43–62,123–223` 按项目聚合类型及 `(kind, id)`，供字段、分类和 AI 设置等使用。 | 不能只在书库加分组或只在某个列表拼接共享条目；也不能把系列设定永久挂在第一本书下，否则第一本书彻删时可能级联删除全系列设定。 |
| 作品使用与演变 | `backend/app/models/entities.py:408–417,536–558` 保存章节出场 FK 和细纲 `character_ids` JSON；`models/planning.py:9–26,48–54` 保存 POV、规划世界观关联；`services/planning_service.py:166–174` 在保存前验证项目。 `models/progressions.py:52–92,124–185` 的时间线归作品，事件锚在作品内的实际章节或计划章号；`services/context_builder.py:500–578,725–769` 按当前作品和章节读出基础值与有效值。 | 同一个角色在 A、B 的章末状态不同是正常需求；同章号不提供任何跨书顺序。需要保护稳定身份、原书历史和绑定，同时避免把历史挂到系列公共档案上。 |
| 结构化辅助数据 | `backend/app/models/entities.py:297–329,367–379,456–469`：组织树、组织成员、人物关系、任职使用类型特定外键，部分唯一约束不含作品 ID；`models/setting_fields.py:32–49,81–137` 的字段定义、实体引用句柄、AI 一跳关联有项目边界；`models/setting_categories.py:15–27,34–56` 的分类与字段定义有项目归属。 | “共享条目”不等于“共享关系／分类／字段目录”。尤其直接让两个项目使用同一人物行时，关系对全局唯一、成员/任职的端点键等不能自动成为逐书数据；这是方案评估项。 |
| 提及和 AI | `backend/app/services/mentions/context.py:40–76` 只加载本项目候选；`mentions/sources.py:20–80` 只扫描本书的正文、聊天、笔记和知识库。 `setting_ai_context.py:135–193` 用候选、当前输入和手选决定是否纳入，并扩展一跳关联；`ai_dispatch.py:68–88,101–146,246+` 和 [.trellis AI 可见规范](../../../../../spec/backend/setting-ai-visibility.md) 对本作品的出站版本和历史代次做校验。 | 只扩充“可选的身份”不应扩充“可读的来源文本”。成员变化、共享条目删改或收紧时，要让受影响作品的旧上下文失效；不能先发旧载荷再刷新 UI，也不能用换戳复用旧载荷。 |
| 写回与旧变更 | `backend/app/services/writeback_service.py:64–66,284–309,702–715,811–829` 有按章角色状态写回与旧值更新路径。 `services/agent/effects.py:281–295` 使用单项目快照；`effects.py:986–997,1108+` 还有撤销路径。 | 共享档案的编辑、推广、收回不能被旧的单书更新入口或撤销流程悄悄触发。项目级工具保留本书语义；共享写操作使用显式权限和交互。 |
| 复制、备份与外部读取 | `backend/app/services/card_service.py:687–761` 在目标作品新建或按同名跳过／改名，没有同步关联；`project_transfer/exporter.py:32–33,64–66,155–207` 流式导出单书并在结束前验证 AI 可见性版本，`project_transfer/registry.py:124–129,179–185` 按项目选条目，`project_transfer/importer.py:542–569` 检查文件内引用。 `services/open_api/entities.py:17–53` 的列表与详情分别有不同的服务调用，`api_key_service.py:262+` 按小说授权。 | 旧卡片和合法备份需要兼容；不能单改备份查询而留下文件内悬挂引用，也不能把新的写作目录查询不加鉴权地复用给 API Key。单书备份须明确快照范围和是否重连。 |
| 回收站 | `backend/app/services/recycle_bin_service.py:53–103,188–210,212–264` 分别处理回收、恢复和彻删，并用生命周期父行协调；项目上的外键级联决定彻删实际影响。 | 系列库生命周期需独立。退出、收回、删除不能只检查当前活跃列表；还需辨认会重新出现的结构化引用。 |

前端复用点：`frontend/src/pages/project/CharactersPanel.tsx:68–84,141–144,230` 和 `WorldPanel.tsx:48–75` 已有按作品加载、类型筛选及深链机制；`frontend/src/components/settingAi/SettingAiDetailSections.tsx:8` 与 `progressions/SettingProgressionsPanel.tsx:41` 分别承载 AI 设置与时间线，不宜混成一个“同步”开关。

现有回归资产：`backend/tests/test_projects_and_story.py`、`test_cast_context.py`、`test_planning.py`、`test_setting_progressions.py`、`test_mentions.py`、`test_setting_ai_context.py`、`test_ai_visibility_dispatch.py`、`test_project_transfer.py`、`test_open_api.py`、`test_recycle_bin.py`；前端可在 `frontend/src/utils/shelf.test.ts`、`frontend/src/api/projects.test.ts`、`frontend/src/components/chapter/settingContextPreviewController.test.ts` 的覆盖方式上补系列场景。

## 方案筛选（不是架构定案）

1. **只分组／批量导入。** 简单但每本书仍有自己的权威副本，无法保证一次修改同步，不满足目标。
2. **直接让条目改为系列所有。** 一个权威条目和 ID，语义直接；需要重审各处基于 `project_id` 的查询、单书关联唯一键、写入／撤销和级联删除。不能用全局放宽查询替代授权。
3. **系列权威档案 + 逐书的稳定引用和本书数据。** 也只有一份共享事实；可以优先评估利用现有作品端 ID 保留出场、章节历史和本书约束。代价是必须让读取／AI 一律解析权威档案，避免逐书持久化同步副本，定义目录去重、绑定及备份行为。

在决定需要同步的是基础档案还是跨书剧情后，再比较后两种的最小可逆路径。不要求首版上全套跨书关系或系列时间轴。

## 产品参考

[Novelcrafter 官方 Series Codex](https://www.novelcrafter.com/help/docs/codex/series-codex) 支持系列内共用条目、在系列与单书之间调整归属、按全部／系列／本书过滤，并可直接创建共享条目。借鉴的是可见的归属操作，不假设其引用保护、AI 或备份规则适用于本项目。
