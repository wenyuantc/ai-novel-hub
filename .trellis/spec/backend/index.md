# 后端开发规范

> 本项目后端开发的最佳实践。

---

## 概述

后端：FastAPI + 异步 SQLAlchemy 2 + Alembic + PostgreSQL（asyncpg），使用 `uv` 管理，
使用 pytest 测试（通过 aiosqlite 使用 SQLite）。源码位于 `backend/`；设计意图见
`.trellis/tasks/10-22-ai-novel-platform/design.md`。这些文件记录代码实际遵循的约定，
并附有摘自真实文件的代码片段。

质量门禁：`cd backend && uv run ruff check . && uv run pytest`。

---

## 规范索引

| 规范 | 说明 | 状态 |
|-------|-------------|--------|
| [目录结构](./directory-structure.md) | `app/` 分层（api/v1 → services → models、ai/providers、ai/prompts）；路由绝不直接接触 ORM | 已填写 |
| [数据库规范](./database-guidelines.md) | 异步 engine/session、Alembic `0001`、外键 CASCADE 与 `outline_id` SET NULL、user_id 归属校验 → 404、长度限制、字数统计 | 已填写 |
| [错误处理](./error-handling.md) | `AppError` 层级、`{"detail": ...}` 响应包、安全的 422/500 处理器、SSE 流式契约场景 | 已填写 |
| [质量规范](./quality-guidelines.md) | ruff + pytest、测试隔离、FakeLLM/fake_dns、禁止模式、SSRF 防护与认证/密钥场景 | 已填写 |
| [结构化设定](./story-entities.md) | 关系 `apply_change` 契约、组织循环检测、卡片 v1 导入导出、设定类 AI 任务、章节选角与上下文裁剪顺序 | 已填写 |
| [AI 批量关系生成](./relationship-generation.md) | 所选 ID 范围、只读草稿与勾选保存、关系/类型并发冲突、事务所有权、任务重开及刷新 | 已填写 |
| [大纲进阶](./outline.md) | 章节号平移登记表（新增章节号字段必须登记）、细纲与建章、大纲快照与恢复、`ainovel.outline` 导入导出、续写 / 展开任务、向导大纲模式与进度 | 已填写 |
| [提示词模板与写作风格](./prompts-styles.md) | 沙箱 Jinja2 模板（31 个 key、锁定变量、校验规则）、parity 对照测试、用户覆盖 / 指纹 / 历史版本、写作风格库与解析优先级、风格段注入 | 已填写 |
| [实例内提示词工坊](./workshop.md) | 风格 / 单 key 模板共享、审核快照、404/403 权限、匿名与删号署名、原子计数、导入前版本与恢复 | 已填写 |
| [灵感模式](./inspiration.md) | 立项结构化字段与【作品信息】、灵感会话 `state` 结构、步骤与 stale 规则、生成选项同步 SSE（请求内重试 / `retry` 事件 / 断开不落库 / 进程内锁）、转为项目与应用到已有项目 | 已填写 |
| [检索基建](./retrieval.md) | 统一检索单元 `retrieval_chunks`、BM25 + 向量混合打分（有界、阈值、旧向量不用）、向量模型（embedding）、知识库解析 / 分块 / `kb.index` 任务、剧情记忆抽取 / 召回、注入点接入方法 | 已填写 |
| [项目备份与 EPUB](./project-transfer.md) | `ainovel.project` v1 格式、表规则注册表（新增项目相关表必须登记，反射测试）、JSON 内 id 重映射、流式导出、预览令牌与单事务导入、风格 / 全局关系类型对应、EPUB 3 + NCX 结构与分卷规则 | 已填写 |
| [项目封面](./project-cover.md) | 图片上传/生成预览确认、拥有权与魔数校验、微秒版本 URL、图片 Provider、备份可选 cover 与 EPUB 封面 | 已填写 |
| [阅读器](./reader.md) | `projects.reading_progress`、`PUT /reading-progress`、`ProjectOut.last_edited_chapter_id` / `reading`（批量查询、删章兜底）、`/reader/toc` | 已填写 |
| [批量生成](./batch-generate.md) | `chapter.batch` 单任务逐章生成 + 任务内分析、断点（`step_cursor` / `checkpoint`）、待审核（`paused`）继续 / 结束、范围锁、失败重试与续跑 | 已填写 |
| [拆书导入](./book-import.md) | `book_imports` 解析记录（切章强规则、序章 / 目录区 / 兜底、docx / epub、子进程正则）、7 种校正与提示、修订号 409 `code = stale_revision`、单事务开始导入、`book.import` 断点 / 暂停 / 跳过 / 只写空字段 | 已填写 |
| [项目智能体](./agent.md) | Provider 工具调用协议（`chat_tools_stream`、`TOOLS_UNSUPPORTED`、三家差异、`provider_metadata` 重放）、工具层与新增工具检查清单（kind 分级、`validate_references`、`id_model`）、提案 / 自动执行（首批 ≤3 项）/ 确认时预览比较 409、效果快照与撤销（`EFFECT_TABLES`、冲突检测、外键顺序）、`agent.turn` 断点续跑、安全规则；`external` kind、动态 MCP 工具、Skill 读工具与强制启用 | 已填写 |
| [Skill 与 MCP 插件](./skills-mcp.md) | Skill 两级归属（不可见 404 / 可见不可写 403）、复制与上限、SKILL.md / zip 格式与安全限制、触发词前缀匹配、生成任务 Skill 快照与 `validate_retry`；MCP 适配层（SDK 1.30、逐请求 SSRF、`max_redirects=0`、超时、脱敏、`to_text`）、请求头加密与整体替换、`mcpServers` 导入、本轮映射表（命名 / 64 字 / 30 项 / checkpoint）、`external` 提案、中断回收 `recover_external`、`MCP_ENABLED` | 已填写 |
| [管理员与会话](./auth-admin.md) | 管理员初始化环境变量、`/admin/users`、防护规则、`token_version` 会话失效、修改密码、全局 Skill 的管理员权限、`MCP_ENABLED` | 已填写 |
| [管理员系统设置](./system-settings.md) | 六项白名单、持久覆盖优先级、revision与原子审计发布、MCP真实调用边界、30秒公开同步及逐项恢复 | 已填写 |
| [管理员首页与全站统计](./admin-dashboard.md) | 含回收站的总量、北京时间日快照、独立任务累计、UUID执行身份、24h异常、历史缺口及刷新 | 已填写 |
| [管理员全站任务监控](./admin-task-monitoring.md) | 现存安全元数据、单SQL筛选计数、本轮时间、持久取消受理/审计、资源屏障与轮次CAS、5秒刷新 | 已填写 |
| [管理员操作审计](./admin-audit.md) | 真实 actor、同事务摘要与失败、版本关联、组合查询、日期预览/nonce/高水位清理与不可清理留痕 | 已填写 |
| [管理员作品管理](./admin-projects.md) | `/admin/projects` 查询与聚合、人工内容管理员例外、严格 owner helper、归属投影、私有资源/模型副作用/阅读进度边界 | 已填写 |
| [作品回收站](./recycle-bin.md) | 30天软删除、管理员恢复/彻删、任务执行链排空、父行事务屏障、到期清理、RESTRICT删号与系统审计 | 已填写 |
| [平台 API Key 与开放读取](./open-api.md) | 可再次查看 / 复制的密钥、小说授权、独立 X-API-Key、数据库原子分钟额度、20 个只读接口、专属 OpenAPI / Swagger UI、前端显隐与复制 | 已填写 |
| [日志规范](./logging-guidelines.md) | 标准库 `logging`、在错误被屏蔽处使用 `logger.exception`、绝不记录密钥 | 已填写 |

