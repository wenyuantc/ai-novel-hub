# Research: 类型化字段规划一致性审阅

- Query: 父任务需求／总体设计与字段子任务需求／技术设计是否存在会影响既有 API、并发或历史保护的可执行合同缺口？
- Scope: internal；首次只读审阅四份规划文档并核对针对性源码；复查仅检查原三项修订，补读两子任务 implement 及分类 design 的对应段落。不修改规划、产品代码或规范。
- Date: 2026-11-05

## Findings

最新审阅结论：**通过，原三项全部关闭；本次复查范围内无阻塞项。**本结论只确认规划合同及相应验证计划已补齐，不表示产品代码或测试已经完成，也不替代最终用户批准。

已一致的核心合同：`text/line/dropdown/reference`；`organization/career_entry` 与原挂职／成员分离；内部 `reference_handle_id`；共享绑定 `label=NULL`；空槽读取无写、首次事件原子 ensure-binding；同一 FieldSpec 回放；当前显示名且不递归引用正文。以下不提出新产品范围或名称时间线。

### 复查结果（2026-11-05）

| 原项 | 最新状态 | 已核实的闭合证据 |
| --- | --- | --- |
| 1：旧事件路由的版本名／codec 合并 | 已关闭 | 字段 `design.md:81-84` 明确保留旧 expected_revision、共享事件必须目录 CAS、PATCH 在内部 canonical 命令上合并、公共值只编码一次、所有 DTO 统一解码。字段 `implement.md:57` 覆盖旧请求、reference 仅改 note／章锚、显式 null、缺失／过期目录版本。 |
| 2：读数据与目录版本一致性 | 已关闭 | 字段 `design.md:88` 明确读前后 fields_revision／timeline revision 核对，变化时冲突或干净重试；字段 `implement.md:58` 加并发屏障回归。分类 `design.md:41`、`implement.md:31` 明确跨全部批次使用一个只读一致快照，并验证中途编辑不能混合结果。 |
| 3：仅被规则使用的定义类型保护 | 已关闭 | 字段 `design.md:57` 已将分类规则纳入类型转换及范围缩减保护，统一覆盖 UPDATE／DELETE、人工／导入／effects；分类 `design.md:57` 和 `implement.md:43` 明确零绑定／零事件但已有规则时不能转类型，并要求失败不改行或版本。 |

上表的 `字段` 路径前缀为 `.trellis/tasks/archive/2026-11/11-05-typed-setting-fields/`，`分类` 路径前缀为 `.trellis/tasks/archive/2026-11/11-05-setting-auto-categories/`。复查没有新增审阅项。

以下保留首次发现与建议作为修订依据；其中“缺口”描述对应修订前的文档，不代表当前仍未解决。

### 1. 原高优先级：旧事件路由的 typed PATCH 版本与 codec 边界（已关闭）

证据：

- `.trellis/tasks/archive/2026-11/11-05-typed-setting-fields/design.md:42`、`:44` 明确公共引用为 EntityRef、存储引用为 reference_handle_id。
- 同文 `:75` 规定新客户端继续用旧时间线路径修改／删除既有事件，`:77` 又规定值／事件请求带 `expected_timeline_revision` 和 `expected_fields_revision`，但旧入口继续旧 timeline CAS。
- `backend/app/schemas/progression.py:118`、`:146`、`:148` 的旧 CREATE／PATCH 使用 `expected_revision`，且 extra=forbid。
- `backend/app/services/setting_progression_service.py:957-974` 先从 ORM 取得原始 value，再合并 PATCH，并交回公共 ProgressionCreate；`:851` 的事件输出也直接序列化 ORM value。

缺口及影响：设计没有明确旧事件路径究竟接收哪个 revision 参数、新共享事件是否强制目录 CAS，以及“只改备注／章锚、未传 value”时内部 handle 如何穿过公共 DTO。若仅在新建入口套 codec，旧 PATCH 就可能把内部对象当公共 EntityRef 再编码、拒绝合法的备注修改，或让事件详情返回内部结构。若把所有旧请求统一改成新 revision 名，又会破坏承诺保留的旧 API。

建议补成可执行合同：

1. 旧路由保留 `expected_revision`；对 definition-bound／新类型事件扩展并强制校验 `expected_fields_revision`，legacy／内置旧请求仍按原合同工作。新复合入口的 `expected_timeline_revision` 映射到同一内部 CAS，不要求旧客户端改参数名。
2. PATCH 必须保留 value 的“未提供”状态；选择一种明确算法：先将既有 canonical value 解码成公共值再合并、统一编码一次，或在专用内部命令上合并 canonical 值。不能把两种命名空间混入同一未区分的 value。
3. 事件列表／详情／变更返回和基础值输出全部经过同一公共投影，不只 effective 输出解码。

最小验证：旧文本 CREATE/PATCH 原请求仍成功；reference 事件仅改 note、仅改章锚、显式 null 三种路径均正确；过期 fields_revision 修改共享事件返回 409，事件及 timeline revision 一起不变。

### 2. 原高优先级：目录数据与 fields_revision 的读端一致性（已关闭）

证据：

