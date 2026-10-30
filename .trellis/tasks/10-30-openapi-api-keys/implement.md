# API Key 与开放接口实施计划

## 阶段与前置门禁

- 用户于 2026-10-30 明确批准最终规划摘要，已激活任务，开始实施；验证证据随切片记录。
- 用户已批准建任务，已确认密钥可再次显示 / 复制以及扩展的只读接口和文档范围。
- `prd.md` 已按目标、背景、需求、验收、不在范围、风险收敛；最终摘要已获用户后续明确批准。
- 收到批准后再检查工作区状态，运行 `task.py start .trellis/tasks/10-30-openapi-api-keys`。已有 `pelican-bicycle.html` 为用户未跟踪文件，不纳入本任务。
- 不拆父子任务：本次是一套共用密钥与文档契约的端到端能力，以以下可验证切片实施。

## 所有权与分工

实施 / 检查使用 Trellis 原生子代理；每次派发首行必须为 `Active task: .trellis/tasks/10-30-openapi-api-keys`。原生上下文未注入时，子代理按角色读取相应 JSONL、PRD、设计与本计划。

部分原规范超过单文件注入 32768 字节，因此清单引用 `research/spec-*.md` 中本任务相关段落的逐字摘录，保留原文件路径、行号和来源摘要，避免截断重要约束。原规范仍为权威；涉及摘录外内容或原规范已变更时，直接读取原文件相关段落，不修改注入上限。

| 角色 / 切片 | 拥有范围 | 限制 |
| --- | --- | --- |
| 后端基座 implement | 新密钥模型 / 迁移、models 导出、密钥 schemas / service / 管理路由及注册、独立认证依赖、额度配置 / 环境模板、备份排除和对应测试 | 不改前端；不得把平台 Key 接入既有 CurrentUser |
| 后端读取与文档 implement | 新开放路由 / schemas / services、main 注册与专属 OpenAPI / Swagger UI、对应读取 / 文档测试 | 基座后开始；不改前端 API 类型，不扩展对外写能力 |
| 前端 implement | `frontend/src/pages/ApiKeysPage.tsx`、API 模块 / types、router、StudioNavigation、所需样式与 API / 状态逻辑测试 | 基座管理契约明确后可与开放读取并行；所有网络走 api client，不持久化明文 |
| 主会话 | PRD / design / 本计划与清单、README 对接说明、规范更新、阶段切换与最终集成提交 | 协调共享文件；不提前激活任务或实现 |
| trellis-check | 任务变更的质量检查及机械性修复 | 全切片落地后运行；设计 / 契约变更报告主会话，不能自行扩大范围 |

每个实现代理均明确告知“你并不独自在仓库工作，不得回退他人修改”。后端两切片可由同一代理按顺序推进；前端不得与后端同时写 README 或同一文件。

## 切片 1：密钥管理与独立认证

- [x] 实施前读取相关 backend specs 和 JSONL；复核当前唯一 Alembic head，避免重复版本号。
- [x] 先写必要失败测试：Key 创建、密文而非明文落库、列表无完整密钥、刷新后 reveal、跨用户和管理员不能查看。
- [x] 新增 api_keys / api_key_projects 及迁移，验证外键、摘要唯一、项目删除清理授权、用户删除级联；api_key_projects 加入 NOT_EXPORTED。
- [x] 实现管理 schema / service / 路由：创建、列表、PATCH、reveal、幂等撤销；部分更新和有效期默认值符合 design。
- [x] 复用 SecretCipher；创建和 reveal no-store，列表从已存掩码构造，不批量解密；撤销清空密文。
- [x] 实现只接受 X-API-Key 的开放请求依赖：摘要查找、账号启用、有效期、撤销、小说授权和资源隔离。
- [x] 实现数据库原子分钟额度及 last_used_at 更新，429 / Retry-After；配置加入现有部署环境模板，默认 120。
- [x] 验证无 Key、Cookie 不替代 Key、Key 不替代登录 JWT、过期 / 撤销 / 禁用 / 删除 / 重新启用、独立于 token_version 的生命周期。
- [x] 测试同一分钟并发请求不能超额，跨分钟可恢复，不同 Key 独立；用可控时钟，不用长 sleep。
- [x] 向主会话回传准确的管理接口字段和测试结果，前端据此开工。

