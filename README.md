# AI 小说平台

多用户、自部署的 AI 辅助小说创作平台：创建项目后由 AI 向导生成世界观、角色与分卷大纲，再逐章流式生成、编辑、润色正文，最终导出 TXT / Markdown / EPUB。

功能对标 [MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) 的核心创作闭环（自行实现，未复用其代码）。

## 功能

- 本地账户注册 / 登录，多用户数据隔离（可通过环境变量关闭注册）；用户可自助修改密码
- 管理员用户管理：创建用户、搜索分页、重置密码、禁用 / 启用、设为 / 取消管理员、删除用户
- AI 模型配置：OpenAI 兼容接口（含中转、DeepSeek、本地 Ollama 等）、Anthropic Claude、Google Gemini；API Key 服务端加密存储，支持连通性测试
- 创作向导：AI 生成世界观、角色卡、分卷大纲，可重新生成和手动编辑
- 章节创作：按大纲流式生成正文、手动编辑、AI 润色；自动带入世界观、角色、大纲与前文作为上下文
- 导出整本或选中章节为 TXT / Markdown / EPUB
- 平台 API Key：生成与管理密钥，点击图标显示 / 隐藏、复制，配置小说授权范围及有效期；提供小说、章节和创作设定的只读开放 API 与 OpenAPI 文档

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

## 开放 API

登录后进入「API 密钥」，创建一把平台访问密钥。可以只授权指定小说，也可以选择本人的全部小说（包含之后新增的小说）；默认有效期 90 天，可指定到期时间或选择不过期。

升级已有实例时，先应用新增数据库迁移并重启后端；本地在 `backend/` 运行 `uv run alembic upgrade head`，Docker 启动命令会自动执行迁移。

密钥默认显示掩码，创建后仍可点击眼睛图标显示 / 隐藏，并通过复制按钮复制完整值。仅密钥所有者能够查看，管理员没有额外的查看权限。平台密钥用于读取本平台内容，与「模型配置」中填写的模型供应商 API Key 分别管理。

文档与描述文件（在当前部署地址后加路径）：

- `/developers`：面向接入客户的公开开发指南，首页顶部“开发文档”可进入；包含快速开始、鉴权、接口目录、分页和错误处理，沿用官网界面风格。
- `/api/open/v1/docs`：Swagger UI，可填写 `X-API-Key` 在线调试。
- `/api/open/v1/openapi.json`：独立 OpenAPI JSON，可下载或导入调用工具。

文档可以直接浏览；实际读取需要在请求头中提供 `X-API-Key`。网页登录 Cookie 或 JWT 不能替代平台 Key，平台 Key 也不能用来调用内部管理或写入接口。

```bash
# 先将 AINOVEL_API_KEY 设为在页面生成的完整平台密钥
curl -H "X-API-Key: ${AINOVEL_API_KEY}" \
  'http://localhost:8000/api/open/v1/novels?page=1&page_size=20'
```

所有业务读取均使用 GET，统一前缀 `/api/open/v1`：

| 路径 | 返回内容 |
| --- | --- |
| `/me` | 当前 Key 的名称、有效期与小说授权范围 |
| `/novels`、`/novels/{novel_id}` | 小说列表、搜索、简介、类型、状态及字数 / 章节数 |
| `/novels/{novel_id}/chapters` | 章节目录、卷信息、状态、字数与更新时间 |
| `/novels/{novel_id}/chapters/{chapter_id}` | 单章正文和元信息 |
| `/novels/{novel_id}/cover` | 封面图片，仍需要 Key 请求头 |
| `/novels/{novel_id}/export?format=epub` | 全文下载，format 支持 txt / md / epub |
| `/novels/{novel_id}/characters`、`/novels/{novel_id}/characters/{character_id}` | 人物列表与详情 |
| `/novels/{novel_id}/world`、`/novels/{novel_id}/world/{setting_id}` | 世界观列表与详情 |
| `/novels/{novel_id}/outlines`、`/novels/{novel_id}/outlines/{outline_id}` | 大纲列表与详情，含分卷信息 |
| `/novels/{novel_id}/outlines/{outline_id}/plans` | 大纲下的章节细纲 |
| `/novels/{novel_id}/organizations`、`/novels/{novel_id}/organizations/{organization_id}` | 组织列表与详情 |
| `/novels/{novel_id}/organizations/{organization_id}/members` | 组织成员及当前状态 |
| `/novels/{novel_id}/relationships`、`/novels/{novel_id}/relationships/{relationship_id}` | 人物关系列表与详情 |
| `/novels/{novel_id}/relationship-types` | 全局预置和当前小说自定义关系类型 |

列表返回 `{items, total, page, page_size}`，默认每页 20 条、最多 100 条。章节可用带时区的 `updated_after` 筛选该行的更新；删除事件和人物改名等派生字段变化不属于完整增量同步，需要定期完整读取并比对。小说 `updated_at` 表示元数据更新时间，`content_updated_at` 表示现存章节的最近更新时间。

章节目录包含空章节和草稿，正文可能为空；全文导出沿用跳过空章节的行为，可从 `X-Export-Warnings` 读取提示。人物、大纲和关系等返回当前保存的设定，可能包含后续剧情；组织的 `member_count` 包含全部成员状态。

Key 的生命周期独立于网页登录：登出、修改或重置密码不会自动撤销 Key；到期、显式撤销、账号禁用或删除后不能访问。撤销永久生效，不能重新启用或再次显示，需要时创建新 Key。完整密钥加密保存，部署时应保留原 `ENCRYPTION_KEY`，更换后旧密钥可能无法再次显示，可撤销并重建。

开放业务接口默认每 Key 每分钟最多 120 次，通过 `OPEN_API_RATE_LIMIT_PER_MINUTE` 调整。超额返回 `429` 和 `Retry-After`；缺失 / 无效 Key 返回 `401`，未授权或不存在的小说 / 子资源返回 `404`，参数错误返回 `422`。该额度不影响原网页业务接口。

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
- 尚未实现：伏笔管理、关系图谱、提示词工坊、拆书、OAuth 登录、向量检索
