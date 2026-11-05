# Research: 最终规划一致性审阅

- Query: 最终 PRD／design／implement 是否存在阻止实施的契约矛盾，重点 CAS、稳定身份、合并、交汇及双顺序轴。
- Scope: internal
- Date: 2026-11-05
- 审阅快照：主会话正在收敛 PRD／implement；未把过时“待确认交汇”文字或关系类型补充中的暂时遗漏当作问题。

## Findings

结论：总体模型、项目级原子 CAS、2–20 条参与支线、退役单元以及两条排序轴可以共存。发现的三处缺口已反馈主会话；主会话确认采用 binding_epoch、恢复前真实章锚点保护和 MatrixColumn union。以下保留证据与收敛方案，正式设计落盘后可解除这些规划阻塞。

### 1. [P1] 快照绑定缺少作者重新挂接的意图代次

**证据**：`design.md:22` 的 unit 无绑定代次；`:86` 的快照只有 `{unit_id, plan_id}`，同时承诺“若 unit 已被作者改挂，保留新位置”；早期锚点研究 `planning-anchor-design.md:82`–85 也没有覆盖二次脱离。

**反例**：快照 S 保存 U→P1；作者把 U 重新关联到 P2；之后 P2 被删除，U 再次成为 orphan。恢复 S 时，当前 `chapter_id = plan_id = null`，服务无法判断 U 从未改挂还是已经改挂后又脱离；仅用当前 FK 会把 U 接回作者已放弃的 P1。项目 planning_revision 只证明此次操作基于最新页面，不能恢复这段历史意图。

**确定建议**：给 unit 增加 `binding_epoch`，新快照 binding 同时保存 epoch。作者显式重新关联、合并／退役以及显式改变规划归属时递增；仅源被删除造成的自动脱离不改变其最后归属意图。恢复 binding 必须 epoch 匹配且 unit 非 retired，并继续校验当前活来源无冲突。不匹配时保留当前位置／orphan 并警告；不沿 retired unit 追到 survivor。该代次不随快照或撤销回退，v3 备份的 unit 与快照必须一致保留。节点文字编辑不递增 binding_epoch，保证恢复绑定不会撤销新的规划文本。

**验收补充**：S→改挂 P2→删除 P2→恢复 S 不重接；仅删除 P1→恢复 S 可以重接；退役 unit 的旧快照不能复活它。

### 2. [P1] 快照的 Chapter 链接同样可能复用整数 ID，不能用恢复后 plan 链接证明旧章身份

**证据**：`design.md:87` 允许凭恢复后的真实 Chapter.plan_id 关联存活章 unit；但 `backend/app/services/outline_snapshot_service.py:265`–291 实际只根据快照的 chapter_id 整数查询当前章并设置 plan_id，没有源代次。`backend/app/models/entities.py:479` 的 Chapter 没有 SQLite AUTOINCREMENT；同仓库 `backend/tests/test_outline_snapshots.py:145` 已验证 SQLite 删除后整数 ID 复用，并非理论场景。

**反例**：S 保存旧章 C7＋P1＋U；删 C7、删 P1，U 保留为 orphan；新章复用 C7 的 id。恢复 S 时现有代码把 P1 挂到新章；新规划恢复若直接信任这个“真实双向链接”，就可能把旧 U 的 POV／支线注入新章。binding_epoch 单独不能解决，因为作者未主动改挂 U。

**已收敛的保守防护（可接受）**：本次不扩大到所有 Chapter 增 UUID，也不重写既有大纲快照 links 的恢复语义。恢复开始时，在项目写锁内保存所有 live unit→当前 chapter 的真实 FK 关系；这一份恢复前证据不能在删除／重建后补取。恢复一个新 planning binding 时：

- 目标 plan 已被既有 snapshot.links 挂到实际 chapter：仅当**同一个 unit 在恢复前仍真实锚定该章**，且 binding_epoch 匹配、未 retired／未冲突时，才恢复 unit→plan；否则不绑定，不把它降格为 plan-only 以绕开检查，而是保留该 unit 在当前位置／orphan 并返回警告。
- 目标 plan 未建章：按明确的 unit id＋binding_epoch 恢复计划绑定，继续检查 unit 未挂到其他活来源。
- 老快照无 planning_bindings：仅保留现存 live 章 unit 的章绑定，不根据恢复后的 Chapter.plan_id 自动补 unit.plan_id；所有失联 unit 都不接裸 chapter_id。

