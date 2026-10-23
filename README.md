# AI 小说平台

多用户、自部署的 AI 辅助小说创作平台：创建项目后由 AI 向导生成世界观、角色与分卷大纲，再逐章流式生成、编辑、润色正文，最终导出 TXT / Markdown。

功能对标 [MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) 的核心创作闭环（自行实现，未复用其代码）。

## 功能

- 本地账户注册 / 登录，多用户数据隔离（可通过环境变量关闭注册）
- AI 模型配置：OpenAI 兼容接口（含中转、DeepSeek、本地 Ollama 等）、Anthropic Claude、Google Gemini；API Key 服务端加密存储，支持连通性测试
- 创作向导：AI 生成世界观、角色卡、分卷大纲，可重新生成和手动编辑
- 章节创作：按大纲流式生成正文、手动编辑、AI 润色；自动带入世界观、角色、大纲与前文作为上下文
- 导出整本或选中章节为 TXT / Markdown

## 快速开始（Docker）

```bash
cp .env.example .env
```

编辑 `.env`，至少填写 `POSTGRES_PASSWORD`、`JWT_SECRET`、`ENCRYPTION_KEY`（文件内有生成命令；数据库密码会拼进连接串，请只用字母、数字、`-`、`_`），然后：

```bash
docker compose up -d --build
```

访问 http://localhost:8000 ，注册账户 →「模型配置」添加 AI 接口 → 新建项目进入创作向导。

### 接入本地模型（Ollama 等）

出于安全考虑，默认禁止 AI 接口地址指向本地或内网，包括回环、私有网段和云元数据地址；同时禁止跟随重定向，防止注册用户借服务器探测内网（SSRF）。要接入本机的 Ollama，需要两步：

1. 在 `.env` 中设置 `ALLOW_PRIVATE_AI_ENDPOINTS=true`，然后重新执行 `docker compose up -d`。
2. 添加模型配置时，Base URL 填 `http://host.docker.internal:11434/v1`。容器里的 `localhost` 指向容器自己，填它连不到宿主机。

开放注册并部署在公网时，请保持该开关关闭。

## 本地开发

后端（需要 PostgreSQL，或用 SQLite：`DATABASE_URL=sqlite+aiosqlite:///./dev.db`）：

```bash
cd backend && cp .env.example .env && uv sync && uv run alembic upgrade head && uv run uvicorn app.main:app --reload
```

前端（开发服务器把 `/api` 代理到 8000 端口）：

```bash
cd frontend && npm install && npm run dev
```

## 测试

```bash
cd backend && uv run ruff check . && uv run pytest
```

```bash
cd frontend && npm run lint && npm run build && npm test
```

## 技术栈

FastAPI · SQLAlchemy 2 (async) · Alembic · PostgreSQL · React 18 · TypeScript · Ant Design 5 · Zustand · Vite

## 已知限制

- 服务进程中途崩溃会让章节停留在「生成中」，可在章节编辑器点「复位」恢复
- 同一项目同时发起两次向导生成，以后完成的结果为准
- 尚未实现：伏笔管理、关系图谱、提示词工坊、拆书、OAuth 登录、向量检索、EPUB 导出