建议验证（工作目录 backend；新增测试文件名称可按实现调整，但覆盖范围不能缩减）：

```text
uv run ruff check .
uv run pytest tests/test_api_keys.py tests/test_migrations.py tests/test_project_transfer.py tests/test_auth.py tests/test_admin.py
```

回退点：此时可以撤下密钥路由并降级新增表，现有小说业务不受影响。测试不连接开发者数据库或真实 AI。

## 切片 2：完整只读目录与创作设定

- [x] 先建立开放响应 schema 和针对真实 HTTP 的读取 / 隔离失败测试。
- [x] 实现小说列表 / 详情、章节目录 / 正文、封面、TXT / Markdown / EPUB、当前 Key 信息。
- [x] 使用数据库分页和稳定排序，批量章节统计 / 封面时间，区分元数据 updated_at 与 content_updated_at。
- [x] 实现人物、世界观、大纲及细纲、组织及成员、关系及类型；按 research/read-contracts.md 保留领域语义。
- [x] 每个资源入口先检查小说授权，再检查路径小说和资源 ID；关系角色过滤不能跨小说，类型不能包含他小说自定义项。
- [x] 复用 plan_outs / relationship to_outs 等批量派生逻辑，避免失效人物引用、称呼颠倒或 N+1。
- [x] 全部列表验证 page / page_size，章节验证 status / updated_after；覆盖无记录、超过末页、同排序值与跨页数据。
- [x] 验证空章与生成中章节可读、目录无正文、卷信息 / 下载内容 / MIME / 文件名与警告正确。

建议验证（backend）：

```text
uv run pytest tests/test_open_api.py tests/test_projects_and_story.py tests/test_chapters.py tests/test_reader.py tests/test_project_transfer.py
```

若既有测试文件名与计划不同，按仓库实际名称执行相关测试并记录，不创建占位测试文件来满足命令。

## 切片 3：OpenAPI 与在线文档

- [x] 从实际开放路由生成专属 OpenAPI JSON，不覆盖默认 app.openapi。
- [x] 注册 /api/open/v1/docs 和 /api/open/v1/openapi.json，位于 SPA fallback 之前；确认生产静态挂载后仍返回正确 HTML / JSON。
- [x] 使用 APIKeyHeader security scheme，Swagger Authorize 调试，关闭密钥持久化，示例仅用占位符。
- [x] 全部操作有 typed 参数 / 响应、唯一 operationId、中文描述、401 / 404 / 422 / 429、图片和导出的真实媒体类型。
- [x] 补充 curl、分页与时间边界、当前剧情设定、空章和导出说明；主会话据最终契约更新 README。
- [x] OpenAPI 测试检查安全方案、路径精确集合、只含 GET 业务操作、必需参数与错误 schema、排除内部管理 / reveal / AI 生成等接口。
- [x] JSON 在匿名请求下可解析，文档可浏览；没有 Key 的真实业务请求仍是 401；原有 docs / schema 行为保持兼容。

## 切片 4：前端密钥管理与文档入口

- [x] 采用现有 API client 及 types，新增懒加载设置页和桌面 / 移动共用导航项。
- [x] 创建 / 编辑表单覆盖名称、指定或全部小说、有效期；列表覆盖掩码、状态、授权、最近使用和撤销。
- [x] 眼睛图标主动 reveal，隐藏后清除明文；复制按钮在隐藏状态也可获取并复制完整 Key；图标有可访问名称。
- [x] 无 Clipboard API 或写入失败时给出可选择文本的手动复制路径，且不在错误消息中暴露 Key。
- [x] reveal 的迟到响应不能在隐藏、切换页面、登出或撤销后恢复明文；销毁临时状态。
- [x] 文档与下载 OpenAPI JSON 入口可用，页面文案区分平台访问 Key 与模型供应商 Key。
- [x] 只为关键行为写测试：请求契约、显隐 / 复制请求失效处理、日期和范围表单变换。避免镜像 UI 文案的低价值字符串测试。

