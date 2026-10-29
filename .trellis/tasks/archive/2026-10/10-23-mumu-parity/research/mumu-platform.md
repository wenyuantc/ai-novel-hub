# Research: MuMuAINovel 平台、账户与周边功能

- **Query**: 调研 MuMuAINovel 的平台/账户/周边功能（auth 含 LinuxDO OAuth、users、admin、announcements、changelog、projects 含导入导出、common、middleware、constants、utils、alembic 双库、Docker 部署；前端 Login、AuthCallback、UserManagement、BookshelfPage、ProjectList、Sponsor、主题、移动端、全局布局导航），并总结技术栈与架构约定
- **Scope**: 外部代码库（/Users/wenyuantc/IdeaProjects/my/MuMuAINovel，GPL v3，仅调研功能与交互，不复制代码）
- **Date**: 2026-10-23
- 路径约定：下文 `B/` = `MuMuAINovel/backend/`，`F/` = `MuMuAINovel/frontend/src/`

---

## 0. 整体技术栈与架构约定

| 维度 | MuMu 的做法 | 证据 |
|---|---|---|
| 后端框架 | FastAPI 0.121 + Uvicorn，全部异步；Pydantic v2 + pydantic-settings（`.env`，大小写不敏感，`extra=ignore`） | `B/requirements.txt`、`B/app/config.py` |
| ORM | SQLAlchemy 2.0 async（`declarative_base`），模型在 `B/app/models/`，Schema 在 `B/app/schemas/` | `B/app/database.py:15-28` |
| 数据库 | 默认 PostgreSQL（asyncpg）；也支持 SQLite（aiosqlite，启用 WAL、busy_timeout 30s、NullPool）。通过 `DATABASE_URL` 是否包含 `sqlite` 自动切换 | `B/app/database.py:49-146`、`B/app/config.py:19` |
| 多用户隔离 | 所有用户共用一个库，靠业务表的 `user_id` 字段隔离。`get_engine(user_id)` 的参数其实被忽略，全局只缓存一个引擎 `shared_postgres` | `B/app/database.py:49-63` |
| 迁移 | Alembic，**两套独立目录**：`alembic/postgres`（`render_as_batch=False`）和 `alembic/sqlite`（`render_as_batch=True`），对应 `alembic-postgres.ini` 与 `alembic-sqlite.ini`；两边的迁移脚本要各写一份（PG 16 个，SQLite 17 个），文件名格式 `YYYYMMDD_HHMM_rev_slug`，中文 slug 也有 | `B/alembic/README`、`B/alembic/*/env.py` |
| 启动兜底 | lifespan 启动时 `BackgroundTask.__table__.create(checkfirst=True)`，并把 pending/running 的后台任务、批量生成任务、分析任务统一标成 failed（"服务重启，任务已中断"） | `B/app/main.py:57-108` |
| 任务队列 | 没有用外部队列。`BackgroundTaskService` 在进程内给每个用户建一个 `asyncio.Queue` 和一个 worker 协程：同一用户的任务串行，不同用户之间并发；任务状态、进度、取消标记、重试次数存在 `background_tasks` 表 | `B/app/services/background_task_service.py:176-247`、`B/app/models/background_task.py` |
| SSE / 流式 | 用 `StreamingResponse` 实现 SSE。消息格式是 `data: {json}\n\n`，`type` 取值 progress/chunk/result/error/done；心跳是注释行 `: heartbeat`，每 15s 发一次（`wrap_stream_with_heartbeat`）；`WizardProgressTracker` 把进度划成标准阶段：init 0-5 / loading 5-15 / preparing 15-20 / generating 20-85 / parsing 85-92 / saving 92-98 / complete 100。前端有两种客户端：`SSEClient`（EventSource，GET）和 `SSEPostClient`（fetch POST + ReadableStream） | `B/app/utils/sse_response.py`、`F/utils/sseClient.ts:26,126-174` |
| 鉴权 | Cookie 会话：`session_token` 用 HMAC-SHA256 签名，payload 为 `{uid, exp, nonce}`，base64url 编码，无状态。另有可被 JS 读取的 `session_expire_at`（httponly=False），前端靠它判断何时续期。`AuthMiddleware` 每个请求都验签，再查一次库拿用户，结果注入 `request.state.user / user_id / is_admin`；`trust_level == -1` 视为禁用 | `B/app/security.py:18-66`、`B/app/middleware/auth_middleware.py`、`B/app/api/auth.py:255-280` |
| 权限依赖 | 不同模块写法不统一：`users.py` 用 `require_login/require_admin`，`admin.py` 用 `check_admin`，`changelog.py` 自己又定义了一个 `require_login`；项目访问统一走 `common.verify_project_access`（项目不存在和无权访问都返回 404） | `B/app/api/users.py:13-24`、`B/app/api/admin.py:79-88`、`B/app/api/common.py` |
| 密码 | PBKDF2-SHA256，26 万次迭代，存储格式 `pbkdf2_sha256$iter$salt$digest`；兼容旧的无盐 SHA-256，登录成功时自动升级成新格式。密码单独存 `user_passwords` 表 | `B/app/user_password.py:35-56,122-129` |
| 中间件 | `RequestIDMiddleware`（读 `X-Request-ID` 或生成 UUID，写回响应头，并注入日志 filter）、`AuthMiddleware`、CORS（debug 模式 `*`，否则用 `cors_origins`） | `B/app/main.py:159-177` |
| 异常处理 | 全局 422 统一返回 `{"detail":"请求参数验证失败","errors":[...]}`；500 在 debug 时带 message，非 debug 时返回"请稍后重试" | `B/app/main.py:136-157` |
| 静态托管 | 后端直接托管前端 SPA：`/assets` 走 StaticFiles，其余非 `api/` 路径 fallback 到 index.html，并做了路径穿越校验；生成的封面挂在 `/generated-assets/covers`；用 `sys.frozen` 区分 PyInstaller 打包（Windows EXE）路径 | `B/app/main.py:233-299` |
| 前端 | React 18 + Vite 7 + TypeScript 5.9 + Ant Design 5 + zustand + react-router v6 + axios（`baseURL:/api`，`withCredentials`），另用 @xyflow/react + dagre 画关系图、dnd-kit 做拖拽、react-markdown | `F/../package.json`、`F/services/api.ts:83-115` |
| 提示词工坊联邦 | 由 `WORKSHOP_MODE=client/server` 决定；client 实例的公告和工坊请求会代理到云端 `WORKSHOP_CLOUD_URL`（默认 mumuverse.space）；`.instance_id` 文件保存实例 ID；代理请求通过 `X-Instance-ID` / `X-User-ID` 头鉴权 | `B/app/config.py:139-186`、`B/app/middleware/auth_middleware.py:24-44` |
| 时区 | 混用三种：auth 里用 `CHINA_TZ (UTC+8)`，公告用 `datetime.utcnow()`，其他地方用 naive 的 `datetime.now()` | `B/app/api/auth.py:26-31`、`B/app/api/announcements.py:150` |

