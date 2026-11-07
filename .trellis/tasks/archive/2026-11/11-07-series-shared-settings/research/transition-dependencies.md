# 研究：角色／世界观归属调整的引用与恢复路径

- 问题：作品条目提升到系列、系列条目移回单书、作品退出系列时，哪些依赖必须保留或阻断，哪些可在使用时重验？
- 范围：内部；角色、六类世界观及两者的持久引用、恢复和派生路径。静态核查源码与回归测试，未运行测试，不预定表结构。
- 日期：2026-11-07

## 必须保护的作品数据

| 依赖 | 已核实的源码 | 对归属调整的含义 |
| --- | --- | --- |
| 章节选角、规划 POV／世界观关联 | `backend/app/models/entities.py:408–417`（出场关联）；`backend/app/models/planning.py:9–25,48–54`（POV、脱离来源选角及世界观）；`backend/app/services/planning_lifecycle.py:55–84`（只允许同项目角色，关联细纲镜像章节选角） | 不可直接级联或置空；同一本书内原有指向与章／细纲的选角权威关系保持不变。 |
| **非 FK** 的实时选角 | `backend/app/models/entities.py:536–557` 的 `ChapterPlan.character_ids` 和 `backend/app/models/planning.py:21–24` 的 `PlanningUnit.detached_character_ids` 都是 JSON 数组；`backend/app/services/planning_lifecycle.py:166–176` 有专门清理逻辑；`backend/app/services/agent/effects.py:726–748` 也单独做冲突检查 | FK 扫描并不够；没有已建章的细纲、没有来源的待关联规划同样算结构化使用，不能漏掉。 |
| **可恢复**的大纲快照 | `backend/app/services/outline_snapshot_service.py:44–74` 保存 `data.plans[].character_ids` 与规划绑定；`246–269,294–307,311–319` 在恢复时复制细纲数组，接回在场章节才改用当前选角；`backend/app/services/planning_lifecycle.py:248–291` 用 unit、epoch、章锚点决定是否接回规划，不能据此认定所有细纲数组都重新校验 | 其他书唯一引用藏在历史快照的情况也要处理。保守首版可计入依赖；若将其排除，必须在恢复前独立重验并明确冲突，不能让旧 ID 静默变成悬空、越界或同名替换。 |
| 本书的人物关系、组织成员、任职、伏笔 | `backend/app/models/entities.py:318–327,371–378,456–486,773–782` 有实体 FK，人物关系的角色对全局唯一；`backend/app/models/progressions.py:52–85` 用作品和具体角色／世界观等作为时间线主体 | 这些关系与剧情位置归作品；共享条目不能把它们和其他书的同条目拼成一份历史。提升不应通过删除旧实体触发级联；移回、退出也不能使依赖失效。 |
| 自定义字段实体引用、基线、时间线旧事件、AI 单向关联 | `backend/app/models/setting_fields.py:81–136` 通过同项目 handle 保持角色／世界观身份，AI links 两端有同项目复合 FK；`backend/app/services/agent/effects.py:681–725,752–769` 同时检查关系 FK 和时间线基线／字段基础值／历史事件的语义引用（包含停用与孤儿事件） | 应检查存储中的真实引用，而非仅查当前有效投影；引用藏在 JSON／handle 背后时，也不能因为当前不可见就丢弃。 |
| 回收站作品 | `backend/app/services/recycle_bin_service.py:53–93` 标记生命周期并排空任务，不删子数据；`188–209` 同 ID 恢复；`241–263` 才物理删除。当前角色／世界观的父作品 FK 是 `CASCADE`（`backend/app/models/entities.py:255–275`） | 回收站作品的依赖不可因普通列表过滤软删除而消失；恢复能力还存在就要保护其引用。原书彻删只应删该书私有状态和引用，不可因原书的 FK 连带删除别书仍用的共享数据。 |

## 执行时重验，而不是永久阻断

