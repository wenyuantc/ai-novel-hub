# 技术设计：Word 与内容组合导出

状态：待最终审阅。需求以 `prd.md` 为准。

## 1. 架构与边界

采用单个复杂任务：三格式共享选择、资料装配、权限和文档模型，前后端必须共同验收，不拆成各自发布但选择行为不同的任务。分步依赖见 `implement.md`。

```text
ExportModal → POST /projects/{id}/export-document
 → 选择校验与人工导出鉴权 → 正文／摘要／资料快照
 → 统一可读文档模型 → TXT / Markdown / DOCX 渲染
 → 新快照权限与聊天证据复核 → 附件

EPUB／旧调用 → 原 GET /projects/{id}/export
```

保留旧 `export_service.py` 和 `ExportFormat`，避免自动扩大开放 API 支持格式。新服务放在 `services/document_export/`，建议分为 `service.py`、`story.py`、`materials.py`、`model.py`、`renderers.py`、`docx.py`。路由不查询 ORM。无数据库迁移、无后台任务、无 AI 调用。

## 2. 请求与响应

新增站内 Cookie 认证的 `POST /api/v1/projects/{project_id}/export-document`，请求由新 `schemas/export.py` 定义。

```json
{
  "format": "docx",
  "story": {"scope": "selected", "unit": "chapter", "content": "both", "ids": [12, 18]},
  "materials": {
    "settings": [{"kind": "character", "id": 7}],
    "conversation_ids": [22],
    "note_ids": [5, 9]
  }
}
```

| 字段 | 契约 |
| --- | --- |
| format | 必填 `docx|md|txt`；与旧 ExportFormat 分离。 |
| story.scope | `all|selected|none`，默认 all。 |
| story.unit | `chapter|scene`，默认 chapter。 |
| story.content | `prose|summary|both`，默认 prose。 |
| story.ids | 严格正整数、去重前先拒绝重复；selected 非空，其他 scope 必须为空。 |
| materials.settings | 四类本书 `{kind,id}`，复用 EntityRef 的身份合同。 |
| conversation_ids / note_ids | 非重复正整数列表，省略为空，不解释成全部。 |

嵌套 schema 拒绝未知字段、null、布尔冒充整数及非法组合。查询 ID 分批，避免超出数据库绑定参数上限。资料没有空选即全量参数；UI 全选明确是“本页全选”。

复用 `ExportFile` 的内容、中文安全文件名、Content-Disposition。DOCX MIME 为 `application/vnd.openxmlformats-officedocument.wordprocessingml.document`；TXT／MD 沿用旧类型。新响应 `Cache-Control: no-store`。X-Export-Warnings 按类别汇总数量，百分号编码后不超过 4 KiB，不放完整正文或全部章名；缺摘要／过期说明同时位于对应文档单元。

| 条件 | 结果 |
| --- | --- |
| 非法结构、格式、ID、重复项 | 422，中文 detail。 |
| 没有选择／最终无可导出单元 | 400，不生成空文件。 |
| 不存在、跨作品、退场、非本人会话 | 404，不泄漏名称。 |
| 自选项生成中、版本变化 | 409，保留选择，不自动重导。 |
| 聊天证据失效 | 沿用安全核验的 404，或导出专用 409；不更改旧详情的错误语义，不披露其他人的信息。 |
| 登录、token、作品导出权限失效 | 既有 401／403／404，丢弃结果。 |
| DOCX 不可表示的 XML 字符 | 400，指出已授权来源单元，不回显正文、不静默删字。 |

## 3. 文档模型与渲染

统一模型仅保存独立纯值：作品标题／简介、有序章节及资料标题、文本、可选 ReaderBlock、来源说明和警告；不携带 ORM、session、原始工具载荷或核验证据。禁止通过 dump ORM 生成资料。

- TXT 和 MD 使用同一内容模型；MD 复用 `rich_document_render.render_markdown`，不把正文再次解释成 HTML 后转 Word。
- DOCX 用 `python-docx>=1.2,<2`，更新 uv.lock 固定版本。官方 API／版本证据见 `research/docx-rendering.md`。
- 公开 heading／paragraph／run API 映射粗斜体、标题、列表和引用；保留中文、空行、全角缩进。字段使用短段落，避免长中文挤入窄表格列。
- DOCX 章标题与资料分类开始新页，场景为章下一级；配置常见字体及中文字体映射。无需服务端 Word，不嵌入宏、不加载外部图片或链接资源。
- DOCX 的段落／分页不承诺 TXT 字节级换行一致；文本选择和顺序必须一致。
- 重型同步渲染在线程执行，只传纯值模型，不传 AsyncSession；以内存二进制流生成，结束后释放，不留永久临时文件。

## 4. 正文、场景与摘要

### 选择与同快照投影

章节按 chapter_no／id，场景按所属章节和 sort／id 排序。all 枚举活动作品现存内容，跳过生成中章并汇总提示；selected 先验证所有 ID 的范围、活动状态与所属章状态，任何缺项不静默忽略。