---

## 1. 认证与登录（后端 `B/app/api/auth.py`，906 行）

### 1.1 登录方式开关 `GET /api/auth/config`
- **用户可见行为**：登录页根据返回结果决定显示哪些 Tab（本地登录、邮箱登录、邮箱注册）以及是否显示 LinuxDO 按钮。
- **返回字段**：`local_auth_enabled`、`linuxdo_enabled`（CLIENT_ID 和 SECRET 都配置了才为 true）、`email_auth_enabled`、`email_register_enabled`。
- **实现要点**：邮箱相关的开关和 SMTP 配置**优先取管理员的 Settings 行**（取 `updated_at` 最新的那个管理员），取不到再回退到 `.env`（`_get_auth_runtime_settings`，auth.py:127-170）。

### 1.2 本地账号登录 `POST /api/auth/local/login`
- **行为**：用户名（也可以填邮箱）+ 密码登录。
- **实现要点**（auth.py:347-412）：
  1. 遍历 **所有用户**，逐个查 `user_passwords.username`，匹配 `users.username` 或密码表里的 username；
  2. 匹配不到时，回退去比对 `.env` 里的 `LOCAL_AUTH_USERNAME/PASSWORD`。首次登录会创建 `local_{md5(username)[:16]}` 用户，并把 trust_level 设为 9；凡是 user_id 以 `local_` 开头的用户自动成为管理员（`user_manager.py:85-88`）。之后再登录只校验库里的密码，`.env` 里的密码不再生效。
  3. 登录成功后写 Cookie：`session_token`（httponly、samesite=lax、secure 按 `SESSION_COOKIE_SECURE` 或 `not debug` 决定）和 `session_expire_at`。
- **配置**：`LOCAL_AUTH_ENABLED/USERNAME/PASSWORD/DISPLAY_NAME`，docker-compose 默认值是 `admin/admin123`，登录页的提示文字里也直接写着这组默认账号（`F/pages/Login.tsx:323-325`）。
- `POST /api/auth/bind/login`：逻辑和上面第 1 步基本相同（"LinuxDO 授权后绑定的账号密码登录"），代码重复。

### 1.3 LinuxDO OAuth2
- **API**：`GET /api/auth/linuxdo/url` 返回 `{auth_url, state}`；`GET /api/auth/linuxdo/callback` 和 `GET /api/auth/callback`（兼容路径）共用 `_handle_callback`。
- **流程**（auth.py:629-731、`B/app/services/oauth_service.py`）：
  1. 生成 `state = token_urlsafe(32)`，存进**进程内 dict** `_state_storage`；
  2. 授权地址 `https://connect.linux.do/oauth2/authorize`，scope=read；
  3. 回调时校验 state，用 code 换 token（`/oauth2/token`），再拉用户信息（`/api/user`，带浏览器 UA，用来避开 Cloudflare）；
  4. 用户映射规则：`user_id = linuxdo_{id}`，同步 username、name、avatar_url、trust_level；`INITIAL_ADMIN_LINUXDO_ID` 指定的那个用户自动成为管理员；
  5. 如果这个用户还没有密码，额外设置 `first_login=true` Cookie（5 分钟，非 httponly），然后 302 跳到 `${FRONTEND_URL}/auth/callback`。
