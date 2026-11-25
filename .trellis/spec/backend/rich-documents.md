# 场景富文档、作者区块与正文标记

> 来源任务：`11-07-rich-text-markers-sections`；基础正文与结构历史见 [场景合同](./scenes.md)，阅读与备份分别见 [阅读器](./reader.md)、[项目备份](./project-transfer.md)。

## 场景：富文档跨层契约

### 1. 范围 / 触发条件

修改场景正文、格式、作者区块、标记、场景拆并／历史、阅读导出、AI 上下文／改写／批量核对或备份时，须同时检查本契约。迁移 `0040`、文档保存、模型真实出站和提案采纳均跨数据库、服务、API 与 UI。

### 2. 签名

```text
GET|PUT /api/v1/projects/{pid}/chapters/{cid}/scenes/{sid}/document
POST    /api/v1/projects/{pid}/chapters/{cid}/scenes/{sid}/document/sections/{key}/convert
GET     /api/v1/projects/{pid}/markers?offset=0&limit=50&snapshot=...
POST    /api/v1/projects/{pid}/markers/{sid}/{key}/relink
DELETE  /api/v1/projects/{pid}/markers/{sid}/{key}?expected_content_revision=...
POST    /api/v1/projects/{pid}/chapters/{cid}/scenes/{sid}/sections/{key}/rewrite[/apply]
GET     /api/v1/tasks/{task_id}/rich-proposals/{token}?task_key=...&run_key=...
POST    /api/v1/tasks/{task_id}/rich-proposals/{token}/accept
GET     /api/v1/projects/{pid}/reader/chapters/{cid}
```

`SceneDocumentSave` = `{expected_content_revision, document, add_section_keys, delete_section_keys, restore_section_keys, add_marker_keys, delete_marker_keys, restore_marker_keys, mapped_section_keys, mapped_marker_keys}`；完整 `document` 必须显式包含 `{content, formatting, sections, retired_section_keys, markers}`。输出为 `{content_revision, planning_revision, document}`。`formatting` v1 含 `boundaries`、`inline`（bold／italic）及 `blocks`（heading／bullet_list／ordered_list／blockquote）。

`0040` 新增 `scene_documents`、`scene_sections`、`scene_markers`、`scene_review_proposals`、`scene_review_payload_chunks`、`scene_selection_receipts` 与 `scene_review_analysis_receipts`；`scene_sections.logical_key` / `scene_markers.logical_key` 是场景内稳定 UUID。备份 `ainovel.project` v8／`app_schema=0040`。

### 3. 契约

