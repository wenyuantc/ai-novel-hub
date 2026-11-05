# Research: 实施前计划契约核对

- Query：仅核对父任务及两子任务计划中阻塞实施的权限、缓存、DTO、迁移与共享文件边界错误。
- Scope：internal；只读计划，不修改产品或计划文件，不扩大需求。
- Date：2026-11-05
- Reviewed：父任务 `prd.md`、`design.md`、`implement.md`；`11-05-project-notes` 与 `11-05-mention-index-navigation` 的对应三份计划。

## Findings

以下 3 项需要由主会话统一权威合同后交给并行工作者，均可直接修正规划，不需要扩大用户范围。

### 1. Summary API 缺少已经承诺的精确条目与分类筛选

- 证据：父 `prd.md:22` 的 R5 要求类型/条目/章节范围筛选；父 `design.md:65` 同样承诺该 UI。父 `design.md:49` 的 `GET /mentions/summary` 只有 `entity_kind?, q?`，没有 entity_id 或 category。
- 问题：EntityRef.kind 只有 character/world，不能分别选择地点和物品；q 是文本条件，不能区分同名不同 ID 的条目。若前端在分页结果上自行过滤，会遗漏后续页目标，并使 total/分页错误。
- 建议最小修正：summary 增加 `entity_id?` 与 `category?: character|place|item`，明确 entity_id 需要 entity_kind、冲突参数返回 422；类型/条目过滤先于条目分页。若统一接口选择其他参数名称，也必须同时固定 TS 与 Pydantic，禁止 UI 后过滤冒充全量筛选。

### 2. 未提及间隔同时定义为全作品与筛选区间

- 证据：父 `design.md:66` 前半句为“gap_chapters = 全作品按当前章序”，后半句又称“筛选区间决定展示计数和间隔的统计范围”。
- 问题：选取早期章节区间时，同一条目在两种算法下会产生不同 gap，last_chapter_no 与页面计数也可能跨越不同范围，M-B 与 M-F 无法据此写一致验收。
- 建议最小修正：写出唯一口径。例如统一在请求章节区间内计算 mention_count、last_chapter_no、gap_chapters，并标注“所选范围”；不指定范围时才是全书。若选择全书 gap，也应明确不受章节筛选影响，并在 UI 标明，不能保留两种互相冲突的描述。

### 3. 无权聊天计数的省略语义与必填 DTO 冲突

- 证据：父 `design.md`“四类来源”段要求管理员管理他人作品时完全剔除聊天来源，“计数也不返回”；但 `design.md:54` 定义 `source_counts: Record<SourceType,int>`，要求 chapter/chat/note/knowledge 四键齐全。
- 问题：后端按省略 chat 实现时，不符合完整 Record 的前端假设；若 Pydantic 定义固定四键，也会在返回时校验失败。两边不能各自从矛盾文案推断。
- 建议最小修正：保留固定 DTO 时明确无权 chat 不扫描、不聚合，`source_counts.chat` 始终为 0，前端依据已有作品归属能力隐藏聊天入口；这个 0 是固定占位，不是他人会话真实计数。另一种可行合同是统一改为 Partial/可缺键并在前端处理缺省，但需要同一次修改合同。

## Reviewed Areas Without Additional Blocking Findings

- 四源先验证作品生命周期，再对 chat 额外检查当前 actor 会话归属；原文来源枚举发生在缓存使用前，符合已有权限边界。
- 当前设计已选择有界进程内 LRU 而非持久化副本；缓存键包含项目、来源身份、原文 hash 与词典 hash，章号/标题查询时取当前值。该设计没有要求写入口同步缓存的正确性漏洞；首扫 IO/CPU 成本仍需按既定性能验收验证。
- 笔记原文进入项目备份、别名 JSON 注册、旧备份默认兼容，0032→0033 与最终 APP_SCHEMA 已固定。
- N 先完成 model/API/备份注册并通知后，M-B 才接手共享文件；N 的 NotesPanel 完成后由 M-F 集成定位。职责顺序明确，本次未发现额外共享文件冲突。

## External References

无；本轮只核对指定的已落盘计划。

## Caveats / Not Found

未运行测试，未审查产品实现，不能据此宣称功能或性能已通过。除上述会使并行实现产生不同 DTO/统计结果的合同缺口外，不报告一般改进建议。