### Code-spec 场景（7 段式契约）

| 契约 | 位置 |
|----------|----------|
| SSE 流式（帧、事件、状态生命周期、回滚） | [error-handling.md](./error-handling.md#场景sse-流式契约-generate--polish--wizard) |
| SSRF 端点防护（`ALLOW_PRIVATE_AI_ENDPOINTS`、`build_provider`） | [quality-guidelines.md](./quality-guidelines.md#场景用户提供的-ai-base-url-的-ssrf-防护) |
| 认证与密钥环境（cookie、JWT、Fernet、密钥掩码） | [quality-guidelines.md](./quality-guidelines.md#场景认证与密钥环境契约) |
| 人物关系写入（`apply_change`、视角称呼、历史） | [story-entities.md](./story-entities.md#场景人物关系写入relationship_serviceapply_change) |
| 章节上下文组装（选角 D4、裁剪顺序） | [story-entities.md](./story-entities.md#场景章节上下文组装选角与裁剪d4) |
| 章节号平移（`CHAPTER_NO_REFS`）、细纲建章、快照恢复 | [outline.md](./outline.md) |
| 批量顺序生成（断点、待审核、范围锁、任务内分析） | [batch-generate.md](./batch-generate.md) |
| 拆书导入（切章与校正、修订号乐观并发、开始导入事务、反推断点 / 暂停 / 跳过、只写空字段） | [book-import.md](./book-import.md) |
| 项目智能体（工具调用协议、提案确认、效果快照与撤销、续跑） | [agent.md](./agent.md) |
| Skill 权限与导入格式、生成任务快照、MCP 适配与安全、动态工具映射、external 提案与中断回收 | [skills-mcp.md](./skills-mcp.md) |
| 检索打分与缓存、知识库建索引、剧情记忆同步与召回 | [retrieval.md](./retrieval.md) |
| 项目备份注册表 / 导入映射、EPUB 结构 | [project-transfer.md](./project-transfer.md) |
| 封面上传与生成预览、版本 URL、图片备份与 EPUB | [project-cover.md](./project-cover.md) |
| 提示词模板（校验 / 渲染 / 覆盖）与写作风格解析 | [prompts-styles.md](./prompts-styles.md) |
| 实例内共享（审核状态、署名、点赞 / 下载计数、模板导入恢复） | [workshop.md](./workshop.md) |
| 管理员角色、用户管理、token 版本控制（`ver`、防护规则） | [auth-admin.md](./auth-admin.md#场景管理员角色用户管理与-token-版本控制) |

---

## 如何维护这些规范

1. 记录项目的**实际约定**（而非理想状态）；编辑前先对照代码核实。
2. 包含从代码库复制的**代码示例**，并注明文件路径。
3. 列出**禁止模式**及其原因。
4. 补充团队曾犯过的**常见错误**（例如返回 500 并回显密码的 422 处理器）。

---

**语言**：所有文档均使用**简体中文**编写；代码、标识符、路径与命令保持原样。
