# Research: 编辑器草稿、提交快照与富文档恢复契约

- Query: 自动保存期间如何继续输入不丢字；需要持久化哪些富文档及编辑证据；怎样复用现有冲突、历史和 AI 边界。
- Scope: internal
- Date: 2026-11-17
- 产品范围：用户已选择 A，仅在页面已打开时支持断网继续写作；联网重新打开并完成鉴权后恢复本地草稿。本记录为设计建议，不代表实现或测试通过。

## Findings

### 1. 文件与现有模式

| 文件 | 职责／证据 |
| --- | --- |
| `frontend/src/components/scenes/sceneDraftController.ts:15–28,79–108` | 唯一场景草稿控制器；已保存 Scene、完整文档、最新草稿及冲突快照；映射／增删／恢复证据当前是私有内存集合。 |
| `frontend/src/components/scenes/sceneDraftController.ts:149–226` | 普通字段和富文档修改入口；保存期间直接拒绝新编辑；富文档每次修改验证区块／标记映射及显式身份变更。 |
| `frontend/src/components/scenes/sceneDraftController.ts:268–363` | 保存串行化、内容版本 CAS、写入结果未知保护；富文档与场景元数据分多次请求保存，部分成功须单独记账。 |
| `frontend/src/components/scenes/useSceneEditor.ts:11–43` | 账号、会话、作品、章节隔离；权限代次变化使旧请求失效，控制器激活／卸载。 |
| `frontend/src/components/scenes/RichSceneEditor.tsx:103–162` | 编辑器真实事务产生标记／区块映射证据；组合输入结束才把已校验正文发送给控制器。 |
| `frontend/src/components/scenes/RichSceneEditor.tsx:249–256` | 文档 prop 与当前投影相同则保留撤销栈；不同时 `EditorState.create` 整体重建，清空历史。 |
| `frontend/src/components/scenes/richSectionHistory.ts:7–30` | 已有 `historyRetiredSections(document, saved)` 纯函数修正墓碑；通过 `addToHistory=false` 的属性事务同步，不重建全文。 |
| `frontend/src/components/scenes/richSectionGuard.ts:46–58` | 已有 `rich-section-history-sync` 事务的逐步骤校验；不能通过标记一个任意 meta 绕过结构验证。 |
| `frontend/src/components/scenes/richSectionTransaction.ts:13–43` | 区块映射必须由 PM 事务证明；新建／移动／删除计划是受校验事务，不能凭相同 UUID 或相同文字猜映射。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:359–377,1108–1114,1188–1196` | 手动保存先场景后章标题，统一 `saving` 同时锁输入和导航；自动保存需要区分网络忙与禁止编辑。 |
| `frontend/src/components/scenes/SceneWritingPanel.tsx:468–501` | 绑定冲突组件和富编辑器；文档与已保存基线分别传入编辑器；`disabled` 目前同时控制正文与危险动作。 |
| `frontend/src/components/scenes/SceneConflictReview.tsx:7–27` | 现有比较仅展示纯正文差异、场景标题与摘要；复选框绑定远端内容／规划版本，未绑定本地草稿代次。 |
| `frontend/src/components/TextDiff.tsx:24–63` | 已有逐字差异、统计、主题适配及差异过大时的提示，可用于正文／标题／区块文字对比。 |

### 2. 不能直接给手动保存加防抖定时器

当前 `change` 和 `changeDocument` 在 `snapshot.saving` 为真时直接返回（`sceneDraftController.ts:149–156`），父页面又将保存态传入正文 `disabled`。同时成功回调会把 `documentDraft` 和普通草稿整体替换成请求时内容（`:335–362`）。若仅解除输入禁用，迟到成功响应就会吞掉保存期间的新输入。

`saveRich` 还会自动提交场景标题与摘要（`:348–358`）。如果本任务自动保存只覆盖章标题与正文，就必须显式区分自动保存字段组与手动“全部保存”，不能意外把独立摘要编辑也自动提交。未纳入自动保存的字段必须继续保留 dirty 和离开提示。

### 3. 推荐状态模型：基线 B、冻结提交 S、当前草稿 D

继续以 `SceneDraftController` 为场景内容唯一所有者；持久化仓库和调度器订阅它，不再维护一套可独立修改的正文 store。

每个目标维护：

- `base`：最后确认的服务端场景／富文档及内容版本，简称 B。
- `draft`：用户当前已完成且校验通过的编辑，简称 D；`editSeq` 每次有效编辑单调递增。
- `attempt`：不可变提交快照 S，包含 `attemptId`、目标／会话／授权代次、提交时 `editSeq`、预期版本、所提交字段和完整证据；`sending` 与 `unknown` 均保留该记录。
- `tailEvidence`：从 S 到当前 D 的后续有效事务证据，独立于 S 之前的证据；请求完成时不能清空整个证据集合。
- `fieldAck`：标题、完整文档、独立元数据的确认边界，避免文档已成功但后续标题失败时重发正文。

一次最多一个服务器写入链；单次请求不能被新输入取消后立即重发。保存期间接受普通文字和格式编辑；AI／恢复／结构变更等破坏性动作仍须等待保存排空并完成显式动作屏障。

成功回调首先验证身份、目标、请求代次和响应与 S 的一致性，然后只推进 B。若 D 的对应字段仍等于 S，标记该字段已保存；若 D 已变化，保留 D 并排入下一次保存，不能用服务端响应覆盖 D。本地草稿的删除／完成确认同样必须匹配 `draftId + editSeq`，不能按章 ID 整批删除。

章标题采用相同的 B/S/D 模型，并由后端标题冲突契约保护；不再以 `applyServerChapter` 无条件 `setTitle` 来响应自动保存。派生字段仍取服务端，标题输入只在其本地代次未变化或作者明确放弃时被替换。

### 4. 富文档证据跨保存的具体要求

三份快照只是必要条件；还需保留从旧基线经 S 到 D 的真实事务证据。`documentSave`（`sceneDraftController.ts:45–76`）依赖：

- `mappedMarkerKeys`、`mappedSectionKeys`；
- `explicitMarkerIntent`、`explicitSectionIntent`；
- 已确认退场的标记身份 `retiredMarkerKeys`；
- 真实历史事务恢复过的 `historyRestoredKeys`、`historyRestoredSectionKeys`；
- B 与 D 的完整文档，包括活动／退场区块身份。

建议提取可测试的“证据累积／拆分／重基线”纯逻辑，但不要存整套 PM 实例或把任意同 UUID 视为可靠映射。

冻结 S 时复制 B→S 证据，后续事务单独累积 S→D 证据。成功后，以已确认 S 为新 B，仅保留尾部证据；确定未提交的失败可以把两段证据合并回 B→D；未知结果保留两段，停止自动同步等待核对。新增／删除／恢复名单在构造下一次请求时针对新 B 的真实身份重新计算，不能直接复用上一次请求的名单。

必须专门处理以下竞态：

1. S 新建区块 K；请求在途时作者删除 K。旧 B 未包含 K，因此本地 D 可能没有 K 的墓碑；S 确认后，K 已经是服务端身份，D 必须补墓碑，下一次请求发送删除 K。
2. S 删除标记／区块；请求在途时作者撤销删除。S 确认后，下一次应发送 `restore_*_keys`，不能误判为新建。尾部必须保留真实 history 恢复证据。
3. S 保存普通文字；D 继续移动区块、编辑区块文字或标记。S 确认后仅推进基线；D 及其映射证据保留。
4. 已确认文档保存后，随后的 GET 或场景元数据 PATCH 失败。文档基线和尾部证据已推进；只核对尚未确认部分，不能退回旧版本重复提交。

墓碑修正优先复用 `historyRetiredSections`。若修正仅改变 `retired_section_keys`，通过现有受校验的 `rich-section-history-sync`／`addToHistory=false` 属性事务同步到编辑器；不要把仅墓碑不同的完整文档作为外部替换，否则 `RichSceneEditor.tsx:255–256` 会清空撤销栈。基线变化通知应在下一笔用户事务前完成同步；正在组合输入时延迟非必要的 editor 状态调整，组合提交后的 D 仍留作尾部。

不能通过临时禁用所有输入来回避这些竞态；也不应在这一任务中重写整个编辑器或取消原有证据校验。

### 5. 可恢复草稿的最小完整载荷

实际存储技术由主设计决定；序列化契约至少应包含：

| 字段组 | 必须保留的含义 |
| --- | --- |
| 身份与版本 | 本地 schema version、actor、作品／章／场景稳定 ID、独立草稿分支 ID、编辑序号、更新时间；不保存会话 token。 |
| 标题状态 | 服务端基线标题及其冲突令牌、当前章标题、提交中标题快照（如有）。 |
| 场景基线 | B 的内容／规划版本、标题／摘要及恢复比较必需字段；不把旧权限快照作为新会话写权限。 |
| 完整文档 | B 与 D 的 `content`、`formatting`、`sections`、`retired_section_keys`、`markers`；保持码点坐标和原始换行。 |
| 编辑证据 | B→D 的有效映射／意图／恢复集合与退场标记集合；保存进行中时保存 B→S、S→D 的分别对应关系。 |
| 未确认提交 | `attemptId`、S、S 采用的版本／字段／证据、各步骤确认结果，以及 `sending/unknown`；重启后所有遗留 sending 先视为 unknown。 |

不需要持久化光标或整个撤销栈才能满足首版恢复；重新打开后的编辑器从恢复文档建立新的撤销起点。恢复文档中的区块／标记及其待提交证据必须完整，不能把“不恢复撤销栈”理解为丢弃历史恢复意图。

必须从 `unknown` 解码本地数据，检查 schema、身份、整数版本、文档有效性、容量及证据集合成员关系，然后才能调用控制器恢复入口。`validSectionDocument` 和 `richDocumentCapacityProblem` 可以复用，但 TypeScript 类型断言不构成存储边界校验。损坏／未来 schema 记录不能静默清空或自动提交。

自动保存触发和本地持久化应基于有效编辑事件，而不是每次 `publish`；否则撤权清空 UI、加载态或响应回填会被错误记录为作者的新空稿。

### 6. 恢复、冲突与未知结果

联网打开后先完成账号与作品访问校验，再加载当前章／场景／富文档的一致快照，最后列出该账号该目标的草稿。恢复操作本身只安装 D，不发写请求；选择“恢复”后先判断 B 与当前服务端是否一致。

- 无遗留 attempt，且当前服务端与 B 的内容版本、完整文档及标题冲突令牌一致：可恢复原有证据后排入正常保存。
- 服务端等于遗留 S，且对应版本／步骤已满足预期：确认该次已经提交，推进 B，保留 D 的尾部，不能把 D 当作已保存。
- 服务端仍等于 B：对遗留请求进行核对后允许重新生成带原预期版本的新请求；迟到原请求与重试仍由服务端 CAS 互斥。没有可靠核对或服务端没有 CAS 的字段不自动重发。
- 服务端与 B、S 都不同：进入冲突，保留 B/S/D，禁止自动采纳远端版本号。

当前 `acknowledgeLatest` 在富文档元数据变化时会拒绝旧结构覆盖（`sceneDraftController.ts:387–404`）；这条保护不能为实现恢复而移除。其当前 `metadata(value)` 包含坐标／格式／区块／标记（仅清空 content），因此即使双方只是编辑正文，伴随格式／标记坐标变化也可能不能直接采用本地全文。这是安全限制，需要在 UI 给出完整比较和复制／导出保留入口，而不是笼统承诺所有富文档冲突都能一键覆盖。

优先复用 `SceneConflictReview` 和 `TextDiff`，但补充章标题、格式变化、按区块 key 的增删与文字变化、标记状态与归属变化。纯正文相同不代表完整文档相同。现有复选确认需同时绑定远端版本、所选本地草稿 ID 和 `editSeq`；用户继续输入或远端重新变化后重置确认。

对于不能验证结构映射的冲突，提供查看／复制或导出完整本地草稿、采用服务端并保留草稿记录的路径；不要声称已经自动合并。如果产品最终要求所有结构冲突也能一键采用本地完整版本，需要额外的受校验文档替换协议，不属于简单接通现有 `acknowledgeLatest`。

### 7. AI、切场景、恢复及身份边界

- `ChapterEditorPage.tsx:437–445` 的 `ensureSaved` 当前基于闭包 dirty 并执行一次 save。自动保存允许途中输入后，必须改为读取最新控制器状态并提供 `flushThroughCurrentEdit`：排空正在发送及后续草稿，确认最新目标／版本且无冲突，才继续 AI 或版本恢复。
- 对会启动 AI／更换正文／场景结构的显式动作，在短暂 flush 与最终请求快照之间设 action barrier，防止作者新输入穿过保存与 AI 提交的间隙；普通定时保存不启用该编辑锁。
- `followTask`（`:448–475`）已有 dirty 时保留草稿逻辑，新增调度器须在任务完成／外部变化时作废旧定时任务并刷新比较基线，不能因任务结束自动重放旧稿。
- `handleRestore`（`:602–610`）、局部改写应用（`:623–624`）以及区块 convert（`SceneWritingPanel.tsx:168–196`）会改变权威文档；需要统一使本次调度／确认代次失效，然后安装新服务端基线。
- 场景切换目前强制先保存（`SceneDraftController.select` 与 `ChapterEditorPage.tsx:1193`）。首版可保留该规则；断网时用户仍可通过现有明确离开动作退出，草稿须先完成本地提交，不能悄悄假定远端成功。
- 身份变化、撤权、只读仍按当前规则清除／隐藏编辑内存（`sceneDraftController.ts:111–138`），草稿持久化记录不能因此被“空草稿保存”覆盖。旧写入结果不能清理新账号／新分支记录；重新读取草稿必须重做访问校验。
- `beforeunload` 不能承担最后一次异步可靠保存；本地持久化应持续进行。现有离开弹窗“修改将丢失”（`:1286–1291`）需根据本地确认结果更新，笔记／排序／AI 预览仍各自按原有未持久化语义提示。

### 8. 建议文件与测试映射

| 实施点 | 修改／新增范围 | 必验行为 |
| --- | --- | --- |
| B/S/D 与字段组确认 | `sceneDraftController.ts`、新纯状态／证据辅助模块 | 保存期间连续输入；单请求串行；场景／章标题／摘要独立 dirty；部分成功不回滚。 |
| 证据重基线与编辑器同步 | `RichSceneEditor.tsx`、`richSectionHistory.ts`、必要时 `richSectionGuard.ts` | 在途新建后删除、删除后撤销、区块文字＋标记映射、跨保存撤销栈仍可用、组合输入时迟到 ACK。 |
| 自动调度与章标题 | `ChapterEditorPage.tsx`、`useSceneEditor.ts`、新调度控制器／hook | 防抖、手动 flush、重试、标题旧响应隔离、AI／恢复动作屏障、卸载清理。 |
| 草稿恢复及比较 | 新恢复面板／存储适配层、`SceneConflictReview.tsx`、`SceneWritingPanel.tsx` | 多份草稿选择、富文档比较、丢失目标、未知结果、跨账号与多标签页分支隔离。 |

已有回归应扩展而不是复制：

- `sceneDraftController.test.ts`：409、外部刷新、迟到响应、授权变化、失效深链、网络结果未知。
- `sceneRichDraftController.test.ts`：完整文档 CAS、真实映射、显式增删、跨保存恢复、容量限制、组合输入；新增 in-flight tail 证据／墓碑竞态。
- `richCompositionHistory.test.ts`、`richSectionTransaction.test.ts`、`richSectionOperations.test.ts`、`richSectionMarkerMapping.test.ts`：真实 PM 事务与历史语义。
- 新草稿解码／恢复／调度测试应使用受控 deferred promises、fake timers 和独立存储适配；验证实际状态与请求载荷，不只断言定时器被调用。
- 真实浏览器验收：人工输入＋网络延迟下保存不中断、断网输入后关闭且联网重开恢复、多标签页独立草稿、格式／作者区块／标记恢复、在途 ACK 时撤销／重做、375px 及键盘操作。模拟组合输入不等于所有系统原生 IME 已验证。

已确认存在的定向命令（在 `frontend/` 执行）：

```bash
npm test -- src/components/scenes/sceneDraftController.test.ts src/components/scenes/sceneRichDraftController.test.ts src/components/scenes/richCompositionHistory.test.ts src/components/scenes/richSectionTransaction.test.ts src/components/scenes/richSectionOperations.test.ts src/components/scenes/richSectionMarkerMapping.test.ts
```

最终实现门禁仍为 `npm run lint`、`npm run build`、`npm test`；本次调研未运行这些命令。

## Related Specs

- `.trellis/spec/backend/rich-documents.md`：码点、完整文档、身份／映射／恢复证据、组合输入及权限边界。
- `.trellis/spec/backend/scenes.md`：正文权威、历史边界、场景身份、AI 快照与显式冲突核对。
- `.trellis/spec/frontend/state-management.md`：单一状态所有者、派生字段来自服务端、旧响应与账号切换保护。
- `.trellis/spec/frontend/quality-guidelines.md`：接口层、中文提示、真实浏览器验收和 README 同步。

## External References

未做外部浏览器存储或第三方库调研；本主题以仓库当前 Tiptap／ProseMirror 集成源码为证据，存储 API 的官方文档由主会话研究。

## Caveats / Not Found

- 未发现可以直接复用的持久化正文草稿协议或富文档全量冲突替换接口；现有比较 UI 不足以展示完整差异。
- 当前撤权测试明确要求清除编辑内存；持久化策略必须区分“隐藏编辑器”与“删除未同步草稿”，不得靠取消这些测试绕过授权语义。
- 富文档 metadata 改变时保留本地全文的范围是主要技术边界；不允许猜映射。若实施计划选用只读查看／导出保留，应在最终规划摘要明确该限制。
- 不保证崩溃前尚未完成的中文候选组合文字可恢复；应保证最后一笔已提交且持久化成功的完整文档，且 UI 不提前宣称未完成存储成功。