章模式复用完整场景投影并保留 separator_before。场景模式每景为独立单元，使用 Scene.content；前景连接分隔不是本景正文，单元用文档段落分隔，不能把不连续选择伪装成完整章。

正文与格式必须同一 SQL 读取，保持 `rich_document_render` 的同快照、投影一致性和 Unicode 码点／CRLF 检查。新增场景投影只读 Scene／SceneDocumentMeta，不加载区块或标记来渲染正文。

### 摘要与空内容

- 章摘要批量使用 `analysis_service.latest_analyses`，只取 summary、分析时间、stale，不输出任务引用、评分和原始分析 DTO。
- stale 复用 `rich_document_ai.analysis_fingerprints`，包含开启作者区块对分析来源的影响，并遵循章版本复核。区块内容只用于内部指纹，不进入正文、资料或 AI 出站。
- 场景使用保存的 Scene.summary，不用章分析／细纲／大纲补空。说明是“已保存的场景摘要”，不计算虚构 freshness。
- both 为单元标题→摘要及说明→正文。summary 缺失保留“暂无摘要”，仍是合法单元；prose 空白跳过并提示。both 可无正文，摘要也空时仍保留占位。全部被生成状态排除且没有资料时 400。

## 5. 创作资料

### 设定

四类 ID 经 `setting_catalog_service.get_entries` 校验。各实体按明确字段白名单输出完整当前档案，不能用只含部分信息的 AI formatter。固定分类及既有 sort／id 排序。

批量复用 `resolve_entry_fields(..., position=None)` 与 `format_entry_fields`；下拉选项／引用用名称，保留合法停用字段的现存内容，不创建空绑定、不递归展开引用对象。字段目录与时间线版本按现有合同前后核对，跨批维持整体代次，竞争返回 409。

人物关系、组织成员／隶属、人物职业及职业阶段按所选条目附带名称和简短属性，重复关系去重，全部查询限定本书。被引用者不生成完整档案章节，不导历史。人工导出不套用 AI never／ai_hidden 或 token 预算。系列仅取已采用本书投影；说明“当前值可能包含计划章节变化”。

### 笔记

按 project_id＋所选 ID 批量读取完整标题和 content，按固定优先／更新时间／ID 排序。列表 excerpt 只服务选择界面。读取不写 revision、flags 或时间戳。

### 聊天

在 agent 服务边界新增只读 helper（可为 `agent/export_reads.py`），不依赖 TaskEngine、不查询提案或生成 TaskOut：

1. 复用作者／accepted 成员的本人会话入口，不以 AI 写权限或当前邀请轮次限制合法只读。
2. 冻结所选会话、已保存 user／assistant 消息。输出仅标题、角色、时间和正文；证据仍是完整 MessageOut 与会话创建身份、writable／sealed 快照。
3. 复用 personal_projection 的私人值过滤和新快照证据核验；新增批量 `conversation_snapshots` 证据，既有单会话 `conversation` 归一化后继续按原合同校验。不得重用已有 `conversations` 键改变语义：它仍是提案使用的会话创建时间映射。
4. 所有会话、消息和私人引用须在一次最终数据库快照中核验；大集合在该快照内分批，不能把逐会话的早期 READ COMMITTED 检查当成整份文件核验。
5. 保留 `owner_only=False`、无提案／无活动轮次的合法 proofs，完整形状见 `research/content-sources.md`。证据不进入导出模型，不能直接 SELECT content 绕过过滤。

批量消息证据可复用现有 messages 映射，但每个被选会话都必须具备完整创建身份、标题、writable／sealed 快照，包括没有可导出消息的会话；不能因为空消息跳过会话核验。批量核对覆盖缺项、替换、外部会话、标题／内容变化，不将多个独立核验之间的窗口当成原子保证。

本次捕获后新增的消息不自动加入；完整会话快照包含更新时间等字段，因此新增消息／摘要更新也可能按既有保守规则导致本次冲突，需要用户重试。捕获的消息／会话身份或内容变化时拒绝文件。安全投影可能隐藏正文中的私人结构化信息，保持现有界面语义。

## 6. 一致性与权限寿命

入口要求 `require_project(..., "export")`，冻结原认证 actor／token、作品与协作邀请／revision，以及生命周期代次 `Project.last_deleted_at`。提交时逐项重验，目录不是授权证明；回收后恢复也不能令旧导出恢复有效。

正文和格式、摘要指纹、自定义字段各自保持现有一致性边界。整份文件是本次请求读取的已保存资料集合，不承诺独立分类都对应数据库同一时点。不得持写锁等待 DOCX 渲染。

冻结纯值模型后结束已完成的读事务；最终复核沿用 `visibility_sessionmaker` 的新会话方法，并传递原 `AuthenticatedActor`，不能只凭 user_id 绕过 token 版本。