- **配置**：`LINUXDO_CLIENT_ID/SECRET/REDIRECT_URI/PROXY_URL`、`FRONTEND_URL`、`INITIAL_ADMIN_LINUXDO_ID`。httpx 设置了 `trust_env=False`，可单独配代理。

### 1.4 邮箱验证码注册 / 登录 / 重置密码
- **API**：`POST /api/auth/email/send-code`（scene 取 register/login/reset_password）、`/email/register`、`/email/login`、`/email/reset-password`。
- **实现要点**：
  - 验证码 6 位数字（`secrets.randbelow`），存在**进程内 dict** `_email_verification_storage`，key 为 `scene:email`，值包含 code、expires_at、last_sent_at、attempts；
  - 有效期默认 10 分钟，重发间隔默认 60s，过于频繁返回 429；错误满 5 次作废并返回 429；
  - 注册时发码要求邮箱未注册，登录和重置时要求邮箱已注册（未注册返回 404，可以用来探测邮箱是否注册过）；
  - 邮箱用户：`user_id = email_{md5(email)[:16]}`，`username = 邮箱`，trust_level=1，display_name 默认取邮箱 @ 前面的部分；
  - 邮件用 aiosmtplib 发送，同时带 HTML 和纯文本两种正文（`B/app/services/email_service.py`）；SMTP 默认 QQ 邮箱 `smtp.qq.com:465` SSL。
- **管理员配置界面**：`F/pages/SystemSettings.tsx:560-625` 可以配置邮件服务商、SMTP 主机/端口/用户名/授权码/发件人、SSL/TLS、邮箱认证开关、注册开关、验证码有效期、重发间隔。

### 1.5 会话续期 / 登出 / 当前用户 / 密码
- `POST /api/auth/refresh`：根据 Cookie `session_expire_at` 算剩余时间，剩余超过 `SESSION_REFRESH_THRESHOLD_MINUTES`（默认 30）就不续；否则重新签发 token。会话默认 120 分钟。
- `POST /api/auth/logout`：删除 `user_id`（旧版遗留）、`session_token`、`session_expire_at` 三个 Cookie。服务端不维护会话，所以 token 无法被吊销。
- `GET /api/auth/user`、`GET /api/users/current`：返回当前用户信息（两个接口重复）。
- `GET /api/auth/password/status`：返回 `{has_password, has_custom_password, username, default_password(固定为None)}`。
- `POST /api/auth/password/set`（修改密码，**不需要提供旧密码**）、`POST /api/auth/password/initialize`（仅限还没设过密码的用户）。密码只要求长度至少 6。

### 1.6 数据模型（`B/app/models/user.py`）
- `users`：`user_id(PK, 前缀 linuxdo_/local_/email_)`、`username`、`display_name`、`avatar_url`、`trust_level`（0-9 表示信任等级，**-1 表示禁用**）、`is_admin`、`linuxdo_id(unique, 非 LinuxDO 用户也写入同样的 ID)`、`created_at`、`last_login`。
- `user_passwords`：`user_id(PK)`、`username`（可作为登录名，和 users.username 不一定相同）、`password_hash`、`has_custom_password`、`created_at`、`updated_at`。
- 没有 email 字段、角色表、登录日志或审计表。

### 1.7 缺陷 / 可改进点（auth）
1. OAuth state 和邮箱验证码都存在进程内 dict：多 worker 部署或重启后失效；state 没有过期时间，未完成的授权会一直累积。代码注释里自己也写了"生产环境应使用 Redis"。
2. 本地登录和绑定登录每次都要遍历全部用户，并对每个用户单独查一次密码表，用户越多越慢（N+1 查询）。
3. 会话是纯无状态签名，登出和禁用都无法让已签发的 token 失效。禁用之所以生效，只是因为中间件每个请求都会回查用户。每个请求都要多一次 DB 查询。
4. `_session_secret()` 在 `SESSION_SECRET_KEY` 未配置时会依次回退到 LinuxDO secret、本地密码、OpenAI key，最后是一个写死的字符串（security.py:18-28）。
5. 修改密码不校验旧密码；除"至少 6 位"外没有其他密码强度要求；邮箱未注册时直接返回 404，可被用来枚举邮箱。
6. LinuxDO 登录时会用远端的 trust_level 覆盖本地值（user_manager.py:91-96）：管理员禁用的用户（-1）只要用 LinuxDO 再登录一次就被自动解禁。
7. 没设密码的用户会得到默认密码 `{username}@666`（user_password.py:73），AuthCallback 前端也按同样规则拼出默认密码，并提示用户可以直接使用（`F/pages/AuthCallback.tsx:40,207-210`），容易被猜到。
8. 同一套逻辑有多份重复实现：`/auth/user` 与 `/users/current`，`/auth/local/login` 与 `/auth/bind/login`。

---

## 2. 用户管理（后端 `users.py` + `admin.py`；前端 `UserManagement.tsx`）

