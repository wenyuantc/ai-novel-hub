# Research: 持久化方案定稿审阅

- Query: 审阅父设计与两个子设计的迁移/格式版本链、引用句柄及 JSON 保护、旧格式、卡片显式引用映射、标签与规则 FK、旧写入口和版本单调性；最多报告 5 项实质问题。
- Scope: internal；只读审阅设计及必要源码，不改实现计划、manifest、产品或 Git。
- Date: 2026-11-05
- 复查范围: 仅复查原 3 项；本轮未扩大调研或读取实施计划。

## Findings

最新结论：**原 3 项均已在设计层闭环，当前无阻塞，也无原审阅建议待补。** 下列保留原问题及源码风险，供实施和验证对照；不表示产品代码已经修复或测试已经通过。

### 1. 已闭环：分类规则建立后的字段类型/适用范围保护

- 复查证据：`.trellis/tasks/11-05-typed-setting-fields/design.md:57` 已明确把分类规则引用纳入禁止换类型和适用范围交集保护，统一校验覆盖人工 PATCH、导入和 effects；`.trellis/tasks/11-05-setting-auto-categories/design.md:57` 明确 UPDATE/DELETE 均接入，零绑定/零事件仍受保护，合法改名/排序/停用及不破坏交集的范围调整保留。**原阻塞解除。**
- 原审阅所见：字段设计只在“已有绑定/事件”后禁止类型转换，对分类规则明确的保护仅是禁止硬删；分类设计 `:27` 要求条件引用 dropdown 定义且适用类型有交集，但原 `:57` 只扩展了定义/选项删除检查。
- 可复现矛盾：创建仅适用于角色的下拉定义 D（尚无条目绑定），创建角色分类 C 引用 D 的选项 O。随后把 D 改成 text，或把 D 的适用类型缩为仅 organization。按字段设计的绑定/事件检查可能通过，真实 FK 也不阻止 UPDATE，但已保存规则不再满足分类设计的合法性，读取会出现永久不匹配或类型错误。
- 最小补齐：分类子任务把规则依赖加入**定义更新**保护。已有分类规则引用时禁止改变其 dropdown 类型；缩小 applicable_types 若使任一现存分类规则失去交集则返回明确 409。复用同一规则图校验覆盖人工 PATCH、智能体恢复和备份导入，不自动删规则或改选项。适用范围仍可在不破坏现有规则时正常调整。
- 有序断言：只有规则、没有值/事件也会保护；合法改名/排序/停用不破坏旧规则；失败时 fields_revision/categories_revision 和所有行均不变。

### 2. 已闭环：两处排除目录版本及恢复高水位合同

- 复查证据：`.trellis/tasks/11-05-typed-setting-fields/design.md:118` 明确 fields_revision 同时从 PROJECT_RULE 导出列及 effects 独立项目快照/恢复集合排除，不写回 before 旧值，项目锁内从当前高水位递增且失败不增；`.trellis/tasks/11-05-setting-auto-categories/design.md:76` 对 fields_revision/categories_revision 两列明确同一合同，新作品使用默认起点。**原建议已落实。**
- 原审阅所见：两份设计仅要求恢复后递增且不能回退，未指出两处独立排除。现有 `backend/app/services/project_transfer/registry.py:136` 排除 planning_revision；**effects 不直接使用该 exclude**，而在 `backend/app/services/agent/effects.py:143` 单独排除 planning_revision。恢复按 before 整行写回，见 `effects.py:503`、`:550`。
- 风险：仅向 PROJECT_RULE.exclude 加 fields_revision/categories_revision 不能阻止 effects 捕获并还原它们；若先恢复旧值再 +1，可复用已经发给客户端的旧版本，形成 ABA，或让不相关目录改动导致项目行比较无谓冲突。
- 最小明确：两个 revision 不随作品备份复制，新作品按默认起点创建；同时从 effects 的项目列快照/恢复值中排除。撤销配置/标签/规则后，在项目锁内从**当前数据库高水位**递增受影响目录，不从 effect.before 起算。旧效果若夹带这些列，也不写回它们；按已定版本兼容策略拒绝或安全清理。
- 有序断言：手动修改→智能体修改→再次手动修改/其他目录修改→可合法撤销后，版本严格高于恢复前；整轮失败不递增；新导入项目不继承源项目 CAS 代次。此项不要求新增版本机制，使用已设计的两列即可。

### 3. 已闭环：新卡片包内同名实体 skip 不自动解析引用