该方案保障新增规划不会误注入复用 ID 的章，同时保留既有 outline.links 的兼容行为。它允许极端恢复场景下作者手动重新关联 orphan，代价明确且不会丢数据。重要的是不能“一边保留 plan 已挂新章，一边给旧 U 挂 plan-only”，否则规划行消失或后续建章又会错误认领。

**验收补充**：旧章删除→新章复用同 id→恢复旧快照，新章不能获得旧 U 的规划；原章始终存活时正常恢复；PG／SQLite 均跑该契约，其中 SQLite 必须实际构造复用。

### 3. [P2] 矩阵接口缺少支线列与交汇格子的类型表达

**证据**：PRD 要求章节与角色／地点／支线交叉；`research/ui-and-api-contract.md:11` 也写了“选择的设定或支线为列”。但 `design.md:49` 的类型只有设定实体，`:97` 的 matrix query 只有 entity_kind/entity_ids，`:98` entries 同样只有设定。现有设计只有 `/weave` 明确返回支线节点／交汇，矩阵 columns／cells 尚无法类型化表达支线。

**影响**：实现者可能只做设定矩阵，把支线全部放到交织页，或临时把 subplot 塞进设定 kind，破坏手动引用／AI 选择的边界。显式交汇参与某支线、但该支线没有普通节点时，矩阵单元格也不能简单以 node 是否存在判断“有推进”。

**确定建议**：固定独立的 `MatrixColumn` discriminated union，例如 `{kind: 'setting', entity_kind, entity_id}` 与 `{kind: 'subplot', subplot_id}`。matrix query 支持列模式或显式列选择（仍限制 30 列）；cells 对应返回 setting 关联／POV／提及状态，或 subplot 的 `node | null`、该 unit 的 `crossing_ids`、`has_progress`。`has_progress = 普通节点存在 OR 至少参与一个交汇`，事件保留完整参与集合；不要把 subplot 加入手动设定 reference kind。matrix 与 weave 复用同一推进投影。

**验收补充**：同一矩阵能选择支线列；只有交汇无普通节点的支线格子显示推进与事件；过滤隐藏部分参与支线仍保留完整交汇事实。

## 已确认自洽的设计

- 项目级 revision＋父项目物理写锁＋全批校验／单事务提交能保护虚拟 source 行；现有 `project_lifecycle.py:50` 与 `note_service.py:70` 提供可复用基础。多查询读的前后 revision 检查也已写明。
- 普通节点 `(subplot_id, unit_id)` 唯一，与同单元多个 crossing 不冲突；一个 crossing 可独立计作参与支线推进，无须虚构普通节点。
- 参与集合整体替换＋删除支线遇引用返回 409，避免事件从两方静默退化为一方。导入后验证参与数、整作品 CASCADE／NO ACTION 的 PG 验证都已明确。
- 合并选择 survivor、保留非冲突 node／crossing 身份、冲突节点显式解决、loser retired，符合“数据不丢且旧快照不复活”；需要补的是上面的绑定代次，不必引入自动追踪 survivor 的别名系统。
- 已建章按真实章记录、未建章按细纲 seq 分轴，间隔使用完整轴并去重同位置推进，语义清楚；不能再复用 mentions.gap_chapters。
- `agent/effects.py:294` 已有反射外键依赖检查，可阻止撤销创建章时悄悄清除后来加入的 unit；应扩展现有机制，避免另建重复撤销系统。

## 实施核查提示（不作为额外规划阻塞）

生命周期清单还应显式覆盖 `organization_service.py:279`、`career_service.py:365`、`relationship_service.py:822` 等专用删除入口，它们不走 story_service；删除后 typed join／POV 改变，必须同事务提升 revision。原设计“所有旧写入口”已经包含该要求，这里仅补具体证据，不要求继续穷尽整个仓库。

项目级 CAS 还需测试：批次末尾冲突时此前 source 物化没有留下空 unit；旧 effect 恢复源后版本不回退；crossing participant 替换失败、合并节点冲突和 NO ACTION 失败均回滚整个事务。

## Caveats / Not Found

- 只读规划一致性研究，未修改产品代码或其他文档，未执行测试。
- 此报告引用审阅时的行号；主会话并行收敛后行号可能变化，应按章节标题与语义定位。
- 关于新增关系设定类型的完整 API／AI／备份合同，由另一路研究补充；本报告不重复该范围。
