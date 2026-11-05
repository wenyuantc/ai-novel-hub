# 规划矩阵与支线交织视图：技术设计

状态：2026-11-05 用户已批准实施；范围含章级、未建章细纲、AI 接入、全类型手动关联和显式交汇事件。一致性研究发现的三项契约缺口已收敛到本设计。

## 1. 边界与复用

- 新增作品内规划领域，复用 Chapter、ChapterPlan、ChapterCharacter 和现有设定实体；不新建正文／场景体系，不把 Foreshadow 改造成支线。
- `planning_service` 拥有规划读写与生命周期；HTTP 路由只解析参数、调用服务并返回 schema。
- `planning_context` 的只读投影服务供生成、重写和预览共用；模板文案集中在 Jinja 文件，投影不写设定、角色关系或职业挂接。
- 前端独立懒加载 PlanningPanel；矩阵、支线轨道、批量表单和 AI 规划预览共享类型与当前作品授权状态。
- 人工管理继续允许拥有者／有权限的内容管理员；真正 AI 任务继续沿用 owner-only 边界。

完整证据：`research/codebase-foundations.md`、`research/planning-anchor-design.md`、`research/ai-integration-design.md`、`research/relationship-and-crossing-contract.md`。

## 2. 数据模型

以下为拟新增模型；迁移号在实施前按当时 Alembic head 分配，不占用其他会话正在使用的号。

| 模型／字段 | 契约 |
| --- | --- |
| Project.planning_revision | 非负并发版本，默认 0；结构或规划改变后同事务递增；不随快照、导入或智能体撤销回退。 |
| PlanningUnit | id、project_id、可空唯一 chapter_id／plan_id、可空 pov_character_id、last_source_label／last_volume_label、detached_character_ids、retired 默认 false、binding_epoch 默认 0。两种来源均空且未 retired 表示待重新关联。 |
| PlanningTag | 项目内名称唯一，name 最多 50 字，tone 使用已定义主题色标识；不保存任意 CSS。 |
| PlanningUnitTag | unit_id＋tag_id 唯一、真实外键。 |
| PlanningUnitWorld | unit_id＋world_id 唯一，覆盖全部世界观分类。 |
| PlanningUnitOrganization | unit_id＋organization_id 唯一。 |
| PlanningUnitCareer | unit_id＋career_id 唯一。 |
| PlanningUnitRelationship | unit_id＋timeline_id 唯一，引用 setting_timelines 中同作品的 relationship 主体；不是当前 Relationship 载体 ID。 |
| Subplot | 项目内独立支线，名称最多 100 字、作者概览最多 5000 字、主题色标识、sort；作者概览不自动进入 AI。 |
| SubplotNode | subplot_id＋unit_id 唯一；本章推进文本 1–2000 字，去除首尾空白；节点身份与源章号无关。 |
| SubplotCrossing | id、project_id、unit_id、标题 1–100 字、交汇说明 1–2000 字、sort；作者明确维护的共享事件，同一单元允许多个事件。 |
| SubplotCrossingParticipant | crossing_id＋subplot_id 唯一、真实 FK；一个事件有 2–20 条同作品、互不重复的参与支线。 |