### 2.1 后端存在两套 API
- `B/app/api/users.py`（前缀 `/api/users`，旧版）：`GET /current`、`GET ""`（列表）、`POST /set-admin`、`DELETE /{id}`（禁止删除任何管理员）、`GET /{id}`、`POST /reset-password`（不能重置自己的密码）。
- `B/app/api/admin.py`（前缀 `/api/admin`，前端实际在用）：
  - `GET /admin/users` → `{total, users:[...+is_active]}`，其中 `is_active = trust_level != -1`；
  - `POST /admin/users` 创建用户：username 3-20、display_name 2-50、password（可选，至少 6 位）、avatar_url、trust_level 0-9、is_admin；不填密码时用默认密码 `username@666`，并在响应的 `default_password` 里返回；
  - `PUT /admin/users/{id}`：可修改 display_name、avatar_url、trust_level(-1~9)、is_admin，**不允许取消最后一个管理员**；
  - `POST /admin/users/{id}/toggle-status`：`{is_active}`，禁用就是把 trust_level 设为 -1，启用则**重置为 0**，原来的等级会丢失；不能禁用自己；
  - `POST /admin/users/{id}/reset-password`：不填新密码时生成 `token_urlsafe(12)` 临时密码并返回；
  - `DELETE /admin/users/{id}`：不能删除自己，也不能删除最后一个管理员；会同时删除 `user_passwords` 记录，但**不清理该用户的项目等业务数据**。
- **管理员规则**：`user_manager.set_admin` 撤销管理员时至少保留一个（user_manager.py:145-180）。

### 2.2 前端 UserManagement（`F/pages/UserManagement.tsx`，887 行，路由 `/user-management`，入口在 UserMenu 下拉中，仅管理员可见）
- 表格列：用户名、显示名称、状态（启用/禁用 Tag）、角色（管理员金色、普通蓝色）、信任等级（-1 灰色，≥5 绿色，其余蓝色）、创建时间、最后登录、操作。
- 搜索和排序都在前端完成（按 username、display_name、user_id 模糊匹配），用 `Pagination` 分页，pageSize 默认 20。
- 弹窗：新建、编辑、重置密码。创建或重置成功后用 Modal 展示生成的密码，禁用和删除都需要二次确认。
- 移动端（`window.innerWidth <= 768`）时，操作列收窄到 80px，按钮收进下拉菜单。

### 2.3 缺陷 / 可改进点
1. **管理员创建的用户 user_id 前缀不一致，is_admin 设置会静默失效**：admin.py:120 生成的是 `admin_created_xxx`，把它当作 `linuxdo_id` 传给 `create_or_update_from_linuxdo` 后，因为不以 `local_` 开头，实际 user_id 变成了 `linuxdo_admin_created_xxx`（user_manager.py:73-76）；但 admin.py:132-139 设置 is_admin 时仍按 `admin_created_xxx` 去查，查不到也不报错。
2. 状态和信任等级共用 `trust_level` 一个字段，启用后原等级无法恢复。
3. 两套用户 API 并存，规则不同（例如 users.py 不允许删除任何管理员，admin.py 只是不允许删除最后一个管理员）。
4. 列表接口不分页、不支持搜索，数据一次性全部返回给前端；没有审计日志。
5. 删除用户后，该用户的项目、设置等数据成为孤儿。

---

## 3. 系统公告（后端 `announcements.py`；前端 AnnouncementModal / AnnouncementTimelineModal / useAnnouncements）

- **用户可见行为**：
  - 登录页和登录回调页会弹出公告 Modal，提供"今日不再提示"和"永久不再提示"（localStorage 键 `announcement_hide_today` / `announcement_hide_forever`，见 `F/pages/Login.tsx:159-165,311-315`）；
  - 项目列表页头部有"系统公告"入口，带未读红点，点击后以时间轴弹窗展示，支持全部标为已读。
- **数据模型** `announcements`：`id(UUID)`、`title(120)`、`content(Text, Markdown)`、`summary(255)`、`level(info/success/warning/error)`、`status(draft/published/hidden)`、`pinned`、`author_id`、`author_name`、`publish_at`、`expire_at`、`created_at`、`updated_at`，另有 4 个索引（`B/app/models/announcement.py`）。
- **API**：
  - 公开接口：`GET /api/announcements/status`（返回 mode、instance_id，client 模式还会返回 cloud_connected）、`GET /api/announcements?page&limit`、`GET /api/announcements/sync?since&limit`（增量同步，响应附带 `active_ids` 供客户端清掉已下线的公告）；
  - 管理接口：`GET/POST /admin/items`、`PUT/DELETE /admin/items/{id}`、`POST /admin/items/{id}/publish`、`POST /admin/items/{id}/hide`；
  - 有效公告的判定条件：`status=published` 且已到 `publish_at` 且未到 `expire_at`；排序为 pinned 优先，再按 publish_at 倒序。
- **实现要点**：
  - **只有 `WORKSHOP_MODE=server`（云端中央实例）的管理员才能发布公告**；client 实例的公告读取接口会通过 `announcement_client` 代理到云端，云端不可用时返回 503（announcements.py:37-49,259-288）。也就是说，自部署的实例默认只能看到上游作者发布的公告。
  - 前端 `useAnnouncements` 把列表缓存在 localStorage（最多 100 条），每 5 分钟增量同步一次、每 60 分钟全量同步一次，已读 ID 也存在本地（键 `mumu_announcements_*`，`F/hooks/useAnnouncements.ts:5-10`）。
  - 发布表单在 SystemSettings 页（标题、级别、摘要、Markdown 正文编辑与预览、状态、发布时间、过期时间、置顶），见 `F/pages/SystemSettings.tsx:763-895`。