- 复查证据：`.trellis/tasks/11-05-typed-setting-fields/design.md:117` 已明确 v4 包内被引用冲突实体必须重命名新建或显式确认复用具体目标；默认 skip 仅跳过内容，引用进入待映射清单；旧 char_map 的默认按名关联不代表引用授权，未解决整笔拒绝，v1–v3 旧关系行为保留。分类设计 `:75` 继续沿用 v4 显式映射，所以该保护覆盖 v5。**原建议已落实。**
- 原审阅所见：原字段设计确认包内 ref、包外映射及 skip 不覆写资料，但未规定被引用包内实体 skip 后的 ref。现有实现 `backend/app/services/card_service.py:391` 在默认 skip 时直接把源名称映射到目标同名 ID，`:426` 也回退按名称查目标；前端 `frontend/src/api/cards.ts:36` 默认将所有冲突设为 skip。
- 风险：新包里的 A 引用包内 ref B，目标作品已有同名 B；若沿用旧 default skip 的 char_map，A 的 typed reference 会自动绑定现有同名对象，即使新卡片本身采用局部 ref。包内 ref 解决的是文件内身份，不能证明目标项目同名实体就是该身份。
- 最小明确：对 v4/v5，给被引用的冲突实体选择“重命名新建”或**显式确认复用的具体目标**；普通 skip 若只是跳过内容，则相关 ref 保持未解决并进入现有显式映射流程，未解决整笔拒绝。v1–v3 的旧关系/成员按名行为可保留。预览展示最终包内 ref→新实体/已确认目标映射，不能把未操作过的默认 skip 当成新引用的显式确认。
- 有序断言：包内引用+同名冲突+默认 skip 不偷偷绑定；显式映射后正确绑定；重命名仍按 ref 保持引用；已有目标值/历史/标签不被覆盖。

## 首轮核对基准（本轮未扩查）

- `0035→0036`、project `v4→v5`、cards `v4→v5`、effects `v3→v4` 的顺序相容。父设计分类卡片目前仍有条件句，主会话已说明会统一为明确 v5，本报告不重复列为问题。来源：父设计 `:84`，字段设计 `:103`，分类设计 `:73`。
- 旧字段保留物理 ID/事件引用，不按同名自动合并；Text→JSON 要保存原字符串而不裁剪；`career_entry` 与 organization 适配现有 String(16)。来源：字段设计 `:9`、`:20`、`:30`、`:103`。
- 引用句柄真实目标 FK NO ACTION、project CASCADE，导入拓扑“实体→句柄→时间线→绑定→事件”，JSON 使用精确 handle_id/option_id remap，保留 inactive/孤儿引用；旧创建/删除、向导、拆书、卡片、effects 都已纳入。来源：字段设计 `:19`、`:59`、`:88`、`:104`。
- 标签是规范字符串数组并登记 json_plain；分类规则用 definition/option 真实 FK，保存时验证选项所属定义；不引入自由表达式 JSON。来源：分类设计 `:11`、`:13`、`:18`、`:27`。
- 卡片外部引用的“预览显式映射、未解决整笔拒绝”、世界观目标作为外部引用的范围选择、v5 仅带条目标签而不带分类规则均已明确。来源：字段设计 `:105`、`:106`，分类设计 `:75`。
- 旧格式按版本升级、核心引用缺失拒绝、旧效果整轮预检、软删恢复/整项目彻删及有损 downgrade 拒绝均已承接既有研究合同；未发现要求回放旧不完整日志或静默清空新值的条款。

## Files Found / Related Specs

- `.trellis/tasks/11-05-custom-setting-fields-categories/design.md`：父级版本与跨子任务合同。
- `.trellis/tasks/11-05-typed-setting-fields/design.md`：字段、句柄、时间线、卡片及 effects v3 合同。
- `.trellis/tasks/11-05-setting-auto-categories/design.md`：标签、规则 FK、v5 与 effects v4 合同。
- `backend/app/services/project_transfer/registry.py`：备份目录版本排除的现有先例。
- `backend/app/services/agent/effects.py`：独立的 effects 项目列选择与整行恢复。
- `backend/app/services/card_service.py`、`frontend/src/api/cards.ts`：当前默认 skip 按名关联的确切接点。
- `.trellis/spec/backend/project-transfer.md`、`agent.md`、`setting-progressions.md`、`story-entities.md`：兼容、撤销、历史与卡片的基准约束；前次研究已读取。

## External References / Versions

内部设计审阅，不依赖外部文档或新增技术建议。版本基准沿用 `persistence-and-writers.md` 中实际核实的仓库 head 0034、project v3、cards v3、effects v2。

## Caveats / Not Found

- 本轮仅复查原 3 项对应设计条款，没有读取或改写 implement/manifests，也未做产品实现或运行产品测试。
- 原问题与建议保留为实施对照，均已由明确设计合同关闭；不宣称代码已实现或验证通过。
- 原阻塞已解除，无新增问题或范围要求。