- PlanningUnit 的章／计划 FK 均 `SET NULL`；POV 角色删除后 `SET NULL`；设定、标签删除只清对应关联，不删除规划单元或节点。
- unit、subplot、node 使用不复用的行身份（SQLite AUTOINCREMENT；PostgreSQL 序列不回退）。快照只引用稳定 unit，不把历史来源整数 ID 当身份。
- 所有引用必须属于同一作品；服务验证双向章／细纲链接与唯一性，导入入口重复验证。
- 关系选择器使用稳定时间线，返回 timeline_id、可空 relationship_id、两端角色、当前／目标章是否存在。隐藏或尚无当前载体的合法关系时间线可选择；组织成员和角色职业挂接仍是附属状态，不另建规划实体。
- 不为 live unit 另存角色集合：已建章读取 ChapterCharacter，未建章读取 ChapterPlan.character_ids；仅完全失去来源时使用 detached_character_ids 保留作者选择。
- 交汇直接挂稳定 unit，不要求先给每条参与支线创建普通节点，也不自动生成虚构节点。事件本身计作参与支线在该位置的推进，普通节点可以同时存在。事件参与集合只能整体校验后更新，不开放可使数量降为 1 的独立成员删除接口。
- CrossingParticipant 的 crossing FK 为 CASCADE，subplot FK 使用 NO ACTION、DEFERRABLE INITIALLY DEFERRED，并在人工删除支线前返回被事件引用的 409；不得因删一条支线而静默删除其他支线的共享事件。真实 PostgreSQL 16 验证发现默认立即检查会阻断多级整作品级联，因此将该外键检查延至事务末；修正后整作品删除通过，引用中的单支线仍被业务校验拒绝。
- 初版不新增章节号存储列；显示顺序从当前来源计算，不扩大 `CHAPTER_NO_REFS`。若实施发现必须增加数字章号，须先补该登记及反射测试。

## 3. 读模型、排序与来源身份

矩阵行是一个 `PlanningRow`，有 `unit_id | null`、`source: {kind: chapter|plan|detached, id}`、真实 chapter_id／plan_id、标题、分卷／大纲、显示位置、POV、标签和类型化设定引用。每个真实章／细纲最多显示一次；两者已绑定时以章节行表示。

- 没有持久 unit 的旧章节／细纲仍返回虚拟行，GET 不创建空记录。首次写入在锁内物化。
- 实际章节按 `(chapter_no, id)` 为主序；未建章细纲显示所属大纲和 seq，不把估算号冒充真实章号。列表提供“实际章节”和“未建章规划”分组，支线分布分别显示已建章轴与计划轴，不能把两条顺序混成已发生时间线。
- 未建章组按现有大纲 `(volume_no, sort, chapter_no, id)` 和细纲 seq 排序；大纲移动不改变实际章节顺序。
- detached 单元单列在待重新关联列表，排除所有正常章节统计与 AI 上下文。
- `PlanningReference` 是判别联合：character／world／organization／career 使用对应实体 id，relationship 使用明确 timeline_id；world 额外含 category。正文提及标记另外来自现有只读索引，仅角色／地点／物品可用，未支持类型返回 `tracking_supported=false`，不能伪造零次。
- `MatrixColumn` 单独区分 `{kind: setting, reference: PlanningReference}` 与 `{kind: subplot, subplot_id}`，不能把支线偷塞进设定 ID。设定格子返回 manual／POV／提及证据；支线格子返回可空 node 和 crossing 摘要引用。支持按设定、支线或自选混合列显示，因此支线在矩阵中本身可编辑。
- 行默认 50、最大 100；显示条目列单窗口最大 30，返回总数并可切换窗口／选择条目。源过滤、实体搜索、标签过滤都由类型化 query 表达。
- 多次查询组合数据时，前后读取 planning_revision；变化则有界重试或返回可重试冲突，不能把旧行配上新版本。提及扫描不持项目写锁。

## 4. 生命周期与兼容

所有 helper 只 flush，由既有外层事务统一提交。

| 操作 | 规划行为 |
| --- | --- |
| plan 建新章 | 保持 unit id，增加 chapter_id；角色带入章 cast，计划与章只显示一行。 |
| plan 复用已有一纲一章 | 仅一侧有规划时沿用该 unit；双方都有作者规划时先完整预检，返回 409 及冲突详情，由作者明确合并后重试；不得部分建章。 |
| 删除章，plan 还在 | 删除前把 cast 转存计划；同 unit 回到未建章。 |
| 删除 plan／大纲，章仍在 | 同 unit 留在实际章，解除 plan。 |
| 两种来源全部消失 | 保存名称及角色选择，unit 和节点转为待重新关联。 |
| 章改挂大纲并解除 plan | unit 随章，旧 plan 回到独立未建章状态，不复制支线节点。 |
| 重展开 | 保留的 plan 沿用 unit；被删除的 plan／空章先脱离并保留规划；新计划不能按同 ID 自动继承。 |
| 改章号／plan seq／大纲排序 | identity 不变，刷新分布；不重写节点文本中的历史描述。 |
| 删除设定／标签／支线 | 设定与标签清对应关联并提升 revision；无交汇引用时，明确告知节点数后删除支线及节点；有交汇引用则 409，先编辑或删除相关事件。 |
| 修改／移动／删除交汇 | 同事务更新事件及参与集合；移动只改 unit 绑定，保留事件 ID；删除事件不删除参与支线的独立节点。 |

