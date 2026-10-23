# AI 小说平台

多用户、自部署的 AI 辅助小说创作平台：创建项目后由 AI 向导生成世界观、角色与分卷大纲，再逐章流式生成、编辑、润色正文，最终导出 TXT / Markdown。

功能对标 [MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) 的核心创作闭环（自行实现，未复用其代码）。

## 功能

- 本地账户注册 / 登录，多用户数据隔离（可通过环境变量关闭注册）；用户可自助修改密码
- 管理员用户管理：创建用户、搜索分页、重置密码、禁用 / 启用、设为 / 取消管理员、删除用户
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

### 管理员

在 `.env` 中同时设置 `ADMIN_USERNAME` 与 `ADMIN_PASSWORD`（规则同注册：用户名 3–32 位且不含空白，密码 6–72 字节；只设一个会启动失败），服务启动时会：

- 账号不存在：创建为管理员（不受 `REGISTRATION_ENABLED` 影响）；
- 账号已存在：确保其为启用中的管理员，**不会覆盖密码**。之后修改 `.env` 里的 `ADMIN_PASSWORD` 不会生效，改密码请登录后在右上角菜单「修改密码」中操作。

管理员登录后在右上角菜单进入「用户管理」，可以创建用户（关闭注册后由此开通账号）、重置密码、禁用 / 启用、设为 / 取消管理员、删除用户（连同其全部项目、章节和模型配置，需输入用户名确认）。管理员看不到其他用户的小说内容。

- 重置密码、禁用账号、用户自助改密后，该用户已登录的其他会话立即失效。
- 管理员不能禁用、降级或删除自己，系统始终至少保留一名启用中的管理员。
- 忘记管理员密码时，可由另一名管理员重置；若没有其他管理员，可临时把 `ADMIN_USERNAME` 换成一个新用户名重启，引导出新的管理员。

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