- **缺陷 / 可改进点**：自部署管理员无法给自己的用户发公告，与上游云端强耦合；已读状态只存在浏览器本地，换设备后会丢失；公告用的 UTC naive 时间与系统其他地方的中国时区混用。

---

## 4. 更新日志与版本检查（后端 `changelog.py`；前端 ChangelogModal / ChangelogFloatingButton / AppFooter）

- **行为**：项目列表页有一个浮动按钮，点击打开更新日志弹窗，内容来自上游 GitHub 仓库 `xiamuceer-j/MuMuAINovel` 的 commits，按 Conventional Commits 解析成 feature/fix/docs/perf 等类型并显示徽标；页脚显示当前版本，检测到新版本时高亮提示，并提供 GitHub、LinuxDO、License 链接。
- **API**：`GET /api/changelog?page&per_page`（第 1 页在进程内缓存 1 小时）、`POST /api/changelog/refresh`（需要登录，清缓存后重新拉取）。
- **实现要点**：
  - commit 类型解析支持三种格式（`type(scope): msg`、`[type] msg`、`type：msg`），以及中文关键词兜底（修复、优化、新增等），见 `F/services/changelogService.ts:79-135`；
  - 版本检查 `checkLatestVersion` 请求 shields.io 的 GitHub release 徽章 SVG，用正则从中提取版本号，再和 `F/config/version.ts` 做比较（`F/services/versionService.ts`），这样可以绕开 GitHub API 的 CORS 和限流问题。
- **缺陷 / 可改进点**：数据源写死为上游仓库，二次开发后显示的仍是上游的更新；靠解析 SVG 拿版本号比较脆弱；前端 changelogService 自己也定义了 GitHub API 地址，和后端代理有重复。

---

## 5. 项目管理与书架（后端 `projects.py`、`common.py`；前端 ProjectList / BookshelfPage）

### 5.1 数据模型 `projects`（`B/app/models/project.py`）
`id(UUID)`、`user_id`、`title`、`description`、`theme`、`genre`（逗号或顿号分隔，前端拆成多个标签）、`target_words`、`current_words`、`status`（planning 等）、`wizard_status(incomplete/completed)`、`wizard_step(0-4)`、`outline_mode(one-to-one/one-to-many, CHECK)`、世界观字段 `world_time_period/location/atmosphere/rules`、`chapter_count`、`narrative_perspective`、`character_count`、封面字段 `cover_image_url/cover_prompt/cover_status(none/generating/ready/failed, CHECK)/cover_error/cover_updated_at`、`created_at`、`updated_at`。

### 5.2 API（`/api/projects`）
- CRUD：`POST ""`、`GET ""?skip&limit=100`（按 updated_at 倒序，返回 `{total, items}`）、`GET/PUT/DELETE /{id}`。
- 删除：先清理该项目在向量库中的记忆，然后**按顺序手动级联删除** 12 类关联数据（角色关系、组织成员、组织、生成历史、分析任务、批量任务、角色职业、职业、故事记忆、章节、大纲、角色、伏笔），原因是 SQLite 默认不启用外键约束（projects.py:201-343）。
- `GET /{id}/export`：导出全书 TXT。每章格式为 `第N章 标题`，正文每段前加两个全角空格缩进，章与章之间只空一行，这个格式专门设计成可以被拆书导入重新识别；没有任何章节时返回 404；文件名用 RFC5987 编码。
- `POST /{id}/export-data`：导出 JSON。可选是否包含 generation_history、writing_styles、careers、memories、plot_analysis。
- `POST /validate-import`：上传 JSON 进行预校验，返回 `{valid, version, project_name, statistics, errors, warnings}`；只接受 .json，大小上限 50MB。
- `POST /import`：始终**新建项目**，归属当前用户，`wizard_status` 直接设为 completed。
- 数据一致性维护：`POST /{id}/check-consistency`、`/fix-organizations`、`/fix-member-counts`（对应 `B/app/utils/data_consistency.py`）。
- 封面：在 `project_covers.py` 中实现（生成、下载），列表页卡片上直接可以操作。

### 5.3 导入导出格式（`B/app/schemas/import_export.py`、`B/app/services/import_export_service.py`，1840 行）
- 顶层 `ProjectExportData`：`version`（当前为 1.1.0，支持 1.0.0 和 1.1.0）、`export_time`、`project`（dict）、chapters、characters、outlines、relationships、organizations、organization_members、writing_styles、generation_history、careers、character_careers、story_memories、plot_analysis、project_default_style。
- **实体之间按名称关联，不导出 ID**：关系用 source_name/target_name，章节通过 outline_title 关联大纲，记忆和剧情分析通过 chapter_title 关联章节，角色职业用 character_name + career_name。
- 导入顺序：角色 → 大纲 → 章节 → 其余实体，过程中维护名称到新 ID 的映射。
- 另有角色/组织的单独批量导出导入（`export_type: characters`）。