### 角色事实统一

- 章 cast 为已建章权威，plan.character_ids 为未建章权威。已绑定时 PlanOut 返回章 cast，计划字段作为兼容镜像。
- 章 cast、已建章 plan 字段和矩阵三种写入口调用同一个 flush-only helper；不得把现有会 commit 的 set_cast 嵌入批量事务。
- 旧数据不在迁移时粗暴覆盖；首次相关写入同步镜像。建章首次合并保留原有取并集行为。恢复大纲快照不覆盖仍存在章节的 cast。

### 明确合并与重新关联

合并请求携带当前 planning_revision、来源两个 unit、最终 POV、集合型数据合并选择和同支线节点文本解决结果。目标 unit 作为 survivor：非冲突节点保留身份；同支线双节点由作者选择保留哪个 ID 并确认合并文本，另一节点才删除；交汇事件整体移动并保留 ID，不按标题自动去重。被合并 unit 脱离、清空并标记 retired，快照不得复活它或自动追到 survivor。缺少任何冲突解决项即 409，不推断用户选择。

合并时有真实章节的一侧优先作为 survivor，合并前不伪造章／细纲双向链接；待原建章流程真实建立链接后再绑定 plan。仅已有角色列表、不含新规划的两侧沿既有角色并集处理，无需把旧建章行为变成不必要的冲突。

显式重新关联或合并提升相关 unit 的 binding_epoch；自动建章承接、源删除导致脱离不改变这一作者意图代次。项目 revision 防并发，binding_epoch 用于跨时间的快照关联校验，二者不互相替代。

待关联单元重新挂到空目标时整体承接，角色与目标既有角色取并集；目标已含规划则使用同一合并流程。重新关联只接受实际存在的 chapter／plan，不能只传章号或名称。retired 单元不在待关联列表，不允许再次写入或绑定。

### 大纲快照与智能体撤销

- 新快照增加 `planning_bindings: [{unit_id, plan_id, binding_epoch}]`，仅保存绑定关系，不回滚后续 POV／节点／事件内容。恢复必须匹配意图代次且 unit 未 retired；作者改挂后即便新来源也被删除，旧快照仍不能重新认领它。
- 恢复前在项目写屏障内保存 unit 的当前章绑定。经快照 old→new plan 映射找到目标后，若该 plan 已挂到一个章，必须证明同一 unit 在恢复前仍绑定该章，才允许补 plan 绑定；否则保留原位置／待关联状态并警告。未建章目标可凭 unit＋binding_epoch 显式重连。不能仅信任 snapshot.links.chapter_id 裸整数或恢复后的 Chapter.plan_id，因为旧章删除后整数 ID 可能被新章复用。
- 老快照没有 planning_bindings：现有 live 章 unit 继续留在该章，不自动补 plan 绑定；失去来源的规划保持待关联。该保守策略只约束规划恢复，不扩大为全面重写既有大纲 links 语义。
- 智能体 effects 直接 SQL 路径纳入影响表和冲突检测，成功撤销后 planning_revision 单独递增；涉及绑定恢复时，binding_epoch 取不小于当前／被恢复值的下一代，不能写回旧值。作者之后改动过受影响规划时返回 409；旧 effect 不得抢占后来新增的规划。
- 覆盖入口清单见锚点研究：chapter_service、plan_service、cast_service、story_service、outline_service、outline_transfer_service、outline_snapshot_service、wizard_service、agent/effects。实现前逐个确认事务和项目屏障。