验证（frontend）：

```text
npm run lint
npm run build
npm test
```

## 切片 5：面向客户的公开开发文档（用户追加）

- [x] 核对 HomePage 结构、样式、公开路由与实际开放接口，以 research/public-developer-docs.md 为本切片边界。
- [x] 在首页标记位置增加“开发文档”；新增 `/developers` 公开懒加载指南，复用品牌、主题和首页视觉。
- [x] 完成快速开始、Key鉴权、20接口目录、分页与响应、错误限流、封面导出和 Swagger / JSON 入口；示例只用占位符。
- [x] 前端 lint / build / test 全部通过；对必要行为做有价值的测试，不为静态文案写镜像断言。
- [x] 真实浏览器确认匿名入口 / 直达 / 刷新、锚点与复制、返回及原首页行为，320/375/768/1024/1440px和明暗主题可用；文件读取通过，IAB下载事件观察限制如实保留。
- [x] 全范围复核追加内容，更新规范、验证记录与提交清单后再进入提交确认。

## 集成验收与收尾

- [x] 汇总所有切片的最终变更，派发全范围 trellis-check，按实际调用路径验证并修复机械问题。
- [x] 后端执行完整 ruff / pytest，前端执行完整 lint / build / test，确认无放宽阈值、跳过测试或新增类型抑制。
- [x] 使用可用的真实浏览器工具，启动独立临时测试实例：创建 Key → 默认掩码 → 显示 / 隐藏 → 复制 → 刷新再次查看 → Swagger 授权读取 → 撤销后调用失败。
- [x] 实测指定小说的 Key 不能在文档中读取另一部小说；只用 Cookie 不能调用开放业务接口（后者由真实 HTTP 集成测试覆盖）。
- [ ] 浏览器覆盖 375px / 桌面、明暗主题、复制受限回退、console 和 network；使用临时用户 / 数据库，不改变用户的现有作品。除复制受限弹窗外均完成；IAB 在 HTTP 和 QA 剪贴板禁用策略下仍复制成功，回退由自动化测试覆盖，保留实测限制，详见 verification.md。
- [x] 核对备份不携带 api_keys 或 api_key_projects；迁移完整升降级验证通过。
- [x] 主会话按最终实现更新 `.trellis/spec/backend/` 的密钥 / 开放 API 契约和 frontend 页面约定，再记录验证证据与任务进度。
- [ ] 仅提交本任务范围，不纳入已有用户文件或其他会话变更；遵守 Trellis 完成 / 归档流程。

完整质量门禁（分别在对应工作目录运行）：

```text
backend: uv run ruff check .
backend: uv run pytest
frontend: npm run lint
frontend: npm run build
frontend: npm test
```

## 验收映射

| PRD 验收 | 主要证据 |
| --- | --- |
| A1、A2、A3 | 密钥 API 测试、密文 / 归属 / 撤销测试、前端状态逻辑和浏览器实测 |
| A4、A5 | 独立认证依赖与逐端点跨用户 / 跨小说 / 未授权测试 |
| A6、A7 | 开放读取领域数据、排序、序列化、图片 / 导出 HTTP 测试 |
| A8 | 数据库分页 / 时间边界及并发额度测试，429 / Retry-After |
| A9 | 实际 OpenAPI 路径 / 安全契约检查及 Swagger 浏览器实测 |
| A10 | 桌面 / 375px、明暗主题、图标标签和复制回退实测 |
| A11 | 迁移、备份注册表与完整后端 / 前端质量门禁 |

## 复核与变更规则

若实现发现必须扩大对外权限、改变明文可再次查看、小说授权、密钥生命周期或文档可见性，先回到规划更新产物并重新审阅；不得把未支持能力静默删除或把内部接口全部开放来节省适配工作。