- `Scene.content` 仍是唯一正文；`Chapter.content` 是有序兼容投影。样式、作者区块和标记单独存储；区块文字不隐式插入正文、字数或阅读结果。客户端和后端交换的所有位置均为 Unicode **码点**，而非 JavaScript UTF-16 单元；不能落在 CRLF 两字符之间，包括拼接后跨场景接缝。区块有同锚点排序、独立标题／正文／格式以及默认 `include_in_ai=false`。
- 保存整体文档时，比较章 `expected_content_revision` 并在事务里校验显式增删、恢复与可靠映射；不得凭字段遗漏推断删除，或把退场身份冒充新建。删除标记／区块保留可验证身份；跨保存撤销通过 `restore_*_keys` 恢复，同 UUID 必须经来源血缘校验。活动标记可绑定正文或同场景活动区块；被编辑覆盖时成为 stale，保存有界旧摘录与位置，不能冒充仍有效。时间线分页续页必须携带第一页 snapshot；重定位还须绑定源／目标版本与所选文本 SHA-256。历史恢复不能新制造跨章同来源标记／区块双活；退场场景原有资料应保留，不应误判为活动场景中的双活。为兼容旧操作可能已产生的双活资料，备份仍可导入并完整保留，不能静默重编号或删除。
- 标记明确退为 `deleted` 墓碑时，清除当前行的摘录、草稿摘录、位置与截断标志，保留身份与来源；拆合时活动目标必须保留自身合法的标记元数据。操作前的历史快照仍可恢复原资料。此规则不追溯清洗已有旧墓碑或旧备份，也不保证带版本备份不含历史内容。
- 只有显式开启的区块才随普通 AI 上下文进入模型。关闭区块里的**选区**可在一次明确确认后发送：请求严格 `confirmed: true`，包含一次性 `request_id`、`start/end`、`selected_hash` 和内容／规划预期版本；发送前在实际出站边界再次校验权限、选择与版本并消费防重放收据，禁止将关闭区块标题或未选文字顺带加入。此前已发送的内容、摘要或记忆无法靠关闭开关撤回，不能承诺历史清除。
- 正文／区块选区改写均绑定具体预览、范围、原文及版本；格式无法继承先返回 `rich_document_confirmation_required`，只在作者确认后提交 `confirm_format_loss=true`。区块锚点等无法安全映射返回 `rich_document_unmapped`，不能把格式确认当作跳过映射检查。旧章级入口要求有效预览日志与内容、规划双版本才能确认格式损失。模型输出或请求未确定时不能盲目重复采纳。
- 无法自动可靠映射的整章／场景候选保持原稿并持久化为待核对提案，候选正文可分块存储；任务 UUID `task_key`、执行轮次 `run_key` 和随机 `token` 一起绑定。`ReviewAccept` 包含三者中的前两者、所有须确定锚点的 `{scene_id, section_key, anchor}` 以及显式 `confirm_format_loss`。同一选择的重复采纳应幂等，不同选择冲突；批量断点与分析收据必须和写入配合，恢复时不能重发模型或重复执行已记录的分析。正常成功路径只分析一次；若进程在记录 `running` 后、模型发送前后中断，无法确定外部调用是否发生，恢复时不自动重发，而是标记失败并允许人工核对／补分析，不能承诺故障下恰好执行一次外部调用。
- 阅读接口一次性读取同一章快照，返回 `{chapter, blocks}`；仅包含正文可见格式，无格式时 `blocks=null`。TXT 为原始正文；Markdown、EPUB 输出可读格式，不包含作者标记和未转换区块。Markdown 正文中的原始 Markdown／HTML 字符保留，第三方渲染解释不由本应用控制；不要承诺外部渲染器会自动净化。`convert` 是显式操作，在可无损映射时将单个区块转成正式正文并更新版本。转换是服务端原子提交，会清空当前编辑器的撤销栈；确认文案必须说明可通过章节版本恢复转换前的文档。
- 项目备份 v8 将格式、区块、标记及其活动／退场身份作为核心资料，导入时重映射数据库外键并验证引用、来源、坐标和投影；运行中提案、确认及分析收据不是作品备份。旧版备份补空结构，不能夹带未来版本的非空数据。`0040` 降级遇到非空富文档／运行记录或新结构历史必须拒绝，不能静默丢失。
- 前端编辑器投影保持码点、换行和区块身份双向往返；输入法组合尚未结束禁止保存／破坏选区的动作。折叠区块不能留下隐藏输入焦点；键盘操作和窄屏应可访问。异步预览、SSE、401/403、采纳及解锁必须绑定 actor、会话代次、目标和请求代次，旧请求不能清掉新会话或解锁新预览。
- 在途保存把已提交证据与之后输入的真实编辑事务分开。回执确认后，只在已保存基线上修正区块退场身份和标记派生资料，使用经过校验且不入撤销栈的事务同步。恢复草稿也按完整文档、原身份及合法证据重建保存请求，见[章编辑器自动保存和恢复](./scenes.md#章编辑器自动保存和恢复)。

### 4. 校验 / 错误矩阵

| 条件 | 行为 |
|---|---|
| 他人作品、场景或区块 | 404，不泄漏存在性 |
| 严格字段类型、UTF-8、范围、CRLF 中间或格式结构无效 | 422；不写入 |
| 内容／规划版本、时间线快照、选区或权限在操作前发生变化 | 409（如 `scene_context_changed`、`marker_timeline_changed`、`stale_section_selection`）或相应认证／授权错误；保留草稿 |
| 遗漏的增删／映射意图、无效来源或无法映射的区块锚点 | 409；不能静默覆盖或伪造身份 |
| 未经格式损失确认 | 409 `rich_document_confirmation_required`；不改变原文 |
| 一次性发送确认已消费 | 409 `section_confirmation_used`；须重新选择并确认 |
| 提案轮次、结构、锚点或采纳选择失效 | 409 `rich_review_task_changed`／`rich_review_invalid_positions` 等；保留原稿和提案 |

### 5. Good / Base / Bad

- Good：用户选中关闭区块的一段，确认一次；出站只包含该段及允许的上下文，预览后另行核对格式并采纳。
- Base：无格式旧场景读写不改变任何原字符；阅读 `blocks=null`，旧备份补空富文档结构。
- Bad：靠选区重复文本猜测标记身份，或遇到未映射锚点直接把模型结果覆盖原章。

### 6. 必需的测试

- `backend/tests/test_rich_document_{core,api,mapping,history,timeline,transfer,migration,render}.py`、`test_marker_lifecycle.py`：格式、码点／CRLF、显式意图、版本冲突、失效与重定位、结构历史、旧版升级、备份及导出隐私。
- `test_rich_document_{ai_context,section_ai,rewrite_confirmation,proposals,analysis,dispatch,postgresql}.py`：真实出站范围、一次性确认、权限／并发、格式二次确认、持久提案与批量断点；SQLite 与真实 PostgreSQL 均验证事务与约束。
- 前端 `richTextProjection.test.ts`、`richSectionProjection.test.ts`、`richSectionNode.test.ts`、`richMarkerMapping.test.ts`、`RichSectionRewriteModalModel.test.ts`、`RewritePanelModel.test.ts`、`sseIdentity.test.ts`：Unicode／大文档往返、区块交互、身份切换与晚到响应。真实浏览器另覆盖组合输入模拟、窄屏、跨保存撤销、双页冲突、实际下载和提案采纳；浏览器模拟 IME 不等同所有操作系统的原生输入法。

### 7. Wrong vs Correct

```typescript
// Wrong：把 JS UTF-16 位置直接当服务端码点，emoji 后的标记会错位。
const serverOffset = textarea.selectionStart

// Correct：按项目现有的码点／编辑器位置映射转换，并且检查 CRLF 边界。
// 入口见 frontend/src/components/scenes/richEditorPositions.ts。
```

```python
# Wrong: 只有格式确认也不能证明区块锚点可以被安全映射。
# Correct: 先验证待映射位置，再单独要求格式损失确认；任一失败均保留原稿。
```

## 章节工作台协作批注

### 适用范围

章节工作台在已保存的同一场景内发起讨论。正文版本、字数、写作统计和草稿的生命周期独立于讨论。普通阅读、书稿和模型请求继续读取各自的正文投影。

### 接口与数据

以 `/projects/{pid}/chapters/{cid}/annotations` 为根路径。GET 分页列讨论，POST 新建讨论及首条留言。`/{id}/messages` 的 GET 按 ID 升序读取全部发言（含首条），POST 追加回复。`/{id}/resolve` 的 POST 幂等标为已处理。列表返回 `{items,total}`。表为 `annotation_threads`、`annotation_messages`，从迁移 `0074` 起可用。

### 读写契约

发言提交 `body` 与 UUID `operation_id`。建讨论另需 `scene_id`、`expected_content_revision`、`start`、`end` 以及一份原句证据。支持浏览器摘要时使用 SHA-256 `selection_hash`，普通 HTTP 可提交 `selected_text` 由服务端逐字比对；两种证据选择其一。位置是正式正文内的 Unicode 码点半开区间。服务端生成署名和最多 240 码点的摘录，并在作品写锁内复核成员和版本。讨论的 `anchor_valid` 与 `resolved` 独立；位置失效时仍返回摘录和首条意见，`start/end` 均为 null。

### 校验和错误

- 非本书成员、已撤权、仅系列授权或仅有后台管理能力的用户没有讨论访问权。查看者和编辑者可发言，处理意见需要作者权限。
- 空白留言、超过 4000 码点的留言、空选区、越界、穿过作者区块或截断 CRLF 的选区均须拒绝。
- 正文版本或原句指纹过期时拒绝建讨论。前端保留留言草稿，等待用户重新核对。
- 相同用户重试同一次发言返回已保存的留言。相同操作 ID 被用于不同意见时返回冲突。

### 正常、空数据和失效场景

旧作品的讨论列表初始为空。文档保存可附 `prose_edits: [{start,end,insert}]`，每一步的位置相对于上一步完成后的正文，最多 256 步，插入总量最多 2,000,000 码点。服务端重放并核对最终正文。已知替换发生在批注之前时，用实际步骤平移选区并复核原句指纹。改动触及选区或缺少可靠映射时保留讨论，停止跳转。完整场景移章时按场景身份更新归属。

### 必测断言

用含重复句、换行和表情的场景证明能跳到第二次出现的原句。检查写入失败后讨论和位置一起回滚。撤权后再请求列表和回复应失去访问权限；其他成员仍可回看旧意见。单书和整库恢复分别验证有效、无效、已处理讨论及署名、留言顺序。将仅出现在留言里的短语放入样本，检查阅读、四种书稿和模型实际请求。

### 易错示例

原句出现在两处时，调用 `indexOf(excerpt)` 会误跳到第一处。工作台只使用已经过版本、范围和指纹校验的选区，无法定位时显示「原文位置已失效」。