## 5. 接口契约

统一前缀 `/api/v1/projects/{project_id}/planning`；按现有 snake_case、Pydantic 入参／Out、前端 types.ts 镜像及 `{detail, code?, ...}` 错误包，不另设通用技能示例的协议。

| 接口 | 输入／输出要点 |
| --- | --- |
| GET /matrix | page、page_size、source_kind、volume_no、tag_id、column_mode=settings\|subplots\|custom、类型化 column_refs（最多 30）；返回 revision、rows、MatrixColumn[]、对应 cells、总数和缺失提示。 |
| GET /entries | kind、category、q、page／page_size；返回类型化可选设定，relationship 返回 timeline_id，不把组织／职业／关系冒充 MentionEntity。 |
| PATCH /units/batch | expected_revision、1–100 个 source 目标、可选 pov／tags／references 操作；返回新 revision 和受影响行。单行编辑复用同契约。 |
| POST /units/merge | expected_revision、两个 unit 和所有冲突解决值；原子合并，不自动重试。 |
| POST /units/{id}/binding | expected_revision、目标 source；目标非空冲突时返回可读详情。 |
| GET /detached | 待关联列表分页，含原来源名称。 |
| GET／POST／PATCH／DELETE /tags[/{id}] | 作品标签维护，变更请求均携带 expected_revision；删除返回影响数用于 UI 提示。 |
| GET／POST／PATCH／DELETE /subplots[/{id}] | 独立支线 CRUD；列表分页／筛选。 |
| PUT／DELETE /subplots/{id}/nodes/{source_kind}/{source_id} | expected_revision＋文本；PUT 明确替换该单元该支线的推进节点。 |
| GET／POST／PATCH／DELETE /crossings[/{id}] | 事件 CRUD；写入含 expected_revision、目标 source、标题／说明和完整 subplot_ids；PATCH 省略的字段不变，参与集合给出则整体替换且至少两条。列表支持 source／subplot 和分页。 |
| GET /weave | 已建章／计划轴、窗口、支线筛选及间隔阈值；返回节点、同单元推进集合、显式交汇及基于完整轴计算的间隔。 |
| GET /units/context-preview | source、scope=chapter|selection；返回规划投影及提示，不调用模型。已建章可显示真实目标位置；未建章仅预览规划指令。 |

批量 patch 示例契约：

```text
{ expected_revision,
  targets: [{ kind: "chapter"|"plan"|"detached", id }],
  pov?: { op: "set", character_id } | { op: "clear" },
  tags?: { op: "add"|"remove"|"replace", ids } | { op: "clear" },
  references?: [
    { kind: "character"|"world"|"organization"|"career",
      op: "add"|"remove"|"replace"|"clear", ids?: [...] }
    | { kind: "relationship", op: "add"|"remove"|"replace"|"clear", timeline_ids?: [...] }
  ] }
```

- 省略字段保持不变；clear 不带值；每种 reference kind 最多一个操作，替换／清空只影响该类型，不能改角色时顺手清掉组织。world 替换涵盖全部 world 分类，UI 显示具体影响与会移除的原条目。
- 全部校验后才写；跨作品引用 400，不可访问资源 404，无能力 403，过期 revision／来源改变／合并冲突 409，结构或数量错误 422。409 至少给 code、current_revision 和冲突目标，前端保留本地草稿并提供刷新重试。
- 取得真实项目写屏障后对 expected_revision 做数据库原子条件更新，修改与 revision 增量在一个事务内。不能只做 SELECT 再比较。
- 所有会改变源身份、角色权威值或规划编辑含义的旧写入口必须提升版本；纯正文变动只刷新提及，不无故导致规划编辑冲突。更名或设定内容更新在 AI job 开始时读取最新值，不依赖规划 revision 表达设定内容版本。
- 真删除关系时间线／角色会清理引用并提升规划版本；关系隐藏、普通关系状态变化仍可按章读取，不误当成 FK 已删除。snapshot 中 binding_epoch 与 unit 保存值一起进入备份，导入时保留匹配关系；它不是像 project planning_revision 一样可排除的运行时计数。
- 项目级版本首版有意较保守：两个标签页编辑不同章节也可能要求刷新，优先防止误关联和静默覆盖。
- 网络结果未知时先重读；不为简单编辑引入持久异步任务或假装支持未实现的 Idempotency-Key。