### 5.4 前端 ProjectList（`F/pages/ProjectList.tsx`，1106 行，路由 `/` 和 `/projects`）
- **这是全局主壳**：左侧固定 Sider（展开 220px，折叠 60px，折叠状态存 localStorage，见 `F/utils/sidebarState.ts`），右侧通过内部状态 `activeView` 切换内容，切换时不改变路由：
  - 我的书架（projects）
  - 创作工具：拆书导入（book-import）、MCP 插件（mcp）、提示词管理（prompts）
  - 系统设置：API 设置（settings）、系统设置（system-settings）、外链 "MuMuのAPI"
  - 侧栏底部放 ThemeSwitch 和 UserMenu
- 顶部 Header（桌面端高 70px，移动端 56px）：系统公告入口（带未读红点），以及统计卡片"创作中 N 本 / 已完结 N 本 / 总字数"。
- 导入弹窗：选文件后自动预校验，展示统计和警告，确认导入后跳转到新项目。
- 导出弹窗：可多选项目，勾选要包含的数据类型；多个项目时**逐个触发浏览器下载，每次间隔 500ms**，最后汇总成功和失败数量。
- 删除确认文案："删除项目将同时删除所有相关数据，此操作不可恢复"。
- 点击项目：向导未完成的跳 `/wizard?project_id=`，已完成的跳 `/project/:id`。
- 页面可见性变化（visibilitychange）时自动刷新列表。

### 5.5 前端 BookshelfPage（`F/pages/BookshelfPage.tsx`，947 行，由 ProjectList 渲染的纯展示组件）
- 书架网格布局，移动端为单列。第一张卡片固定是"新建"书本卡，提供"开始向导"和"灵感模式"（`/inspiration`）两个入口。
- 每个项目是一张书本样式的卡片：左侧书脊，右上角书签状态（进度、完结、未完成向导），显示类型标签、字数、进度条（颜色随进度变化）、更新时间。
- 封面：`cover_status=ready` 时卡片正面显示封面图，可以**翻转**到信息面；generating 时显示 loading，failed 时可重试；支持重新生成（覆盖）和下载。
- 页面顶部有"导入 / 导出"按钮；未配置 API 时显示一个可关闭的 Alert，带"去设置"跳转。
- 暗色模式下通过 `useThemeMode().resolvedMode` 切换配色，并用 CSS `color-mix` 计算半透明色。

### 5.6 缺陷 / 可改进点（项目）
1. 项目列表默认 limit=100，没有服务端搜索、排序选项、归档、回收站或软删除，删除不可恢复。
2. 手动级联删除依赖开发者记得把新表加进删除列表，容易漏（例如知识库、Agent 会话、后台任务等表都不在列表中）。
3. JSON 导入导出按名称关联，同名角色或同名章节会冲突；多项目导出不是打成一个 zip，而是依次触发多次下载；导入只能新建项目，不能合并到已有项目。
4. 很多接口重复写同样的 `getattr(request.state,'user_id')` 判断和项目归属查询，`common.verify_project_access` 没有被统一使用。

---

## 6. 前端认证页面

### 6.1 Login（`F/pages/Login.tsx`，1057 行，路由 `/login`）
- 布局：桌面端左右分栏。左侧 `Col lg=11` 是品牌区，包含 logo、渐变标题、4 个特性卡片（多 AI 模型协同、智能向导驱动、角色组织管理、章节创作闭环）、网格背景；右侧 `lg=13` 是登录卡片。移动端（xs）隐藏左侧，只显示登录卡片。页面上提供 ThemeSwitch。
- 登录卡片：用 Tabs 承载"本地登录 / 邮箱登录 / 邮箱注册"（只显示后端已启用的方式），LinuxDO 登录单独一个按钮，另有"忘记密码"切换到重置面板（邮箱 + 验证码 + 新密码 + 确认）。
- 发送验证码按钮有倒计时（三种场景各自独立计数），文案为"Ns 后重发"。
- 本地登录的表单标签写作"管理账号"和"访问密钥"。
- 支持 `?redirect=` 参数：已登录直接跳转；走 LinuxDO 前把 redirect 存进 sessionStorage 的 `login_redirect`。
- 页面底部展示登录提示列表（默认账号、首次 LinuxDO 登录会自动建号等）。

### 6.2 AuthCallback（`F/pages/AuthCallback.tsx`，316 行，路由 `/auth/callback`）
- 进入后先调用 `getCurrentUser`；失败时显示 Result 组件"登录失败"，并提供返回登录页按钮。
- 检测到 `first_login=true` Cookie 后立即清除，并弹出"设置密码"Modal：用户可以输入自定义密码，也可以直接使用默认密码 `{username}@666`；无论哪种选择都会调用 `initialize` 接口落库，之后就能用账号密码登录。
- 完成后跳转到 `login_redirect`，没有则跳 `/`；页面中也会弹出公告 Modal。

