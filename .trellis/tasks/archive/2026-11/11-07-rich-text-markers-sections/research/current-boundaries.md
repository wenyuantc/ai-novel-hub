# 正文与编辑器现状研究（2026-11-08）

## 当前事实

- 场景正式正文是 `Scene.content`（`backend/app/models/scenes.py:9`），章正文由活动场景的 `separator_before + content` 无损拼接（`backend/app/services/scene_service.py:59`）。统一写入会推进内容 CAS、重算投影和字数（同文件 `:137`、`:153`），结构快照目前为 schema 1（`:213`，`backend/app/schemas/scene.py:150`）。旧场景拆分是正文码点切片（`scene_service.py:494`），合并为连接（`:565`）。
- 场景编辑器当前为受控文本框（`frontend/src/components/scenes/SceneWritingPanel.tsx:27,72`）；选区是 UTF-16，AI 请求转换为 Unicode 码点并先保存（`frontend/src/components/chapter/RewritePanel.tsx:18,117-119`）。提及定位依赖整章纯文本 hash 和场景相对位置（`SceneWritingPanel.tsx:42`）。
- 普通 AI 改写发送选区以及前后文和前序场景（`backend/app/services/rewrite_service.py:110-160`）；全场景／整章任务在结果齐备后原子替换场景正文（`backend/app/services/generation_service.py:441-468`）。上下文构造、自动分析、Agent 工具以及续写还存在其他正文读取入口；不能只在 UI 隐藏区块。
- 后台引擎支持 `paused` 事件，暂停后不占活动并发／资源键，且不调用 `on_abort/on_success`（`backend/app/services/task_engine/engine.py:9-14,406-411,495-505`）；因此新待确认路径必须持久化提案、显式释放章抢占并在确认时重新鉴权／核对版本。批量任务使用 `step_cursor` 和 `checkpoint`（`backend/app/services/batch_service.py:273-292,557-634`）；确认后必须从当前章正确接续，不重新计费生成或提前分析。
- 阅读器目前按换行拆正文并作为文本渲染（`frontend/src/utils/reader.ts:152`、`frontend/src/pages/ReaderPage.tsx:169,349`）；TXT／Markdown 导出当前直接输出 `chapter.content`（`backend/app/services/export_service.py:62,76`），EPUB 将它按行拆段（`backend/app/services/epub_service.py:111,158`）。分区块不能混入这些纯正文读路径，富格式输出需要独立受限结构与安全序列化。
- 备份当前 `ainovel.project` v7，`APP_SCHEMA=0039`（`backend/app/services/project_transfer/exporter.py:32-35`），注册表和场景快照校验维护正文一致性（`project_transfer/registry.py`、`project_transfer/scenes.py`）。新的区块、标记与格式是核心作者资料，不能仅放在任务日志或前端；须升级备份并处理旧版、历史引用与不安全降级。
- 本仓库 `frontend/package.json` 尚无富文本编辑器。Tiptap 开源核心官方文档说明其使用 JSON 树、基于 ProseMirror schema 与事务，可通过自定义节点视图实现拖动：[核心概念](https://tiptap.dev/docs/editor/core-concepts/introduction)、[schema](https://tiptap.dev/docs/editor/core-concepts/schema)、[React 节点视图](https://tiptap.dev/docs/editor/extensions/custom-extensions/node-views/react)。官方还说明不合法结构默认可能被丢弃，必须启用检查并在本项目服务端拒绝未知结构，不能静默修复。
- 本机 2026-11-08 使用 `npm view` 查询 `@tiptap/react@3.33.0` 为 MIT，peer 支持 React 18；`@tiptap/starter-kit@3.33.0` 为 MIT。隔离 `/tmp` 的 Node schema 实验表明 JSON 文本节点可保留含 `\r\n`、制表符和尾空格的字符串，但**没有验证浏览器编辑、粘贴或拆段往返**，因此不能把该实验当作无损证明；实现时必须先跑真实 DOM／跨语言投影实验作为闸门。

## 设计后果

1. 以场景正式纯文本为兼容权威，富格式、区块和标记以具类型元数据／实体组织；章投影仍仅包含正式正文。区块各自拥有文本和格式，不是另一份章正文。
2. 一次操作以章 `content_revision` CAS 原子保存纯文本及元数据；旧文本写入口不得悄悄覆盖新结构。未改动的旧数据禁止因迁移被解析成 Markdown、自动分段、补换行或 trim。
3. AI 投影必须按区块 `ai_include` 过滤；关闭后主动选区的例外只允许一次、绑定目标与版本并经过明确确认。需要覆盖实际出站、重试、Agent、分析和前章上下文，不能只测 UI。关闭开关不能追回以前已经发送或产生的摘要、聊天、用户手工复制内容。
4. 面向读者的格式与作者标记分离；标记失效时另保留可重关联记录。模型整体改写正文时，对不可可靠恢复的区块位置／格式进入持久、可取消且可重新验证的待确认流程，保持原章完整直到采纳。
5. 前端编辑器位置是 ProseMirror 结构位置及 UTF-16，后端正文坐标为 Python Unicode 码点；必须有共享定义的投影与双向映射，不能直接复用 textarea 的扁平偏移。验证 emoji、组合字符、换行、列表边界、区块和场景交界。
