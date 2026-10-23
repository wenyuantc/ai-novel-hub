# 技术设计：AI 小说平台 MVP

## 架构总览

Monorepo 双端分离：

```
aiNovel/
├── backend/              # FastAPI 应用
│   └── app/
│       ├── api/          # 路由 (v1)
│       ├── core/         # 配置 / 安全 / 依赖注入
│       ├── models/       # SQLAlchemy ORM
│       ├── schemas/      # Pydantic
│       ├── services/     # 业务逻辑（user_id 隔离统一在此层）
│       └── ai/           # LLM Provider 抽象 + prompts/
├── frontend/             # React 18 + TS + Vite + AntD + Zustand
├── docker-compose.yml    # app + postgres
└── .env.example
```

- 后端：FastAPI + SQLAlchemy 2.x (async) + asyncpg + PostgreSQL 16+，Alembic 管迁移
- 认证：本地账户（bcrypt 哈希），JWT 置于 httpOnly Cookie；注册开关 `REGISTRATION_ENABLED`（默认开）
- 前端：React Router 路由，Zustand 状态，AntD 组件，fetch + SSE 读流

## 数据模型（核心表）

- `users(id, username, password_hash, created_at)`
- `ai_configs(id, user_id, provider, base_url, api_key_enc, model, is_default)` — provider ∈ openai_compat | anthropic | gemini
- `projects(id, user_id, title, genre, premise, style, status, created_at)`
- `world_settings(id, project_id, title, content, sort)`
- `characters(id, project_id, name, role_type, profile, relations_text, sort)`
- `outlines(id, project_id, volume_no, chapter_no, title, summary, sort)`
- `chapters(id, project_id, outline_id, chapter_no, title, content, status, word_count, updated_at)` — status ∈ draft | generating | done

数据隔离约束：所有业务查询必须经 service 层按 user_id 过滤，路由层不得直连 ORM。

## AI 集成

- Provider 抽象：`generate_stream(messages, model, **kw) -> AsyncIterator[str]`
  - openai_compat：openai SDK + 自定义 base_url（覆盖中转 / DeepSeek / Ollama 等）
  - anthropic、gemini：各官方 SDK
- API Key 服务端对称加密存储（主密钥来自 env，缺失则启动 fail-fast）；接口回显仅掩码
- SSRF 防护（`app/ai/endpoint_guard.py`）：所有模型请求经 `ai_config_service.build_provider` 统一出口；`ALLOW_PRIVATE_AI_ENDPOINTS=false`（默认）时解析 Base URL，任一地址非公网（回环/私网/链路本地/保留/组播，含 IPv4 映射 IPv6）即拒绝，并令三个 SDK 不跟随重定向。残留风险：校验与连接之间的 DNS 重绑定窗口，更强隔离需在网络层限制出站
- 流式：后端 SSE（`text/event-stream`）透传增量 token，生成完成后持久化全文并更新章节状态
- 提示词集中在 `ai/prompts/`（向导-大纲/角色/世界观、章节生成、润色），模板化拼装
- 上下文组装（MVP 不用向量检索）：世界观全文 + 角色卡 + 当前卷大纲 + 最近 1–2 章全文 + 更早章节标题/摘要，按 token 预算裁剪

## 关键接口（REST /api/v1）

- `POST /auth/register|login|logout`，`GET /auth/me`
- `CRUD /ai-configs`，`POST /ai-configs/{id}/test`（连通性测试）
- `CRUD /projects`
- `POST /projects/{id}/wizard/outline|characters|world`（SSE）
- `CRUD /projects/{id}/world | /characters | /outlines`
- `CRUD /projects/{id}/chapters`，`POST /chapters/{id}/generate|polish`（SSE）
- `GET /projects/{id}/export?format=txt|md`

## 兼容与扩展点

- 全新项目，无迁移负担；Schema 演进走 Alembic
- 为延期能力预留扩展点：向量检索（后续 pgvector）、OAuth（auth 模块留 provider 位）、提示词工坊（prompts 后续表化）

## 权衡记录

- 不引入 sentence-transformers/向量检索：MVP 用结构化上下文组装即可，省 400MB 模型与推理依赖；长篇一致性需求显现后再加 pgvector
- JWT-in-Cookie 而非 Session 表：少一张表、实现简单；代价是无法服务端强制下线（MVP 可接受）
- 导出仅 TXT/Markdown；EPUB 等后续迭代

## 运维 / 回滚

- docker-compose：app + postgres（数据卷持久化）；密钥走 .env，不入库不入 git
- 迁移回滚 `alembic downgrade`；发布回滚靠镜像 tag 回退
- SSE 注意反代/中间件缓冲问题（见 implement.md 风险点）
