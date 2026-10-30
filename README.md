# AI 小说平台 · aiNovel

aiNovel 是一个多用户、自部署的 AI 辅助小说创作平台，覆盖从灵感立项、世界观与角色设计、分卷大纲、章节写作，到分析修订、阅读和导出的完整流程。

平台将故事设定、正文、知识库和剧情记忆放在同一作品中管理。你可以手动写作，也可以配置自己的模型接口，让 AI 生成、续写、润色或重写内容，并通过版本记录、分析建议和变更确认持续调整作品。

支持 OpenAI 兼容接口、Anthropic Claude、Google Gemini，以及通过兼容接口接入的本地模型。模型服务和 API Key 由用户自行配置；部署平台本身不会附带可用的模型额度。

## 目录

- [功能概览](#功能概览)
- [技术架构](#技术架构)
- [Docker 部署](#docker-部署)
- [本地开发](#本地开发)
- [模型接入](#模型接入)
- [创作流程](#创作流程)
- [环境变量](#环境变量)
- [账号与权限](#账号与权限)
- [开放 API](#开放-api)
- [运维与数据备份](#运维与数据备份)
- [开发与测试](#开发与测试)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [当前限制](#当前限制)

## 功能概览

| 模块 | 已实现能力 |
| --- | --- |
| 作品管理 | 作品书架、简介、主题、类型标签、目标字数、创作状态、继续写作；上传封面或使用图片模型生成封面 |
| 灵感立项 | 从创意出发逐步完善书名、简介、主题、标签、卖点、受众和创作规模，并转为作品 |
| 创作向导 | 分步生成世界观、职业体系、角色和分卷大纲，支持调整参数、重新生成和手动编辑 |
| 故事档案 | 世界观条目、角色卡、组织层级与成员、人物关系及关系图谱、职业阶段与角色职业记录 |
| 大纲与细纲 | 分卷大纲、AI 扩展与续写、章节细纲、大纲快照及导入导出 |
| 章节写作 | 流式生成、续写、润色、重写、手动编辑、上下文预览、生成记录、正文差异比较和版本恢复 |
| 批量创作 | 批量生成章节，查看任务进度、阶段和结果，通过任务面板管理后台任务 |
| 章节分析 | 提取剧情、冲突、情绪等信息，提供评分与修改建议；查看分析历史并确认设定变更 |
| 伏笔管理 | 管理伏笔、关联角色与章节，查看状态、变更历史和时间线 |
| 知识库 | 导入参考文档、查看分块、检索调试、按需注入写作上下文；支持关键词与向量混合检索 |
| 剧情记忆 | 保存和检索剧情记忆，与知识库共用检索基础设施，为后续写作提供上下文 |
| 提示词模板 | 查看和自定义模板，预览渲染结果、比较版本、恢复版本及导入导出 |
| 写作风格 | 内置风格、自定义风格、从样文提取风格和试写；支持作品默认风格与章节风格 |
| 提示词工坊 | 分享和导入创作资源，支持投稿与管理员审核 |
| 写作技能 | 使用 `SKILL.md` 与参考资料组织写作规则，支持技能管理、导入导出及智能体使用 |
| 项目智能体 | 通过对话读取作品资料、调用工具、提出内容变更，由用户审核后应用相应修改 |
| MCP 插件 | 接入远程 Streamable HTTP / SSE 工具服务，管理工具启用状态和确认设置 |
| 拆书导入 | 导入已有书稿、识别和校正章节、创建作品，并使用后台任务反推创作设定 |
| 阅读与迁移 | 全屏阅读、阅读进度；导出 TXT / Markdown / EPUB；导出和导入作品 JSON 备份 |
| 多用户与管理 | 本地注册登录、用户数据隔离、自助改密，以及管理员用户管理 |
| 平台开放 API | 管理平台访问密钥，按作品授权只读访问小说、章节和设定，提供开发指南与 OpenAPI 文档 |

## 技术架构

| 层次 | 技术与职责 |
| --- | --- |
| 前端 | React 18、TypeScript、Vite、Ant Design 5、Zustand、React Router；关系图谱使用 React Flow 与 Dagre |
| 后端 | Python 3.12、FastAPI、Pydantic、SQLAlchemy 2 异步 ORM、Alembic 数据库迁移 |
| 数据库 | Docker 部署使用 PostgreSQL 16；本地开发与测试可使用 SQLite + aiosqlite |
| 模型适配 | OpenAI 兼容、Anthropic、Google Gen AI；分别配置文本、图片和向量模型 |
| 后台任务 | 数据库持久化任务队列，进程内异步调度；通过 SSE 推送进度与输出 |
| 检索 | BM25 关键词检索、向量相似度和混合排序；向量保存在应用数据库中，使用 NumPy 计算 |
| 测试与检查 | 后端 pytest / Ruff；前端 Vitest / ESLint / TypeScript |

```mermaid
flowchart LR
    Browser[浏览器] --> Web[React 前端]
    Web -->|HTTP / SSE| API[FastAPI 后端]
    API --> DB[(PostgreSQL)]
    API --> Tasks[进程内任务引擎]
    Tasks --> DB
    Tasks --> Models[文本 / 图片 / 向量模型]
    API --> MCP[远程 MCP 工具]
```

Docker 构建会先编译前端，再将构建产物放入后端镜像，由 FastAPI 统一提供页面和 API，因此默认只需暴露一个应用端口。当前部署无需额外启动 Redis、Celery 或独立向量数据库。

**后台任务引擎目前要求单进程运行。** 请保持 Dockerfile 默认的 Uvicorn 启动方式，不要添加多个 `--workers`，也不要让多个应用实例共享同一数据库调度任务。详见[当前限制](#当前限制)。

## Docker 部署

### 1. 准备环境

需要安装 Docker，并确保可以使用 `docker compose` 命令。以下命令均在项目根目录执行；生成密钥的示例另外使用本机 Python 3。

```bash
cp .env.example .env
```

### 2. 填写配置

打开根目录 `.env`，至少替换以下三个值。示例文件中的 `change-me` 不能直接用于启动：

| 变量 | 填写要求 |
| --- | --- |
| `POSTGRES_PASSWORD` | 数据库密码；当前 Compose 直接拼接连接串，请仅使用字母、数字、`-`、`_` |
| `JWT_SECRET` | 随机签名密钥，至少 16 个字符 |
| `ENCRYPTION_KEY` | 合法的 Fernet 密钥，用于加密模型凭据、平台 API Key 和 MCP 请求头等敏感信息 |

可以分别运行下面的命令生成，再将输出填入 `.env` 对应字段：

```bash
# POSTGRES_PASSWORD
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# JWT_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'

# ENCRYPTION_KEY：32 字节随机数据的 URL-safe Base64 编码
python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
```

如果需要管理员，可同时填写 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`。如果只供自己或固定成员使用，也可以设置 `REGISTRATION_ENABLED=false`，通过管理员创建账号。

请保管好 `.env`，尤其是 `ENCRYPTION_KEY`。后续更新、迁移或恢复数据库时需要继续使用原来的加密密钥。

### 3. 构建并启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app
```

首次启动会拉取镜像、安装依赖并构建前端。数据库健康检查通过后，应用会先执行 `alembic upgrade head`，再启动后端。

默认访问地址：

| 地址 | 用途 |
| --- | --- |
| [http://localhost:8000](http://localhost:8000) | 平台首页 |
| [http://localhost:8000/healthz](http://localhost:8000/healthz) | 应用进程健康检查，正常返回 `{"status":"ok"}` |
| [http://localhost:8000/developers](http://localhost:8000/developers) | 面向接入方的开发指南 |
| [http://localhost:8000/api/open/v1/docs](http://localhost:8000/api/open/v1/docs) | 平台开放 API 的 Swagger UI |

若修改了 `APP_PORT`，请相应替换上述端口。`/healthz` 只确认应用能够响应请求，不会检查数据库或模型服务的连通性。

### 4. 开始使用

注册账号，或使用配置的管理员账号登录，然后进入「模型配置」添加并测试一个文本模型。接下来新建作品，按照[创作流程](#创作流程)完成设定与章节写作。

## 本地开发

### 环境要求

| 工具 | 要求 |
| --- | --- |
| Python | 3.12；后端声明的范围为 `>=3.12,<3.13` |
| uv | 用于安装后端依赖和运行 Python 命令 |
| Node.js | 建议使用 22.x，至少 22.13；前端锁定依赖的 `engines` 约束决定实际兼容范围 |
| npm | 用于安装前端依赖及执行脚本 |
| 数据库 | 快速体验可使用 SQLite；需要贴近 Docker 环境时使用 PostgreSQL |

### 1. 安装后端依赖

从项目根目录执行：

```bash
cd backend
cp .env.example .env
uv sync --frozen
```

编辑 `backend/.env`，填写 `JWT_SECRET` 和 `ENCRYPTION_KEY`。可以使用前面介绍的密钥生成命令，也可以在安装依赖后运行：

```bash
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

本地开发与 Docker 使用不同的配置文件：本地后端从当前工作目录读取 `.env`，因此后端命令应在 `backend/` 下运行。

### 2. 选择数据库并启动后端

**使用 SQLite 快速启动：** 将 `backend/.env` 中的数据库地址改为：

```dotenv
DATABASE_URL=sqlite+aiosqlite:///./dev.db
```

`uv sync --frozen` 默认包含开发依赖，其中包括 SQLite 所需的 `aiosqlite`；生产依赖的 `--no-dev` 安装方式不包含该驱动。数据库文件会保存在 `backend/dev.db`。

**使用 PostgreSQL：** 先准备可连接的 PostgreSQL 实例，再填写实际连接信息，例如：

```dotenv
DATABASE_URL=postgresql+asyncpg://ainovel:your_password@localhost:5432/ainovel
```

仓库默认 Compose 中的数据库只供容器网络访问，没有向宿主机发布 `5432` 端口；仅运行 `docker compose up -d db`，宿主机上的后端不能直接通过 `localhost:5432` 连接该容器。

配置好数据库后，在 `backend/` 下执行：

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

本地启动不会自动执行迁移，新建数据库或更新代码后都需要先运行 `alembic upgrade head`。

### 3. 启动前端

另开一个终端，从项目根目录执行：

```bash
cd frontend
npm ci
npm run dev
```

访问 [http://localhost:5173](http://localhost:5173)。Vite 默认将 `/api` 代理到 `http://localhost:8000`，包括流式任务接口。

如果后端使用其他地址，可在启动前端时指定代理目标：

```bash
VITE_BACKEND_URL=http://127.0.0.1:9000 npm run dev
```

开发时优先通过 Vite 页面访问 API。需要浏览器直接跨域访问后端时，可配置 `CORS_ORIGINS`；它是允许的完整来源地址列表，不是后端监听地址。

## 模型接入

登录后进入「模型配置」，填写名称、接口提供方、模型标识、API Key 和可选 Base URL，再执行连通性测试。模型标识应以所使用服务实际提供的名称为准。

### 接口与能力

| 类型 | 用途 | 接入说明 |
| --- | --- | --- |
| 文本模型 | 创作向导、章节生成、分析、灵感和对话等 | 支持 OpenAI 兼容、Anthropic、Gemini；项目智能体另外要求模型支持工具调用 |
| 图片模型 | AI 生成作品封面 | 使用支持图片生成的 OpenAI 兼容或 Gemini 接口 |
| 向量模型 | 知识库与剧情记忆的语义检索 | 使用支持 embedding 的 OpenAI 兼容或 Gemini 接口；Anthropic 适配器不提供此能力 |

同一接口地址不一定同时提供文本、图片、向量和工具调用能力。模型配置中的上下文大小、最大输出和思考等级也需要与供应商支持的参数一致；其中 `off` 表示不发送额外思考参数。

使用官方接口时，Base URL 可以留空以使用对应 SDK 的默认地址。使用中转或其他兼容服务时，填写服务商提供的 API 根地址；OpenAI 兼容客户端会在其后构造 `/chat/completions` 等请求路径，通常不应填写完整的单个接口路径。

向量模型不是普通写作的前置条件。未配置向量模型、索引尚未完成或查询向量生成失败时，检索会退回关键词模式并给出提示。更换默认向量模型后，需要在作品中重建索引。

### 接入 Ollama 等本地模型

默认禁止模型地址指向回环、内网、链路本地等非公网地址，并在受限模式下禁止跟随重定向，以降低 SSRF 风险。接入受信任的本地或内网模型时：

1. Docker 部署修改根目录 `.env`，本地开发修改 `backend/.env`，设置 `ALLOW_PRIVATE_AI_ENDPOINTS=true`。
2. Docker 运行 `docker compose up -d` 使环境变量生效；本地开发重启后端。
3. 在「模型配置」选择 OpenAI 兼容接口，填写对应模型和 Base URL。

| 运行方式 | 本机 Ollama 的 Base URL 示例 |
| --- | --- |
| 后端直接运行在宿主机 | `http://localhost:11434/v1` |
| 后端运行在本项目 Docker 容器中 | `http://host.docker.internal:11434/v1` |

容器内的 `localhost` 指向容器自身。Compose 已配置宿主机网关映射，但仍需确保 Ollama 的监听地址、防火墙和网络允许容器连接。对于无需鉴权的本地 OpenAI 兼容服务，API Key 可以留空。

该开关是部署级配置，开放注册并部署在公网时请保持 `false`。MCP 是另一项独立的外部工具能力，可通过 `MCP_ENABLED=false` 整体关闭。

## 创作流程

1. **配置文本模型。** 添加模型并完成连通性测试，再将常用模型设为默认。
2. **建立作品。** 可以直接新建，也可以从「灵感立项」整理创意后转为作品；已有书稿可使用「拆书导入」。
3. **完善故事档案。** 按向导生成世界观、职业、角色和大纲，检查结果后再补充组织、人物关系等设定。
4. **细化大纲与章节计划。** 组织分卷和章节细纲，明确本章事件、角色与目标，必要时保存大纲快照。
5. **生成或手写正文。** 在章节编辑器中检查上下文，选择模型与写作风格，进行生成、续写、润色或重写；多个章节可使用批量生成。
6. **分析与修订。** 查看章节分析、评分和建议，确认需要写回的设定变化，维护伏笔与剧情记忆，并通过版本比较审阅正文修改。
7. **阅读、导出与备份。** 在阅读器中检查成稿，导出需要的格式，同时保存作品 JSON 备份或数据库备份。

需要长期参考的资料可导入「知识库」，将常用写作要求保存为提示词模板、写作风格或写作技能。项目智能体适合以对话方式查询资料和提出变更；应用修改前应检查提案内容。

AI 生成结果需要人工审阅。自动章节分析通常会增加一次模型调用，仅在部署级 `AUTO_ANALYZE_ENABLED` 与作品的「自动分析」都开启时自动排队；手动分析和一键分析不受这个部署开关影响。

## 环境变量

配置模板见根目录 [`.env.example`](.env.example) 和后端 [`backend/.env.example`](backend/.env.example)。下表的默认值指程序或 Compose 的默认行为，示例中的占位密钥必须替换。

### 数据库、凭据与访问

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` | `ainovel` | Compose 中的数据库用户名 |
| `POSTGRES_PASSWORD` | 必填 | Compose 中的数据库密码，不应保留示例值 |
| `POSTGRES_DB` | `ainovel` | Compose 中的数据库名称 |
| `DATABASE_URL` | 本地 PostgreSQL 地址 | 后端 SQLAlchemy 异步连接串；Compose 自动构造，本地开发在 `backend/.env` 填写 |
| `JWT_SECRET` | 必填 | 登录令牌签名密钥，至少 16 个字符 |
| `ENCRYPTION_KEY` | 必填 | Fernet 加密主密钥；缺失或格式非法时拒绝启动 |
| `REGISTRATION_ENABLED` | `true` | 是否开放自主注册；不影响管理员创建账号或启动时引导管理员 |
| `ADMIN_USERNAME` | 空 | 启动时引导的管理员用户名，须与密码同时填写或同时留空 |
| `ADMIN_PASSWORD` | 空 | 仅在新建引导管理员时设置密码，不会覆盖已有账号的密码 |
| `COOKIE_SECURE` | `false` | 使用 HTTPS 访问时设为 `true`，让登录 Cookie 带 Secure 标记 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` | 登录有效期，单位分钟，默认 7 天，必须大于 0 |
| `OPEN_API_RATE_LIMIT_PER_MINUTE` | `120` | 每把平台 API Key 每分钟的开放接口请求上限，必须大于 0 |
| `CORS_ORIGINS` | 空 | 后端允许的跨域来源，逗号分隔；后端示例文件预填本地开发来源 |
| `APP_PORT` | `8000` | Compose 发布到宿主机的端口，不会改变容器内监听端口 |
| `FRONTEND_DIST_DIR` | `frontend/dist` 的绝对路径 | 后端托管的前端构建目录；不存在时只提供 API |

### 模型与后台任务

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ALLOW_PRIVATE_AI_ENDPOINTS` | `false` | 是否允许模型接口访问本地 / 内网地址 |
| `CONTEXT_CHAR_BUDGET` | `12000` | 章节上下文字符预算，最小 1000；这是字符预算，不是模型 token 上限 |
| `AI_REQUEST_TIMEOUT` | `300` | 单次模型请求超时秒数，必须大于 0，不是整个多步任务的总时限 |
| `TASK_MAX_CONCURRENCY` | `4` | 后台任务全局并发上限，最小 1，其余任务排队 |
| `TASK_MAX_PER_USER` | `2` | 单用户后台任务并发上限，最小 1，仍受全局上限约束 |
| `AUTO_ANALYZE_ENABLED` | `true` | 是否允许生成、润色或重写成功后自动排队章节分析 |
| `MCP_ENABLED` | `true` | 是否开放远程 MCP 插件能力；关闭后插件接口返回 `403`，智能体不暴露 MCP 工具 |

根目录 `.env` 用于 Compose 变量替换，**不会自动把任意新增变量传入容器**。当前 Compose 已转发模型与任务配置；如需在 Docker 中使用 `CORS_ORIGINS`、`FRONTEND_DIST_DIR` 等未列入 `app.environment` 的变量，还需要同步修改 `docker-compose.yml` 的环境变量映射。

## 账号与权限

### 普通用户

用户可管理自己的作品、模型配置和创作资源，并在账号菜单中修改密码。用户名须为 3–32 位且不含空白；密码须为 6–72 **字节**，中文等多字节字符需要按 UTF-8 字节数计算。

作品数据按用户隔离，管理员身份不会额外获得读取其他用户小说内容或显示其他用户平台密钥的权限。

### 管理员引导与用户管理

在 `.env` 中同时设置 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 后，服务启动时会执行管理员引导：

- 账号不存在：创建为管理员，不受 `REGISTRATION_ENABLED` 影响。
- 账号已存在：确保其为启用中的管理员，**不会覆盖原密码**。此后修改 `.env` 中的 `ADMIN_PASSWORD` 不能重置该账号密码。
- 只设置用户名或密码之一，或者不符合注册规则：启动配置校验失败。

管理员登录后可在账号菜单进入「用户管理」，搜索和分页查看用户，创建账号、重置密码、禁用 / 启用、调整管理员身份，以及删除用户。删除用户需要输入用户名确认，并会连同其作品、章节和模型配置一起删除。

- 管理员不能禁用、降级或删除自己，系统始终至少保留一名启用中的管理员。
- 禁用账号或管理员重置密码会使该用户原有登录会话失效；用户自助改密会使其他会话失效，当前会话会获得新令牌并保持登录。
- 忘记管理员密码时，可由另一名管理员重置；没有其他管理员时，可以临时改用新的 `ADMIN_USERNAME` 和配套密码重启，创建另一个管理员后进行恢复。
- 如需关闭自主注册，请先配置可登录的管理员，再设置 `REGISTRATION_ENABLED=false`，后续通过「用户管理」创建账号。

## 开放 API

第三方程序使用 `/api/open/v1` 读取授权内容；`/api/v1` 是平台网页使用的内部业务接口。开放接口与网页登录采用不同的认证方式。

### 创建和管理密钥

登录后进入「API 密钥」，创建一把平台访问密钥。可以只授权指定小说，也可以选择本人的全部小说（包含之后新增的小说）；默认有效期 90 天，可指定到期时间或选择不过期。

升级已有实例时，先应用新增数据库迁移并重启后端；本地在 `backend/` 运行 `uv run alembic upgrade head`，Docker 启动命令会自动执行迁移。

密钥默认显示掩码，创建后仍可点击眼睛图标显示 / 隐藏，并通过复制按钮复制完整值。仅密钥所有者能够查看，管理员没有额外的查看权限。平台密钥用于读取本平台内容，与「模型配置」中填写的模型供应商 API Key 分别管理。

### 文档和调用示例

文档与描述文件（在当前部署地址后加路径）：

- `/developers`：面向接入客户的公开开发指南，首页顶部“开发文档”可进入；包含快速开始、鉴权、接口目录、分页和错误处理，沿用官网界面风格。
- `/api/open/v1/docs`：Swagger UI，可填写 `X-API-Key` 在线调试。
- `/api/open/v1/openapi.json`：独立 OpenAPI JSON，可下载或导入调用工具。

文档可以直接浏览；实际读取需要在请求头中提供 `X-API-Key`。网页登录 Cookie 或 JWT 不能替代平台 Key，平台 Key 也不能用来调用内部管理或写入接口。

```bash
export AINOVEL_API_KEY='替换为页面生成的完整平台密钥'

# 检查当前 Key 的信息和授权范围
curl --fail --show-error \
  -H "X-API-Key: ${AINOVEL_API_KEY}" \
  'http://localhost:8000/api/open/v1/me'

# 分页读取已授权小说
curl --fail --show-error \
  -H "X-API-Key: ${AINOVEL_API_KEY}" \
  'http://localhost:8000/api/open/v1/novels?page=1&page_size=20'
```

### 接口目录

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

例如下载一本小说的 EPUB，可先从列表取得小说 ID，再替换下面示例中的 `1`：

```bash
curl --fail --show-error \
  -H "X-API-Key: ${AINOVEL_API_KEY}" \
  -D export-headers.txt \
  -o novel.epub \
  'http://localhost:8000/api/open/v1/novels/1/export?format=epub'
```

### 分页、同步与内容边界

- 列表返回 `{items, total, page, page_size}`，页码从 1 开始，默认每页 20 条、最多 100 条。
- 章节可用带时区的 `updated_after` 筛选严格晚于指定时间的章节行更新；删除事件、人物改名和关联大纲等派生字段变化不属于完整增量同步，需要定期完整读取并比对。
- 小说 `updated_at` 表示元数据更新时间，`content_updated_at` 表示现存章节的最近更新时间，不能直接视为全部内容的统一变更版本。
- 章节目录包含空章节、草稿和生成中章节，正文可能为空；全文导出会跳过空章节和生成中章节。EPUB 导出提示可从 UTF-8 百分号编码的 `X-Export-Warnings` 响应头读取。
- 人物、大纲和关系等接口返回当前保存的设定，可能包含后续剧情，不会按阅读章节回溯历史状态。
- 组织的 `member_count` 包含全部成员状态，不仅是当前活跃成员。

### 密钥生命周期与限流

Key 的生命周期独立于网页登录：登出、修改或重置密码不会自动撤销 Key；到期、显式撤销、账号禁用或删除后不能访问。撤销永久生效，不能重新启用或再次显示，需要时创建新 Key。完整密钥加密保存，部署时应保留原 `ENCRYPTION_KEY`，更换后旧密钥可能无法再次显示，可撤销并重建。

开放业务接口默认每 Key 每分钟最多 120 次，通过 `OPEN_API_RATE_LIMIT_PER_MINUTE` 调整。限流采用固定分钟窗口，该额度不影响原网页业务接口。

| 状态码 | 常见原因 | 处理方式 |
| --- | --- | --- |
| `401` | Key 缺失、无效、过期、已撤销，或账号不可用 | 检查 `X-API-Key`、有效期和账号状态 |
| `404` | 小说或子资源不存在，或未授权访问 | 检查小说 ID、子资源归属与 Key 授权范围 |
| `422` | 分页、筛选或其他参数不符合要求 | 按 OpenAPI 文档调整参数 |
| `429` | 当前 Key 达到请求上限 | 按 `Retry-After` 响应头中的秒数等待后重试 |

## 运维与数据备份

### 常用管理命令

以下命令在项目根目录执行：

```bash
# 查看容器状态和应用日志
docker compose ps
docker compose logs -f --tail=100 app

# 更新配置后重新创建必要的容器
docker compose up -d

# 更新源码后重新构建并启动，入口会自动执行数据库迁移
docker compose up -d --build

# 停止服务并删除容器，保留默认命名数据卷
docker compose down
```

`docker compose restart` 不会重新读取 Compose 环境变量。修改根目录 `.env` 后应使用 `docker compose up -d`。

PostgreSQL 数据保存在命名卷 `pgdata` 中，实际卷名通常带有 Compose 项目前缀。**`docker compose down -v` 会删除数据卷和数据库内容**，日常停止或升级不要使用这个参数。

更新前应备份数据库和原配置。数据库迁移不等于完整回滚机制；需要回退版本时，应先确认代码与数据库结构兼容，必要时恢复配套备份。

### HTTPS 与反向代理

公网部署时，可在应用前设置 HTTPS 反向代理，并将 `COOKIE_SECURE=true`。代理需正确转发 `Host`、`X-Forwarded-For` 和 `X-Forwarded-Proto`，应用默认启动参数已启用 `--proxy-headers`。

章节生成、分析和智能体任务使用 SSE，反向代理应关闭响应缓冲和缓存，并为长连接设置足够的读取超时；否则可能出现后端持续生成、页面直到结束才一次性显示结果的情况。

上传请求还受反向代理自身的请求体限制影响。配置代理时，应结合下文的文件限制和 multipart 开销设置允许大小，而不是只调整应用配置。

### 作品级备份与迁移

在作品管理界面导出 `ainovel.project` 格式的 JSON 备份，可用于跨实例迁移或保存单部作品。备份包含正文和主要故事设定，并可按选项携带章节版本、大纲快照、生成记录和灵感会话等数据；导入会创建新作品并重映射内部 ID。

作品备份不包含用户账号、模型配置与凭据、平台访问密钥、智能体会话和后台任务。检索向量属于派生数据，不随备份迁移；导入后需要自行配置模型，并按提示重建索引。

TXT / Markdown / EPUB 用于阅读和发布，不能替代保存创作结构的作品 JSON 备份；作品 JSON 备份也不能替代整个平台的数据库备份。

### 数据库备份

以下命令从 Compose 数据库容器导出 PostgreSQL 自定义格式备份，不会把数据库密码写入命令行。文件保存在宿主机 `backups/` 下：

```bash
mkdir -p backups
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "backups/ainovel-$(date +%Y%m%d-%H%M%S).dump"
```

请同时在安全位置保存该数据库对应的 `.env` 或等效配置，尤其是原 `ENCRYPTION_KEY`。仅恢复数据库而丢失加密密钥，无法解密原有模型凭据等敏感信息。

恢复时应先备份目标数据库，并确认备份文件和目标实例。下面的 `--clean` 会删除备份中对应的已有数据库对象，再恢复数据；请将示例文件名替换为真实备份：

```bash
docker compose stop app
docker compose up -d --wait db
docker compose exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --clean --if-exists --no-owner --no-privileges' \
  < backups/ainovel-YYYYMMDD-HHMMSS.dump
```

确认恢复命令成功后，再启动应用：

```bash
docker compose up -d app
```

`--wait` 会等待数据库健康检查通过；若 Compose 版本不支持该选项，可先启动数据库，确认健康后再恢复。SQLite 开发实例应在停止后端后备份相应数据库文件，并保留相同的加密配置。

## 开发与测试

### 后端命令

在 `backend/` 目录下执行：

| 命令 | 作用 |
| --- | --- |
| `uv sync --frozen` | 按锁文件安装运行和开发依赖 |
| `uv run uvicorn app.main:app --reload` | 启动开发服务 |
| `uv run alembic upgrade head` | 将数据库迁移到最新版本 |
| `uv run alembic current` | 查看当前数据库迁移版本 |
| `uv run ruff check .` | 执行 Python 代码检查 |
| `uv run pytest` | 运行后端测试 |

### 前端命令

在 `frontend/` 目录下执行：

| 命令 | 作用 |
| --- | --- |
| `npm ci` | 按 `package-lock.json` 安装依赖 |
| `npm run dev` | 启动 Vite 开发服务器，默认端口 5173 |
| `npm run lint` | 执行 ESLint 检查 |
| `npm run build` | 先执行 TypeScript 构建检查，再生成 `dist/` |
| `npm test` | 使用 Vitest 运行前端测试 |
| `npm run preview` | 本地预览构建产物；需要另行提供 API 服务 |

前后端均使用锁文件固定依赖。新增或修改功能时，请按影响范围执行检查和测试；项目约定入口为 [AGENTS.md](AGENTS.md) 与 [`.trellis/spec/`](.trellis/spec/)。

如需检查生产页面的同源托管效果，可以先在 `frontend/` 执行 `npm run build`，再重启后端并访问 8000 端口。后端只在构建目录存在且含 `index.html` 时挂载前端。

## 目录结构

```text
aiNovel/
├── README.md
├── AGENTS.md                    # 项目协作与开发约定入口
├── .env.example                 # Docker Compose 配置模板
├── docker-compose.yml           # PostgreSQL 与应用服务
├── Dockerfile                   # 前端构建 + Python 后端运行镜像
├── backend/
│   ├── .env.example             # 本地后端配置模板
│   ├── pyproject.toml           # Python 依赖、测试与 Ruff 配置
│   ├── uv.lock
│   ├── alembic.ini
│   ├── alembic/versions/        # 数据库迁移
│   ├── app/
│   │   ├── main.py              # 应用入口、生命周期、静态页面与健康检查
│   │   ├── api/v1/              # 网页内部业务接口
│   │   ├── api/open/            # 平台只读开放 API 与独立文档
│   │   ├── core/                # 配置、数据库、认证、安全与错误处理
│   │   ├── models/              # SQLAlchemy 模型
│   │   ├── schemas/             # Pydantic 请求与响应结构
│   │   ├── ai/providers/        # 模型提供方适配
│   │   ├── ai/prompts/          # 提示词注册与 Jinja2 模板
│   │   ├── services/            # 创作、检索、导入导出等业务逻辑
│   │   │   ├── task_engine/    # 后台调度与任务处理器
│   │   │   ├── agent/          # 项目智能体与变更确认
│   │   │   ├── mcp/            # 远程 MCP 服务接入
│   │   │   ├── book_import/    # 书稿解析、切章与设定反推
│   │   │   ├── project_transfer/ # 作品 JSON 备份与恢复
│   │   │   └── open_api/       # 开放读取服务
│   │   └── skills/             # 内置写作技能资源
│   └── tests/
├── frontend/
│   ├── package.json
│   ├── package-lock.json
│   ├── vite.config.ts          # 开发代理、构建和 Vitest 配置
│   └── src/
│       ├── api/                # API 客户端、类型与 SSE 协议
│       ├── components/         # 共享组件与功能组件
│       ├── pages/              # 页面及作品各功能面板
│       ├── hooks/              # 数据加载、任务订阅与页面状态
│       ├── stores/             # Zustand 状态管理
│       ├── theme/              # 主题与配色
│       ├── utils/              # 纯函数和前端辅助逻辑
│       └── router.tsx          # 路由与页面懒加载
└── .trellis/                   # 开发流程、编码规范与任务记录
```

## 常见问题

### 应用启动后立即退出

先查看 `docker compose logs --tail=100 app`。常见原因是 `JWT_SECRET` 太短、`ENCRYPTION_KEY` 不是合法 Fernet 密钥、管理员用户名和密码未成对配置，或数据库连接 / 迁移失败。

根目录 `.env.example` 的 `change-me` 只是占位符；本地 `backend/.env.example` 的 `ENCRYPTION_KEY` 默认留空，也必须自行填写。

### 本地访问 8000 端口没有页面

本地后端在没有 `frontend/dist/index.html` 时只提供 API。开发时访问 [http://localhost:5173](http://localhost:5173)；需要同源页面时先构建前端，再重启后端。可通过 [http://localhost:8000/healthz](http://localhost:8000/healthz) 判断后端是否启动。

### 本地模型连接失败或被拒绝

检查 `ALLOW_PRIVATE_AI_ENDPOINTS` 是否开启并已重启服务，模型服务是否允许对应来源连接，以及是否误将容器里的 `localhost` 当作宿主机。容器访问宿主机的地址示例见[模型接入](#模型接入)。

### 模型测试成功，但生成、封面或智能体失败

连通性测试不能保证所有能力都可用。请分别检查模型是否支持目标任务、工具调用或图片 / 向量接口，模型标识是否正确，额度是否充足，以及最大输出、思考参数和超时是否与接口兼容。

### 正文生成没有实时输出

检查浏览器网络请求和反向代理的 SSE 缓冲、压缩及超时设置。任务在后端运行，关闭页面或断开订阅不会自动取消任务，可重新进入页面或任务面板查看。

若服务曾中断且章节仍显示「生成中」，先确认没有活动任务，再按编辑器提示使用「复位」恢复草稿状态。

### 知识库只使用关键词检索

检查是否配置了默认向量模型、索引是否已完成，以及是否更换过向量模型。没有可用向量或生成失败时会降级到关键词检索；在知识库 / 剧情记忆界面查看索引状态与提示，再决定是否重建。

### 上传文件失败

| 类型 | 应用支持范围 |
| --- | --- |
| 知识库文档 | TXT、Markdown、JSON、DOCX、EPUB、PDF，单文件最多 20 MiB；PDF 需要文字层，不提供扫描件 OCR |
| 拆书导入 | TXT、Markdown、DOCX、EPUB，单文件最多 20 MiB；不支持直接拆书导入 PDF |
| 作品备份导入 | 平台作品 JSON 备份，最多 100 MiB |
| 写作技能导入 | `SKILL.md` 或包含技能与 Markdown 参考资料的 ZIP，最多 2 MiB |
| 作品封面 | JPEG、PNG、WebP，最多 10 MiB |

文件扩展名和实际内容需要符合要求；还应检查反向代理的上传上限。拆书的未导入解析记录最多保留 5 份，创建满 7 天后会按过期规则清理。

### 修改管理员密码配置没有生效

`ADMIN_PASSWORD` 只用于新建引导账号，不会覆盖已有密码。已有账号请通过「修改密码」操作，或由其他管理员在「用户管理」中重置。

### 修改数据库密码后连接失败

已有数据库卷不会因修改 `.env` 自动重新初始化。需要同步修改数据库内的用户密码与应用连接配置；不要通过删除数据卷来解决密码不一致。

### 开放接口提示 401 或 404

确认使用的是「API 密钥」页面生成的平台 Key，并通过 `X-API-Key` 请求头发送。模型供应商的 Key、网页登录 Cookie 和 JWT 都不能替代它。返回 `404` 时还要检查作品是否在授权范围内，以及章节等子资源是否属于该作品。

## 当前限制

- **单进程任务调度。** 多个 Uvicorn worker 或多个实例会共享任务状态，而启动恢复逻辑会将遗留的运行中任务标记为中断，当前不支持这种部署方式。
- **运行中的模型调用不能跨进程恢复。** 服务重启后，排队任务会继续被调度，原运行中任务会标记为中断；任务输出快照不代表能够从同一 token 位置继续生成。
- **外部工具需要独立服务。** MCP 目前接入远程 Streamable HTTP / SSE，不提供本机 stdio 插件进程托管。
- **认证方式为本地账号。** 当前未提供 OAuth 第三方登录。
- **阅读同步接口存在内容边界。** 开放 API 提供当前设定及章节行更新筛选，不提供完整删除事件流或按章节回溯的历史设定。

## 参考项目

功能对标 [MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) 的核心创作闭环，本项目自行实现，未复用其代码。具体能力、配置和行为以当前仓库实现为准。