### 6.3 ProtectedRoute 与会话管理
- `ProtectedRoute`：每次挂载都调用 `/auth/user` 校验登录，校验中显示全屏 Spin，失败时跳 `/login?redirect=当前路径`（`F/components/ProtectedRoute.tsx`）。
- `sessionManager`（`F/utils/sessionManager.ts`）：每 60s 读一次 `session_expire_at` Cookie。剩余 ≤5 分钟时弹出警告（10s 的 message）；剩余 ≤30 分钟且用户在最近 30 分钟内有过操作（监听鼠标和键盘事件）时自动调用 refresh；已过期则调用 logout 并跳登录页。
- axios 响应拦截器遇到 401 统一处理并跳转登录（`F/services/api.ts:100-115`）。

### 6.4 UserMenu（`F/components/UserMenu.tsx`）
- 显示头像、显示名，管理员显示"👑 管理员"，普通用户显示"🎖️ Trust Level N"。
- 下拉菜单：用户信息、用户管理（仅管理员）、修改密码（Modal 里只有新密码和确认密码两项）、退出登录。
- 有 `compact` 和 `showFullInfo` 两种形态，分别用于侧栏折叠状态和移动端抽屉。

---

## 7. 赞助页 Sponsor（`F/pages/Sponsor.tsx`，281 行）
- **路由位置**：`/project/:projectId/sponsor`，并且是**项目详情的默认 index 路由**（`F/App.tsx` 中 `<Route index element={<Navigate to="sponsor" />}>`），也就是说每次进入项目详情先看到的是赞助页。
- 内容包括：渐变 Hero 标题；"赞助专属权益"卡片（加入赞助群、优先需求响应、Windows 一键启动 EXE、专属技术支持，每项标注对应的赞助档位）；5 个金额档位（¥5 辣条、¥10 拼好饭、¥20 咖啡、¥50 烧烤、¥99 海底捞），点击后弹出对应的收款码图片（`/5.png` 等静态资源）。
- 样式用 `clamp()` 做响应式字号。
- 与本项目的关系：这是上游作者的商业化入口，与小说创作功能无关。

---

## 8. 主题（暗色模式）
- 文件：`F/theme/ThemeProvider.tsx`、`themeConfig.ts`、`themeStorage.ts`、`themeContext.ts`、`useThemeMode.ts`，以及组件 `F/components/ThemeSwitch.tsx`。
- 三种模式：light / dark / system，存在 localStorage 的 `mumu_theme_mode` 中，默认 system；监听 `prefers-color-scheme` 的变化。
- 基于 antd 的 `ConfigProvider` 和 `theme.defaultAlgorithm / darkAlgorithm`：主色 `#4D8088`，圆角 8，按钮高 36，卡片圆角 12；浅色模式背景 `#F8F6F1`（暖米色），暗色模式背景 `#0f1115` / `#141414`。
- 切换时如果浏览器支持 **View Transition API** 就使用过渡动画，并在 `<html>` 上设置 `data-theme-transition=to-dark/to-light`；同时始终设置 `data-theme-mode`、`data-theme-resolved` 和 `color-scheme` 属性，供 CSS 选择器使用。
- ThemeSwitch 是三段的 Segmented 控件（灯泡、月亮、显示器图标），折叠侧栏里则是一个点击轮换的图标按钮。
- antd 语言包为 zh_CN。
- 节日装饰：`F/components/SpringFestival.tsx`（553 行）全局挂在 App 顶层，提供春节装饰，依赖 canvas-confetti。

---

## 9. 移动端适配
- 统一判断条件是 `window.innerWidth <= 768`，各页面分散使用（pages 和 components 中共出现 41 次）；只有部分页面监听了 resize（例如 `ProjectDetail.tsx:39,72`），ProjectList 和 UserManagement 只在渲染时读一次，**窗口缩放后不会实时更新**。
- 主要形态：侧栏变成 antd `Drawer`（由汉堡按钮 `MenuUnfoldOutlined` 打开）；Header 高度 70 降到 56；内边距缩小；书架改为单列；删除确认 Modal 去掉 top 定位；表格操作列收进下拉菜单；登录页隐藏品牌区。
- 页脚 `AppFooter` 根据 sidebarWidth 计算左偏移，移动端偏移为 0。
- CSS 媒体查询很少（App.css 2 处、index.css 4 处），主要依靠 JS 内联样式。

---

## 10. 全局布局与导航
- **两层壳**：
  1. **ProjectList 壳**（`/`）：左侧 Sider 菜单，右侧按 activeView 切换内容（见 5.4），不改变 URL，所以设置、MCP 等页面**没有独立 URL**，刷新后会回到书架（但 `/settings`、`/prompt-templates`、`/mcp-plugins` 也另外注册了独立路由）。
  2. **ProjectDetail 壳**（`/project/:projectId/*`，`F/pages/ProjectDetail.tsx`，768 行）：左侧菜单分为两组。"创作管理"组包括世界设定、角色、组织、职业、关系、大纲、章节、章节分析、伏笔、知识库；"创作工具"组包括写作风格、提示词工坊、Skill 对话、Skill 管理；外加赞助入口。顶部统计显示大纲、角色、章节条数和已写字数；右侧可展开 Project Agent 抽屉（移动端为 Drawer）。