- `.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/prd.md:34`、`:56` 要求并发保护与不覆盖竞争修改。
- `.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/design.md:75` 和子任务 `design.md:22/:77` 规定字段目录 CAS；子任务 `design.md:68/:71` 规定读取返回 fields_revision。
- 子任务 `design.md:83` 采用批量定义／选项／目标预取，但没有规定版本读取顺序、一致快照或读取前后核对。
- 可复用的本项目模式在 `backend/app/services/planning_context_service.py:474-478`、`:517-520`：读取前取得 revision，完成多次查询后再次核对，避免把旧资料配上新版本。

缺口及影响：仅规定写 CAS 不足以保证版本有效。如果一次目录响应先读旧定义／选项，再读到并发操作后的新 fields_revision，客户端会拿旧资料配合当前版本写入，绕过本应阻止覆盖的 CAS。这是读响应身份与版本绑定问题，与前端是否废弃迟到响应不同。

建议补成可执行合同：目录／条目字段元数据读取须先获取 fields_revision，并在最后确认未变化，或明确使用同一数据库一致快照；变化时返回带 scope 的冲突或在独立干净读取中重试。禁止把分次查询得到的旧元数据直接配上最后读到的新 revision。相关上下文若复用这些预取结果，也保持一次读取内的字段图一致。

最小验证：用可控屏障在“读完定义、读完响应版本”之间更新定义／选项；响应只能是一致版本的数据，或明确冲突，不能出现旧标签／选项＋新 fields_revision。

### 3. 原中优先级：仅被分类规则使用的定义类型转换保护（已关闭）

证据：

- `.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/design.md:47` 规定分类使用共享下拉字段的稳定选项，`:57` 要求已使用定义类型不原地转换。
- `.trellis/tasks/archive/2026-11/11-05-typed-setting-fields/design.md:57` 的类型转换禁止条件只列“已有绑定／事件”；`:58` 虽列出后续分类规则引用，但限定于硬删保护。
- 字段子任务 `prd.md:24/:40`、`design.md:112` 要向分类交接稳定定义／选项合同。

可发生场景：作者先配置一个下拉定义和选项、创建使用它的分类规则，但尚无条目填写，因此没有 binding／事件。按子设计目前写明的类型转换条件，它仍可改为 line；分类规则的 option_id 随即失去合法的下拉语义。保留主键并不能解决类型改变。

建议补充：将“规则引用”同时纳入定义类型变更的依赖保护，而不只纳入 DELETE；字段目录修改应有统一依赖检查，分类子任务接入后扩展该检查。若字段子任务先只识别值／事件，可先固定这一扩展合同，并把接入及回归列为分类交接门禁，不能把“无绑定”当作未来可转换的充分条件。

最小验证：零条目值、零事件但已有分类规则引用的 dropdown 定义不能转换类型；删除规则后再按其他既有转换限制处理。此项不要求新增规则时间线或分类能力。

## Files Found / Code Patterns

| 路径 | 本次用途 |
| --- | --- |
| `.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/prd.md` | 来源需求、兼容及并发验收。 |
| `.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/design.md` | 总体类型、引用、CAS和分类依赖合同。 |
| `.trellis/tasks/archive/2026-11/11-05-typed-setting-fields/prd.md` | 字段子任务范围及分类交接要求。 |
| `.trellis/tasks/archive/2026-11/11-05-typed-setting-fields/design.md` | 冻结类型、懒绑定、codec、旧路由与版本合同。 |
| `.trellis/tasks/archive/2026-11/11-05-typed-setting-fields/implement.md:57/:58` | 原第 1／2 项的定向回归与并发屏障验证计划。 |
| `.trellis/tasks/archive/2026-11/11-05-setting-auto-categories/design.md:41/:57` | 跨批一致快照及规则对定义 UPDATE 的依赖保护。 |
| `.trellis/tasks/archive/2026-11/11-05-setting-auto-categories/implement.md:31/:43` | 跨批编辑屏障、零绑定但规则引用的类型转换回归计划。 |
| `backend/app/schemas/progression.py:113` | 旧请求名与 extra=forbid。 |
| `backend/app/services/setting_progression_service.py:844/:951` | ORM 原值输出、旧 PATCH 合并及公共模型复用。 |
| `backend/app/services/planning_context_service.py:474/:517` | 多查询读前／读后 revision 一致性模式。 |

## Related Specs

- `.trellis/spec/backend/setting-progressions.md`：旧 API、稳定字段身份、同事务版本及只读求值。
- `.trellis/spec/backend/planning.md:56`：多查询上下文读取的版本一致性。
- `.trellis/spec/guides/cross-layer-thinking-guide.md`：JSON／事件解码只有一个所有者，边界类型与派生状态必须对应事实身份。

## External References

本次为内部规划一致性审阅，没有新增外部技术结论或依赖版本建议；引用类型及 JSON 机制的官方资料已在 `typed-field-timeline.md` 记录。

## Caveats / Not Found

- 首次审阅限于指定四份文档；复查只读取原三项对应的设计修订与执行计划段落，没有扩大检查或修改其他文件。
- 原三项均已由明确合同及定向验收计划闭合，未遗留技术阻塞；首次记录仅供追溯。
- 未实现产品代码或运行测试；文中最小验证已进入计划，不表示测试已经通过。后续实现仍需执行门禁及实际消息验证。
