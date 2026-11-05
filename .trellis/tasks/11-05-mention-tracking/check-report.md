# 全范围检查报告

检查代理：`mention_full_check`（trellis-check），2026-11-05。检查范围为父任务 AC1–AC9、提及子任务及独立作品笔记子任务的全部实现；检查阶段尚未提交、未归档，未修改无关的 `pelican-cycling.html`。后续提交及收尾结果见开发者journal。

## Findings (fixed)

1. **设定深链首次加载失败后丢失目标。**
   - 文件：`frontend/src/hooks/useEntityDeepLink.ts`、`frontend/src/utils/mentions.ts`、`frontend/src/pages/project/{CharactersPanel,WorldPanel}.tsx`。
   - 原因：列表尚未成功加载时被调用方归一化为空数组，网络失败后会将 `?id=` 判为不存在并清除，之后重试不能再打开原目标。
   - 修复：区分未加载的 `undefined` 与已加载空列表；仅成功取得列表后解析目标，且验证 `project_id`。两处列表显式呈现失败和重试入口；使用稳定的空列表 memo 避免破坏现有 Hook 依赖。
   - 回归：`frontend/src/utils/mentions.test.ts` 覆盖首次失败仍等待、重试成功、真实空列表及跨作品目标。
2. **提及来源目录加载失败时重试按钮没有重试目录。**
   - 文件：`frontend/src/components/mentions/EntityMentions.tsx`。
   - 原因：错误区可能来自 catalog 请求，但原“重试”及“刷新提及”只重新加载结果列表，来源筛选保持失败状态。
   - 修复：两入口统一重新加载 catalog 和结果列表。未增加只重复函数实现的测试；由完整前端门禁及请求线路检查覆盖。
3. **两处既有备份测试没有同步当前导出 schema 版本。**
   - 文件：`backend/tests/test_project_cover.py`、`backend/tests/test_reader.py`。
   - 原因：本次迁移已到 `0033`，封面和阅读进度备份测试仍断言导出 `app_schema=0031`；全量检查暴露该漏项。
   - 修复：精确更新当前导出的版本断言为 `0033`，保留其余封面、阅读进度、恢复和损坏输入断言。旧包兼容测试仍以 `0031` 输入验证兼容，没有降低测试门槛。

## Findings (not fixed)

- 未发现需要保留的产品代码问题、接口设计分歧或任务外改动。
- 正式 spec 已由主会话同步至 `backend/mention-tracking.md`、两层索引及 `backend/project-transfer.md`。检查对照了 API、缓存限制、笔记字段、权限、备份和重试合同；发现的一处码点分段表述也已由主会话修正为实际的 `mentionSegments` / `Array.from` 路径，复核一致，无剩余 spec 漂移。

## 全范围核查