渲染完成、响应交付前复核作品权限及原协作身份；含聊天时对**原始冻结的最小 payload**和全部原 proofs 再次投影，与第一次用于渲染的安全内容作结构化比较。不得重查并冻结新 proofs，也不能把已过滤内容当作第二次原材料。变化则丢弃文件并报冲突／相应安全错误，不自动循环重导。最后核验后不等待其他 I/O；已交付文件无法撤回。

**导出专用授权证据必须与聊天核验处于同一最终快照。** 当前 personal_projection 的 auth CTE 未取 member.revision，也不比对捕获时邀请／revision；只检查 accepted 和 writable 不足以发现撤权后重邀且旧会话仍只读的竞争。新增可选导出授权证据，携带初始 actor／作品／owner、last_deleted_at 及协作 invitation_id、revision，在最终授权事实中比较完整身份；legacy 调用未提供此证据时保持原有语义。无聊天也用同等新快照授权核验。不得把较早的 require_project(grant_identity=...) 与随后另一个快照的聊天 SQL 拼成保证。

### 大集合的最终快照方案

现有“每 400 个 ID 生成片段再 UNION ALL”仍是一条大 SQL，不能解决总绑定参数上限。新增导出核验入口采用**渲染结束后才打开的短只读事务**：PostgreSQL 使用连接级 REPEATABLE READ，在首条 SELECT 前设置；SQLite 明确 BEGIN，使 SELECT 位于真实读事务。不改全局引擎或其他业务隔离级别。该核验会话复制原 authenticated_actor 和 authorization_runtime；不能重新构造当前 token。

在同一核验连接／事务内按类型分批，每批最多 200 个 ID，并检查实际单条语句的总参数数；授权、会话、消息和私人引用都取自此快照，完整集合验证后才对原始 payload 投影。抽取现有安全比较／过滤原语复用，旧单会话入口不强制改为新事务路径。禁止在分批期间再次调用会另开会话的旧 _final_visibility 来破坏快照。finally 结束事务并关闭会话，取消也要释放连接；只读事务不跨 DOCX 渲染。

最终快照开始前已提交的撤权／token／生命周期变化必须被看到；快照开始后的外部变更不承诺撤回已通过核验的交付。SQLite WAL 与默认日志模式、PostgreSQL 第二连接提交竞争均需测试，不能只以 AsyncSession.begin 的 Python 状态断言 SQLite 真正开始事务。

技术依据：[PostgreSQL 事务隔离](https://www.postgresql.org/docs/current/transaction-iso.html)、[SQLite 隔离](https://www.sqlite.org/isolation.html)、[SQLAlchemy SQLite 事务](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#transactions-with-sqlite-and-the-sqlite3-driver) 与 [连接隔离级别](https://docs.sqlalchemy.org/en/20/core/connections.html#setting-transaction-isolation-levels-including-dbapi-autocommit)，2026-11-16 核对。

分批读取避免 ID 参数和重复 JOIN 放大；前端不加载资料全文。验收用百万中文的多章／多景混合资料样本记录耗时和资源，不承诺未经测量的固定时限，不静默截断。

## 7. 前端

复用 OverviewPanel 的 ExportModal，必要时拆分 `components/export/` 与纯状态模块；格式新增 DOCX，初始格式保留 TXT。

- 文档模式显示章／景、全部／自选／不含故事、正文／摘要模式与三类资料；两个粒度分别保存本地选择，只提交当前粒度。
- 目录复用现有章节、场景分页、setting entries、本人会话和 notes API；完整处理分页。资料分类打开才加载目录，不查询未选资料正文。
- 稳定键 kind:id，分页／搜索保留已选项；本页全选、计数、已选名称、单项移除、清空均明确。空自选不退回 all。
- EPUB 仍走旧字段和 GET；切换不携带组合参数，返回文档模式保留弹窗内状态，不跨账号持久存储。
- 新 `api/exports.ts` 用 rawRequest 发 POST JSON，复用文件名和提示解码。若抽取 blob 保存函数，保持旧 API 签名及行为。
- 请求捕获 ProjectScope、弹窗和请求代次，使用 AbortController。blob 读取后、创建 URL／触发下载前检查 `isProjectScopeCurrent(scope, "canExport")`；失效不下载、不报成功、不关闭新弹窗。
- 失败保留选择、显示 ApiError 中文详情；阻止并发重复提交，不把刷新当自动重试。对象 URL 及时释放。
- AntD 主题 token、可访问中文标签；375px 表单可换行，无页面级横向溢出。

## 8. 兼容、发布与回滚

新后端接口先部署，再部署前端；无迁移。旧 ExportFormat、GET 默认空选、开放 API 和 JSON 备份不变。

批量聊天 proofs 为最敏感共享边界，须保留旧单会话调用与隐私测试。回滚前端恢复旧导出，后端移除新路由和依赖／锁文件变更，不更改作品数据。

实现与验证后同步 README、TODOS 第 12 项及导出 specs；不能把规划写成已实现。DOCX 包结构测试须配合实际渲染／打开，未验证的 Office 软件版本如实注明。