- 其他独立全屏路由：`/wizard`、`/inspiration`、`/chapters/:chapterId/reader`、`/user-management`、`/login`、`/auth/callback`。
- 全局事件总线 `F/store/eventBus.ts`，以及 zustand store（`F/store/index.ts`、`hooks.ts`）。
- 浮动组件：`ChangelogFloatingButton`、`FloatingTaskPanel`（后台任务）、`FloatingIndexPanel`。

---

## 11. constants / utils / 其他后端周边
- `B/app/constants/prompt_categories.py`：提示词工坊的分类（通用、玄幻/仙侠、武侠、言情、科幻、悬疑/惊悚、历史、都市、游戏/电竞、其他）和 29 个热门标签。
- `B/app/utils/`：
  - `sse_response.py`：SSE 格式化、标准进度阶段、心跳包装、`create_sse_response`（见第 0 节）；
  - `reasoning_text.py`：去掉 `<think>` 标签、从响应中提取 reasoning 文本、识别 MiniMax、解析 SSE data 行；
  - `data_consistency.py`：确保组织记录存在、同步成员计数、校验关系和组织成员、执行全量一致性检查。
- `B/app/main.py`：`GET /health`（Docker 健康检查使用）、`GET /health/db-sessions`（管理员可查看会话创建、关闭、活跃、错误、SSE 断开等统计，用于排查连接泄漏）。
- `B/app/security.py` 中的 `validate_public_http_url` / `validate_ai_http_url`：用于防 SSRF，拒绝内网、回环、链路本地、组播地址，URL 中不允许携带账号密码；可通过 `ALLOW_PRIVATE_AI_ENDPOINTS` 和 `ALLOWED_AI_HOSTS` 放行本地 Ollama 或 `host.docker.internal`。
- **RequestIDMiddleware 的问题**：每个请求都往**根 logger** 上添加或移除 filter（`B/app/middleware/request_id.py:37-48`），请求并发时多个 filter 同时挂着，日志里的 request_id 会串到别的请求上。

---

## 12. Docker 与部署方式
- **Dockerfile**（多阶段构建，支持 amd64 和 arm64）：
  1. 用 `node:22-alpine` 构建前端：先删除 package-lock 再 npm install，用 sed 把 vite 的 outDir 改成 dist；
  2. 运行镜像基于 `python:3.12-slim`：安装 gcc、curl、postgresql-client、netcat；pip 安装依赖；**构建时从 ModelScope 下载 ONNX embedding 模型**（paraphrase-multilingual-MiniLM-L12-v2）并校验 sha256；把前端产物复制到 `./static`；**镜像里只放 PostgreSQL 的 alembic 配置**（`alembic-postgres.ini` 复制为 `alembic.ini`）；
  3. 构建参数 `USE_CN_MIRROR=true` 时切换到阿里云、npmmirror、清华镜像；
  4. HEALTHCHECK 访问 `/health`；入口为 `entrypoint.sh`。
- **entrypoint.sh**：用 `nc` 等待数据库端口就绪（最多 30 次），再用 `psql SELECT 1` 探测，然后执行 `alembic upgrade head`，最后启动 uvicorn。
- **docker-compose.yml**：`postgres:18-alpine`，挂载 `init_postgres.sql`，并带有一组 PG 调优参数（max_connections=200、shared_buffers 等）；应用服务挂载 `./logs`、`./storage/generated_covers` 和 `./.env:ro`，所有配置通过环境变量注入，默认值包括 LinuxDO `11111`、本地账号 `admin/admin123`、`SESSION_COOKIE_SECURE=true`。
- **其他部署形态**：
  - Termux（安卓）一键安装脚本 `install-termux.sh`：使用 SQLite（`sqlite+aiosqlite:///.../ai_story.db`）和 `alembic -c alembic-sqlite.ini upgrade head`；
  - Windows 免安装 EXE（PyInstaller，代码中用 `sys.frozen` / `_MEIPASS` 判断）：作为赞助权益提供，不开源；
  - 源码开发：后端用 `.venv` + uvicorn，前端 `npm run dev`；vite 默认把产物输出到 `../backend/static`。
- 运维辅助脚本 `B/scripts/migrate.py`：封装了 alembic 的 revision、upgrade、downgrade、current、history、heads、stamp、auto_migrate、init 等命令；`setup_postgres.py` 用于初始化 PG。
- 版本号 1.5.7，出现在 `config.app_version`、`package.json`、compose 中的 `APP_VERSION`、`F/config/version.ts` 等多处，需要手动保持同步。

---

## Caveats / Not Found
- 没有逐行阅读 `Login.tsx`、`BookshelfPage.tsx` 和 `ProjectList.tsx` 中纯样式部分的代码，交互描述来自 grep 结果和关键段落，细节动画（如翻转动效的具体实现）未核实。
- `B/app/services/email_service.py` 和 `announcement_client.py` 只确认了用途，没有细读重试和超时策略。
- 管理员 is_admin 设置失效（第 2.3 节第 1 条）是根据代码路径推断的，没有实际运行验证。
- 没有调研 `project_covers.py`（封面）、`book_import.py`（拆书）、`settings.py`（用户 API 设置）的细节，它们属于其他调研范围。
- Windows EXE 的打包脚本不在仓库中。