## 6. AI 上下文契约

### 读取与选择

生成 job 开始时一次读取真实 chapter 绑定的规划：POV、本章节点、交汇及手动引用。后续自动续写复用这份 messages；批量逐章读取，重试重新取当时规划。孤立、retired 和未建章的其他规划一律不进入目标章。

- 只注入目标章节点和交汇；事件独立查询，带标题、说明和完整参与支线，按事件 ID 只注入一次，不能按每条参与支线重复整段；没有普通节点的事件照常注入。AI 不读取 UI 的筛选条件，不因隐藏某列而丢失参与者，也不为补齐参与者读取其他章节点。不额外注入历史计划，历史仍来自现有正文／摘要／剧情记忆。
- 节点与交汇均是本章规划目标，不是既成事实；支线作者概览、终局和后续记录不注入。这个保证只针对新规划记录，不声称能识别自由文本中所有剧透，也不改现有全卷大纲策略。
- POV 加入有效角色选择并去重，但不隐式写入手动出场集合。本次明确的临时指令优先，其次本章 POV，再次风格的泛化视角；绑定角色不自行决定第一／第三人称。
- 全类型显式关联优先于普通参考条目保留；角色／world 走章初有效投影；组织／职业即使没有关联角色也能独立注入，按类型＋ID 与隐式关联去重。
- 组织简介与职业体系仍是当前静态档案，角色阶段与成员状态按已有时间线。引用不新增任何职业授予、阶段或组织成员事实。
- 显式人物关系在目标章初解析稳定时间线；exists=false 只给未生效的中性提示，不能拿最新关系类型、亲密度或说明补齐。两端角色加入只读参考集合以提供历史资料，并标为关系背景，不要求自动出场；不写 cast、不改变关系。显式与隐式关系按 timeline／规范人物对去重，且不递归扩展全书社交关系。

### 提示词与预算

- generate／revise／rewrite 新增独立锁定 `planning_context`，默认空串；渲染使用固定 Jinja 片段。旧已保存覆盖通过现有锁定块兜底补入，新保存覆盖遵循锁定校验。
- 新规划块包括 POV、节点／交汇及新增显式 world／组织／关系／职业参考资料，避免它们在旧覆盖裁剪普通 context 时消失。POV 的有效角色卡也在此块保障；同一投影从普通设定段中排除已在保护块输出的实体详情／关系，关系计算仍保留完整参考角色集合。单纯手动角色列表继续使用现有 cast 行为，不因只有标签就改变旧提示词。
- 局部 rewrite／expand／condense／deslop／continue 的规划块标明仅为章节背景约束；操作仍只作用于选区／光标，不能强制在片段完成全部节点。
- 整章 polish 继续保持原情节语义，不因新增规划强制改写故事。
- 先给规划文本分配有界预算，再从现有 context／setting_context 总预算预留同等字符。节点与交汇公平分配，保留 POV／节点／事件身份及事件参与名单；截断有明确标记，预算不足以保留最小身份时明确报错，不能静默忽略全部规划。
- 显式组织注入名称／类型／宗旨／说明；职业注入名称／类型／描述／条件／能力／规则／阶段列表，均纳入总预算和截断提示。没有显式引用时沿用旧格式与裁剪。
- 无新增规划／仅有标签时，实际 messages 与旧行为逐字兼容；只有交汇也算有规划。生成记录保存 unit、revision、scope、node IDs、crossing IDs及其参与支线 IDs、显式引用和截断信息，实际 messages 作为最终文本证据。
- 模板编辑器是样例预览；规划页预览是本次新增块，不称为完整章节最终提示词。未建章不能假装已有精确章初有效设定。