| 父验收项 | 实际路径与证据 |
| --- | --- |
| AC1 名称、别名及匹配 | `matcher.py` 对转义后的字面词执行大小写敏感、ASCII 边界、同起点长词优先和非重叠扫描；词典 fingerprint 包含身份/名称/别名。matcher 测试覆盖中文、英文、单字、包含、同名、emoji/CRLF。世界观 aliases API/0033 迁移/旧备份/基础 metadata revision 已核查。 |
| AC2 草稿与阅读 | `MentionPreview` 只请求无写入 preview，按当前原文与 hash 绑定响应；正文编辑/标记切换不替换草稿，AI 流期间暂停；Reader 保留原始空白及码点。`MentionText` 使用文本节点和可键盘操作按钮，同页只读预览；已有离页守卫保留。真实浏览器由主会话验证。 |
| AC3 四来源定位 | `sources.py` 分别读取 Chapter.content、本人会话中 user/assistant content、ProjectNote.content、KbDocument.text；工具载荷及资料重叠分块不参与。列表和 source window 共用稳定 source ID/hash；聊天链接包含 conversation/message ID，知识库窗口保持绝对偏移。HTTP 四源及窗口测试、前端 API/URL/Unicode 测试覆盖。 |
| AC4 独立笔记 | API→service→model→0032 迁移及 UI 的创建/查询/编辑/删除完整。更新和删除使用 revision CAS，409 保留输入；导航 blocker、身份/作品/mount 守卫阻止迟到结果回填。备份注册、旧包兼容、导入校验和 ID 重映射有回归。 |
| AC5 章节统计 | summary 只统计保存正文；筛选在条目分页之前执行。确定/歧义分列，空章不计 gap，最近章/count/gap 均基于同一筛选范围。前端按稳定章 ID 对齐，50 章窗口、不连续章号、数值替代视图、零值和未写状态均有测试；点击格子固定来源为 chapter。 |
| AC6 当前结果与旧链接 | 当前原文 SHA256、词典 fingerprint 和源身份控制 cache；标题/章号每次从当前库取值。新增测试实际执行手动/AI 保存、版本恢复、改名别名、删除来源、改章号和备份导入。hash 不符返回 stale、target=null，不盲选新原文旧偏移。旧 v2 效果缺 aliases 只兼容当前空别名，新增别名后的撤销仍冲突。 |
| AC7 权限与异步隔离 | `get_manageable_project` 在来源/cache 前验证生命周期和人工内容权限；chat 二次 join 会话真实归属。管理员 catalog 隐藏 chat，固定四键 source_counts 的 chat 为 0，显式 chat 请求 404。跨用户、跨本人作品、回收站均有 HTTP 回归；前端查询 key 包含作品/身份/授权/目标，AbortController 取消旧响应。 |
| AC8 现有能力与适配 | 全量前后端回归覆盖既有选区 AI、设定基础与剧情变化、项目备份、人工只读管理。标记/弹层不写编辑表单；世界观别名保留基础 revision 守卫。主题使用现有 token；窄屏、暗色及键盘由主会话真实浏览器最终核验。 |
| AC9 完整扫描与性能 | 来源按游标小批次读取，knowledge 每批 1 份；hash/匹配在线程执行；列表只保存当前分页片段但完整计数。LRU 上限 4096 来源/200000 总跨度/20000 单源跨度，超限扫描完整且不缓存，不截断结果。500 万正文+50 万资料实测见后端实施记录。 |

附加检查：路由不直接接触 ORM；读取路径 `no_autoflush` 且无提交；新增表登记项目备份及模型注册；迁移链为 `0031→0032→0033`；SQLite 迁移/回退测试保留关联数据；新增代码未引入调试输出、类型绕过或检查抑制。`get_context.py --mode packages` 确认单仓库 `backend`/`frontend` 两层，已按两层 index、quality guidelines 及 task check.jsonl 核查。

## Verification

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| Backend lint | `cd backend && .venv/bin/ruff check .` | 通过，All checks passed |
| Backend 全量原跑 | `cd backend && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/python -m pytest -q` | 1819 passed、6 failed、1 skipped、1 warning，888.84 秒，exit 1；6 个失败全部按下表复验解决 |
| Backend MCP 复验 | 同一 locale，`.venv/bin/python -m pytest tests/test_mcp_plugins.py -q`，允许本机端口的授权环境 | 21 passed，11.66 秒，exit 0 |
| Backend 封面复验 | 同一 locale，`.venv/bin/python -m pytest tests/test_project_cover.py -q` | 11 passed，6.35 秒，exit 0 |
| Backend Reader 复验 | 同一 locale，`.venv/bin/python -m pytest tests/test_reader.py -q` | 7 passed，4.69 秒，exit 0 |
| Frontend lint | `cd frontend && npm run lint` | 通过，零警告 |
| Frontend 类型/构建 | `cd frontend && npm run build` | 通过，`tsc -b` 和 Vite 均完成 |
| Frontend 全量测试 | `cd frontend && npm test` | 最终状态 115 文件/838 测试通过，8.57 秒 |
| 差异空白检查 | `git diff --check` | 通过 |

