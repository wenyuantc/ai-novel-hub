# 可阅读文档组合导出

## 1. 范围／触发条件

新增或修改站内 Word／Markdown／TXT 的选择、正文投影、资料内容、聊天安全核验、下载或 EPUB 兼容时遵循本契约。可阅读文档不是可回导的项目备份；旧备份与 EPUB 规则见 [project-transfer.md](./project-transfer.md)。

## 2. 签名

```text
POST /api/v1/projects/{project_id}/export-document
{format: "docx"|"md"|"txt",
 story: {scope: "all"|"selected"|"none", unit: "chapter"|"scene",
         content: "prose"|"summary"|"both", ids: [严格正整数]},
 materials: {settings: [{kind: "character"|"world"|"organization"|"career", id}],
             conversation_ids: [严格正整数], note_ids: [严格正整数]}}
```

后端 `schemas/export.py`、`services/document_export/{service,story,materials,model,renderers}.py`；`services/agent/export_reads.py` 的 `capture_document_access(db,user_id,project_id,conversation_ids)` 和 `verify_document_access(db,capture)`；`personal_projection.project_document_payload_in_snapshot(...)` 仅供调用方已授权的同一快照使用。前端 `api/exports.ts`、`components/ExportModal.tsx`、`components/export/`。

`story` 省略时默认全部章节正文，`materials` 省略为空。全部／不包含正文时 ids 必须为空；自选必须非空。ID 重复、布尔冒充整数、未知字段、null 或非法组合拒绝。旧 `GET /projects/{pid}/export` 的格式、参数和空章节选择语义不变，开放 API 也不新增格式。

## 3. 契约

- 作品作者与已接受邀请的协作者可导出；仅有管理员人工读取权限不等于导出权限。数据读取不写库、不调用 AI，只输出已保存内容。
- 章按 chapter_no／id 排序，场景按所属章及 sort／id 排序。场景模式每景独立输出正文，不自动附带整章摘要或前景连接符；正文与排版必须同一次 SQL 读取并验证章投影，不能混搭坐标。生成中的章在 all 模式跳过并提示，自选时 409。
- 章摘要取最新 ChapterAnalysis；过期判断复用 `analysis_fingerprints`，包含开启作者区块对分析来源的影响，但绝不把作者区块正文塞入文件。场景取已保存 Scene.summary，无过期判断。缺摘要显示「暂无摘要」，不回退细纲或自动生成。
- 设定只读取所选的本书四类当前实体和 typed／legacy 自定义字段，选项及引用转名称；关系／成员／职业挂接显示名称与简要信息，不递归展开未选条目。系列只读已采用本书投影；当前值可能包含计划章节事件。笔记读完整正文，不能把列表 excerpt 当正文。
- 聊天仅限当前作品及实际会话创建者，viewer 可只读本人旧会话；只输出已保存 user／assistant 消息。工具消息、参数、任务结构、模型密钥、Skill 快照均不可进入文件。
- 捕获时保留原 `AuthenticatedActor`、作品创建身份／last_deleted_at、原协作 invitation_id／revision、所选完整会话／消息证据及原始待投影 payload。渲染后在新快照复核所有证据和私有引用，再以原始 payload 重新投影并比较首轮安全结果；变化时丢弃文件。不能用当前 token 重建 actor、使用缩水消息代替 proofs、以多次独立 READ COMMITTED 查询声称同快照，或在无聊天时跳过授权复核。
- 最终核验 PG 使用 REPEATABLE READ；SQLite 在首个 SELECT 前显式 BEGIN；各批有界并在一个短只读事务中完成，释放后才交付。导出不持数据库事务等待 DOCX 渲染；最终快照开始后发生的变更不保证撤回已交付文件。
- DOCX 用 python-docx 生成，并将标题、段落、粗斜体、列表和引用输出为文档结构；TXT 为纯文本，MD 正文沿用阅读格式投影。同步渲染在线程中只接收纯值节点。作者区块、标记、设定历史、知识库及未保存草稿不隐式导出。
- 附件 `Cache-Control: no-store`；文件名使用 Content-Disposition 的 UTF-8 扩展名，`X-Export-Warnings` 百分号编码。前端 scopeVersion、actor、弹窗／请求代次和 AbortSignal 需在 blob 读取完成后、创建 URL 前再判断。`RequestOptions.isCurrent` 为可选局部守卫，失效时旧 401／403 不触发新会话的全局副作用。

## 4. 校验与错误

| 情况 | 结果 |
| --- | --- |
| schema、ID 或选择不合法 | 422，不产生文件。 |
| 完全未选或没有可导出内容 | 400，明确提示。 |
| 他人作品／场景／会话、删除或退场条目 | 404，不泄漏资源详情。 |
| 自选章生成中、读到的来源或授权事实变化 | 409 或既有 401／404，丢弃附件，不自动重试。 |
| DOCX 不支持的 XML 控制字符 | 400，提示来源，不静默删字。 |
| 弹窗关闭、身份／作品变化后的晚到响应 | 不下载、不报成功、不清空新弹窗；错误不修改新会话。 |

## 5. 正例／基准／反例

- 正例：选第 1 章的第 2 场景、一个人物和本人一篇笔记；文档只出现这些正文／资料，引用对象仅以关系名称出现。
- 基准：正文范围选择 none 且勾选一篇笔记，得到仅有作品信息和该笔记的文档；选择为空时不能默认为全书。
- 反例：作者选了协作者的会话 ID、导出时被撤权后重邀，或将已过滤的聊天 payload 当作原始数据重投影；均不能交付旧文件。

## 6. 必需测试

- `tests/test_document_export.py`、`test_document_export_story.py`、`test_document_export_render.py`、`test_document_export_materials.py`：真实 HTTP／三格式、场景选择、缺失／过期摘要、中文／CRLF／emoji／格式、四类资料、字段引用、长笔记、跨作品与旧接口。
- `tests/test_document_export_access.py`、`test_document_export_access_postgresql.py`：本人会话／空会话、工具与私人引用过滤、原 actor/token、viewer／重邀、作品回收代次、超过 400 项分批、SQLite WAL／默认日志和 PG 双连接快照与事务释放。
- `tests/test_export.py`、`test_epub.py`、`test_rich_document_render.py` 及既有 agent 隐私测试：旧路径、EPUB 与共享个人投影不回归。
- `frontend/src/api/exports.test.ts`、`components/export/selection.test.ts`：请求体、空选／跨页、晚到 blob 和错误响应。真实浏览器补桌面／窄屏、键盘、实际下载；DOCX 另用实际文档查看器验证中文和多页。

## 7. 错误与正确示例

```python
# 错：把所有会话的原始 AgentMessage.content 直接输出；渲染后只检查当前是否 accepted。
# 对：capture_document_access 先冻结本人资料和原授权，生成文件后 verify_document_access
# 在独立一致快照比较 actor、授权代次、完整会话与消息证据及安全投影。
```

```typescript
// 错：await response.blob() 后不检查弹窗和身份，直接触发下载。
// 对：携带 signal 和 isCurrent，blob 完成后重新核对作用域，再创建 object URL。
```