## 7. 前端与支线交织

详见 `research/ui-and-api-contract.md`。布局为作品“规划”页：矩阵／支线交织切换，待重新关联入口；统一筛选、当前选择、加载错误和只读状态。

- 自动提及、手动关联、POV 用不同符号和文本；不只依赖颜色。角色、world、组织、职业均可批量维护。
- 标签项目内可维护；批量操作先展示目标数与行为摘要。切换作品／授权变化／过滤移除目标时清掉过期选择，旧响应不能回灌。
- 支线节点从格子抽屉编辑，支线作者概览与节点推进文本分别标注 AI 用途。长列表按行／列窗口加载。
- 同一个单元内两条及以上支线出现普通节点或参与事件，标记“同章推进”或“同细纲推进”，仅代表位置并列，不自动宣称情节交汇。明确交汇另用事件标记与连接、标题及参与支线；筛选只显示部分参与支线时提示还有多少条隐藏，不能把多方事件改成两方事实。
- 交汇编辑允许从单元格／轨道新建、选择 2–20 条支线、填写说明，随后编辑、移动或删除；目标单元变更走统一 CAS，旧界面不能把事件写到已重建的来源。
- 间隔在完整、未分页且未按标签筛掉位置的同一轴上计算；相邻有效推进位置之间的单元数为内部空档，末次推进到轴末为尾部间隔；一个位置有多个节点／事件只算一次。已有章按实际存在章记录计数，未建章按细纲数计数，两种轴不混算。
- 阈值为视图选项（默认 5，范围 1–100），空档达到阈值高亮；不自动判定为剧情错误。没有任何推进的支线标为“未安排”，不虚构从第 1 章起的断档。
- 既有组件只复用样式／访问性模式；规划断档不复用提及的 gap_chapters 计算，它仅统计正文提及后的已写章数。

## 8. 备份、迁移与回滚

- 新增表（含交汇及参与集合）全部登记 project_transfer.RULES 为核心资料，推进 `ainovel.project` 至 v3；v1／v2 导入初始化空规划，不从伏笔或名称匹配捏造支线。
- planning_revision 属运行时版本，排除备份／效果 payload；导入新项目从 0 开始。detached_character_ids 和 snapshot planning_bindings 需正确 ID remap。
- binding_epoch 与 retired 作为规划身份语义保留；关系关联表在 setting_timelines 与 PlanningUnit 之后导入，timeline_id 不得被映射到 relationships。包含只有时间线、没有当前关系载体的备份往返用例。
- v3 导入后检查同项目、双向绑定、重复目标、节点归属、交汇参与数与 retired 单元无活来源／节点／事件；合法 orphan 保留。导出已建章 Plan 的角色镜像遵循章 cast 权威。
- 新规划表加入 agent effects 的间接变化捕获与撤销冲突保护；注册表顺序改动同时测试 effects。
- 只做增量迁移，不改正文。部署前备份数据库；若回滚应用功能，先停用新入口／AI 块并保留新表数据。删除新表的 downgrade 会丢规划，只能在明确导出并获得相应授权后执行，不作为自动失败恢复。

## 9. 主要风险与设计选择

1. 源删除／快照重建复用 ID：独立 unit、显式快照绑定、项目 revision 和真实写屏障共同处理。
2. 既有 cast 与 plan 双入口：统一权威 helper，覆盖所有旧入口；不引入第三套活跃角色列表。
3. 旧自定义模板和自动续写：锁定 planning_context＋首次消息快照，不能仅改默认模板。
4. 新关联不等于新角色事实；历史承诺限定已有时间线能力。
5. 大书矩阵有两维规模：服务端分页／窗口、明确总数；不新建全文扫描机制。
6. 本任务三段工作共享一个稳定规划模型与端到端验收，先按单任务分阶段执行；不把仅完成后端、UI 或提示词之一视为本项完成。