- **AI 任务输入和草稿。** `backend/app/models/entities.py:662–671` 将 input／checkpoint 存为 JSON。关系生成例子：`backend/app/services/task_engine/handlers/entities.py:34–80` 创建时校验选中 ID 后持久化 request，运行时从原 request 重建；`backend/app/services/entity_ai_service.py:544–560,657–707` 用当前作品身份与 `never` 再校验并组装上下文。归属变化后，待执行／重试／提交草稿须按当前作品、系列成员和当前条目身份重验；不能用旧请求扩大权限，也不能以任务中历史的 ID 作为永久业务绑定。
- **真实出站与会话。** `backend/app/services/ai_dispatch.py:59–88,151–178` 的戳绑定作品、可见版本、历史代次及回收站代次。若某书的有效条目范围收窄，旧投影不得在归属切换后用新戳继续发送；需要影响到该书的实际出站门禁和历史封存，不能只更新发起变更的书。会话提案确认会重建范围、比对预览（`backend/app/services/agent/service.py:321–365`，ID 校验见 `backend/app/services/agent/tools/references.py:154–199`）；历史文本仍是历史，不需要改写。
- **Agent 已执行效果（恢复写入，不是普通预览）。** `backend/app/models/agent.py:53–62` 保存提案与 effects；`backend/app/services/agent/service.py:621–654,666–679` 的人工撤销路径没有调用 `require_open_history`。现有 `effects.py:647–656,986–996,1108–1145` 按单项目 scope 比较当前快照、在保存点中恢复并做图校验。共享后需针对当前归属重验撤销目标与恢复的所有引用；无法原样安全恢复就整体冲突，不得复活或覆盖系列条目，也不要仅靠“封存对话”假设撤销失效。
- **分析回写。** `backend/app/models/entities.py:719–738` 的分析变更 target/before/after 是 JSON。`backend/app/services/writeback_service.py:661–707,792–829` 的确认／撤销包含章内容哈希、时间线版本或前后值校验。改造时这些检查还需覆盖目标的当下归属与允许写的作品层；待确认记录和审计史不构成永久阻挡，已落地的本书时间线才是应保护的资料。

## 可重算派生数据与传输边界

- 字面提及不是持久引用。`backend/app/services/mentions/context.py:40–75` 从作品取候选；`backend/app/services/mentions/matcher.py:250–286` 的 LRU 按来源、原文哈希和词典指纹索引。脱离后可按新词典重算索引和热力图，原文章、笔记、历史对话等不得因为字面提及而被改写或永久锁住。
- **单书备份不能悬空，也不应意外扩大授权。** `backend/app/services/project_transfer/registry.py:124–129,179–185,281–286,366–395,410–421,460–466` 按项目表规则导出，登记 FK 和 JSON 重映射；`backend/app/services/project_transfer/remap.py:95–108,211–245` 对映射不到的 JSON 数组 ID 会去掉；`backend/app/services/project_transfer/importer.py:542–565,1462–1482` 对缺 FK 报错，在导入事务中创建新作品。共享基线不能只导出裸 ID；如首版允许系列成员书独立备份，须提供能在新环境恢复的本书依赖闭包，或在不能完整导出时明确拒绝。不要把单书备份当作系列成员关系、整库共享权限或同名自动关联的授权。Agent 提案与效果是明确不随书迁移的运行数据（`registry.py:90`）。
- 流式备份也有归属竞争：当前 `backend/app/services/project_transfer/exporter.py:155–204` 最后校验 AI 可见版本再闭合文件。若导出过程中共享归属或被导出的依赖集合改变，需要定义一致性边界，不能生成看似可用却混合两代资料的备份。

## 保守首版守卫建议

按「操作后仍可合法解析」预检引用：**提升**只在原书引用与作品专属资料能原样保留时放行；**移回／退出**只在不会使活跃或可恢复作品的结构化引用失效时放行。若有阻断，给作者指出受影响作品及引用类别，整笔不改，不静默级联、不自动改绑；预检与提交应处在能和引用写入串行化的边界内，避免先查无引用、随后并发添加。旧输入／效果在使用时拒绝或按当前范围重建，缓存重算，不扫描正文里的字面同名。

例：甲书提升「林遥」后，原书选角与细纲不变；乙书仅在正文写过「林遥」，不因文本阻挡移回甲书；若乙书把她放入了未建章的细纲，或只在可恢复的大纲快照中仍被引用，首版阻断。若乙书在回收站且仍可恢复，规则相同。若只有乙书排队的 AI 草稿或旧 Agent 效果，则不永久阻挡归属变化，但不得按旧归属继续执行或还原。

## 现有测试与相关规范

- `backend/tests/test_planning_lifecycle.py:215,249,265,409,438` 覆盖快照、当前选角、真删除清理 JSON 与退役身份。
- `backend/tests/test_agent_effects.py:170,190,210,246` 覆盖新增依赖、非 FK 选角、其他作品占用身份及原子撤销；`backend/tests/test_setting_ai_visibility_formats.py:235,251,284` 覆盖撤销中的可见性保护。
- `backend/tests/test_recycle_bin.py:63,342,361` 与 `backend/tests/test_recycle_bin_tasks.py:41,65` 覆盖同 ID 恢复、清理竞争、备份和任务；`backend/tests/test_project_transfer.py:68,78,602` 覆盖注册表反射与历史重映射；`backend/tests/test_ai_visibility_dispatch.py:197,239` 覆盖实际发送竞争。
- 对照 `.trellis/spec/backend/{outline,planning,setting-ai-visibility,agent,recycle-bin,project-transfer}.md`。

## 限制

以上是首版安全边界的代码证据，不是现成的统一依赖扫描器。未验证未来的共享数据表示、不同步时的导出产品选项或性能预算；不把当前同项目过滤直接放宽成系列过滤，也不在本研究决定任何迁移实现。外部资料不能回答这些仓库特有路径，本文件无外部依赖。