构建仍按路由拆包：`MentionsPanel` 13.87 kB、`NotesPanel` 10.05 kB；`RelationGraph` 保持独立 chunk。最大 chunk 为既有 `vendor-antd` 1158.44 kB，未超过现有 1500 kB 告警线。

后端全量执行与完整文件复验合计覆盖 **1825 个不同测试通过、1 个既有条件跳过**，没有未解决失败；这不是一次全量命令全绿的改写。复验之外的产品代码与首次全量状态相同，仅更新了上述两处 schema 版本测试断言。

| 全量原跑失败节点 | 原因 | 对应复验 |
| --- | --- | --- |
| `test_mcp_plugins.py::test_connect_time_ssrf_check_sends_no_request[settings_overrides0]` | 沙箱在 `mcp_fakes.py:87` 拒绝绑定 127.0.0.1 随机端口，`PermissionError [Errno 1]` | MCP 文件 21 passed 中包含此项 |
| `test_mcp_plugins.py::test_redirect_not_followed` | 同上 | MCP 文件 21 passed 中包含此项 |
| `test_mcp_plugins.py::test_fastmcp_integration_list_and_call[settings_overrides0-streamable_http]` | 同上 | MCP 文件 21 passed 中包含此项 |
| `test_mcp_plugins.py::test_fastmcp_integration_list_and_call[settings_overrides0-sse]` | 同上 | MCP 文件 21 passed 中包含此项 |
| `test_project_cover.py::test_cover_backup_roundtrip_legacy_and_corruption` | 当前导出为 0033，旧断言期待 0031 | 更新精确版本后封面文件 11 passed |
| `test_reader.py::test_progress_survives_backup_round_trip` | 同上 | 更新精确版本后 Reader 文件 7 passed |

唯一 skip 是 `test_migrations.py::test_0031_postgresql_cascades_sources_and_subjects`：环境未设置 `AINOVEL_POSTGRES_TEST_URL`；SQLite 迁移与备份实际执行，没有新增跳过。唯一后端 warning 来自既有 Gemini 图像 SDK 方法弃用测试，未涉及本功能。原跑最终失败明细和汇总尾部保存在 `/private/tmp/ainovel-mention-check-backend-result-tail.txt`，完整分批输出在本次工具执行记录。

后端验证调整测试进程 locale 为本机可用的 UTF-8，避免宿主默认 `C.UTF-8` 使 Alembic 中文配置按 ASCII 解码；MCP 复验仅放开本机监听的执行环境，没有修改服务桩、应用配置、业务断言或跳过条件。

## 浏览器与性能交叉证据

主会话使用独立 SQLite/合成作品执行真实浏览器验收，完整证据见同目录 `browser-verification.md`。反馈如下（非检查代理重复操作）：

- 四源列表与定位、知识库全文偏移 24103、聊天 message 2、Reader emoji、草稿预览不进入统计、离页保护、保存后旧 hash 明确失效。
- 新笔记从 `?new=1` 保存转为 `?id=2` 并可重开；地点/物品别名保存后可从笔记打开对应设定。
- 375px 设定预览可完整显示；资料抽屉最终 `width=375,left=0,right=375` 且 `scrollWidth=clientWidth=375`；统计页 `document.scrollWidth=viewport=375`。
- Enter 打开热力图“林远·第3章”格子，仅显示对应 2 处正文；时间线与逐章数值表一致。筛选 `q=云城`、第3–4章，青云城确定提及 1、gap 1。
- 桌面 1280px、浅色及暗色视觉正常。聊天新增合成后续消息后刷新，DOM 已加载 message 28，目标 message 2 的 top=494.9/bottom=540.7（900px 高）前后相同，没有被粘底抢走。

`../11-05-mention-index-navigation/implementation-backend.md` 的真实 HTTP 样例为 500 章/5000000 码点正文 + 10 份/500000 码点资料、202 条目；总计 1100 次且仅返回当前页 1 条，冷 0.2329 秒、热 0.1100 秒。该记录是特定 SQLite 合成语料的测量，不扩张为理论最大语料承诺。
