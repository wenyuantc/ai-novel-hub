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
| [大纲进阶](./outline.md) | 章节号平移登记表（新增章节号字段必须登记）、细纲与建章、大纲快照与恢复、`ainovel.outline` 导入导出、续写 / 展开任务、向导大纲模式与进度 | 已填写 |
| [管理员与会话](./auth-admin.md) | 管理员初始化环境变量、`/admin/users`、防护规则、`token_version` 会话失效、修改密码 | 已填写 |
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
| 管理员角色、用户管理、token 版本控制（`ver`、防护规则） | [auth-admin.md](./auth-admin.md#场景管理员角色用户管理与-token-版本控制) |

---

## 如何维护这些规范

1. 记录项目的**实际约定**（而非理想状态）；编辑前先对照代码核实。
2. 包含从代码库复制的**代码示例**，并注明文件路径。
3. 列出**禁止模式**及其原因。
4. 补充团队曾犯过的**常见错误**（例如返回 500 并回显密码的 422 处理器）。

---

**语言**：所有文档均使用**简体中文**编写；代码、标识符、路径与命令保持原样。
