# 设计：浏览器验收环境与执行方式

依据见 `research/acceptance-environment.md`。本任务默认不改产品代码；只有按 PRD R4 修小缺陷时才改。

## 任务结构

采用单个任务，不拆父子任务。七组验收共用同一套环境、账号和假模型，拆开会重复搭建；各组的独立性由 `implement.md` 的分批和 `research/verification.md` 的分节体现。

## 验收环境

```
Playwright 脚本 ──► 本机 Chrome ──► FastAPI（127.0.0.1:随机端口）
                                     ├─ 同源提供 frontend/dist（FRONTEND_DIST_DIR）
                                     ├─ 临时 SQLite（alembic upgrade head）
                                     └─ 模型请求 ──► 本地假模型服务（127.0.0.1:随机端口，/v1）
```

- **单端口生产构建**：验收前在 `frontend/` 执行 `npm run build`，后端经 `FRONTEND_DIST_DIR` 同源提供页面与接口。不使用 Vite dev，这样验的是实际交付的包。现有 `frontend/dist` 不确定是否对应当前代码，必须重建。
- **运行数据在仓库外**：SQLite 文件、备份目录、端口文件、请求日志、截图都放在 `mktemp -d` 得到的系统临时目录。
- **配置只走进程环境变量**：`DATABASE_URL`、`JWT_SECRET`、`ENCRYPTION_KEY`、`BACKUP_DIR`、`FRONTEND_DIST_DIR`、`REGISTRATION_ENABLED=true`、`COOKIE_SECURE=false`、`ALLOW_PRIVATE_AI_ENDPOINTS=true`、`AUTO_ANALYZE_ENABLED`、`ADMIN_USERNAME`、`ADMIN_PASSWORD`。密钥与管理员口令每次启动现场生成，只写入临时目录。后端从当前工作目录读 `.env`（`backend/app/core/config.py:23`），所以启动时工作目录设为临时目录，用 `--app-dir` 指向 `backend/`，避免读到产品配置。
- **建表只用 alembic**，不用 `create_all`，以免验到与迁移不一致的库结构。

## 假模型服务

采用本地 OpenAI 兼容 HTTP 服务，在应用的「模型配置」里配成 `openai_compat` 并设为默认。不采用进程内注入 `FakeLLM` 的原因：注入方式要覆写发送门禁（`backend/tests/conftest.py:57-66`），那样协作者的模型授权与回退路径（G3 的 C-11、G4 的 D-4）就不是真实路径了。

服务能力：

- `POST /v1/chat/completions`，支持流式与非流式，返回 `usage`。
- 默认按提示词关键词分流：章节正文、章节分析 JSON、事实台账抽取、冲突比对、影响评估各返回结构合法的固定内容。分析类的 JSON 结构在编写时对照 `backend/tests/helpers.py` 中的测试样例。
- 控制接口（只绑定回环地址）：压入一次性脚本（文本、流式工具调用、指定状态码的失败）、设置响应延迟、查询调用记录。G4、G7 的智能体场景靠压入 `load_skill`、`start_task`、改名提案等工具调用脚本驱动。
- 所有请求体记入临时目录的日志，用于核对出站内容（例如 Skill 输入值是否按预期填入）。

## 账号与数据

全部经接口创建，脚本可重复执行（已存在则登录）。

| 账号 | 身份 | 用于 |
|------|------|------|
| `qa_author` | 作品与系列的作者 | 全部 |
| `qa_editor` | 编辑者；可切换有无作者模型授权 | G1、G2、G4 |
| `qa_viewer` | 查看者 | G1、G3、G6 |
| `qa_series` | 仅系列设定协作者 | G3 |
| `qa_series_editor` | 系列设定协作者，兼某成员书编辑者 | G3 |
| `qa_admin_rw` | 管理角色含 `projects.read`、`projects.edit`，再被邀请为协作者 | G3 |
| `qa_admin_ro` | 管理角色仅 `projects.read`，再被邀请为查看者 | G3、G5、G6 |
| 超级管理员 | 启动时由环境变量引导 | 建角色与分配 |

夹具数据：一本约 15 章的作品（第 5 章已分析，含剧情记忆与设定变化；第 5、12 章含「佩剑」冲突；角色「林远」）；一个含两本书的系列，已采用与未采用的共享条目各一；若干固定与收藏笔记；一个带下拉默认值、必填单行和触发词的 Skill；一个含多场景与分区块的章节；若干自定义分类。

自定义管理角色与只读管理账号只建在隔离库里。以往这一步需要用户明确同意，本次以用户批准本规划为准。

## 浏览器驱动

- Playwright 从 npx 缓存运行（`npx playwright` 或缓存目录中的包），`chromium.launch({ channel: 'chrome' })` 驱动本机 Chrome；不写入 `frontend/package.json`。
- 每个身份一个独立的 `browser.newContext`。
- 读取失败用 `page.route` 对指定接口返回 500，放行后重试，核对重试确实发出新请求。
- 每个场景的脚本输出断言结果与关键可见文本，截图存临时目录；结论与文本证据写入 `research/verification.md`。

## 仓库内的产出

```
.trellis/tasks/11-18-browser-acceptance-backlog/research/
├── harness/
│   ├── README.md        启动步骤、环境变量名、已知限制
│   ├── serve.py         组装环境、迁移、启动后端
│   ├── fake_model.py    假模型服务
│   ├── seed.py          账号与夹具
│   └── scenarios/       各组的 Playwright 脚本
└── verification.md      59 个场景的结论
```

这些文件不含密钥、口令、数据库或截图。

## 缺陷修复的流程

发现失败 → 记录复现 → 按 PRD R4 的界线归类 → 小缺陷：先写能复现的自动化测试，再修，跑受影响测试与 lint，重建前端包，重启环境后复验原场景；较大缺陷：只记录并在 TODOS 立项。回归遵循项目的精简策略：中途只跑受影响与哨兵测试，全量回归留到收口，且仅在改过产品代码时执行。

## 风险

- **假模型的 JSON 结构与后端解析不符**会让场景在到达界面前失败。缓解：冒烟阶段先各走一次分析、检查和评估，确认结构后再开始验收。
- **调研给出的预期可能有误**（场景表由阅读代码与记录得出，未执行过）。按 PRD R4 最后一条，拿不准时先问用户。
- **并发会话共享工作区**：本任务不做任何 git 清理类操作；修缺陷只改定位到的文件。
- **临时目录被系统清理**：只影响运行数据，脚本在仓库内，可重建。

## 回滚

验收本身不改产品。缺陷修复按文件粒度独立，若某处修复引出回归，可单独撤回该处改动并把场景改记为失败、转为 TODOS 条目；撤回前先向用户说明并取得确认。
