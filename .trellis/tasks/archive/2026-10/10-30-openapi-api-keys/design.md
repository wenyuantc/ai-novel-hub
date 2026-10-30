# API Key 与开放读取接口设计

## 1. 目标、边界与任务组织

需求以 `prd.md` 为准。本设计提交最终规划审阅，获用户明确批准后才实施。

这是一个端到端功能任务：密钥管理、开放读取与文档共用同一套鉴权和数据契约，按实施切片顺序推进，不拆出各自独立上线的子任务。现有网页登录和 `/api/v1` 业务接口继续使用原认证。

新增边界：

- `/api/v1/api-keys`：用户通过现有 Cookie / JWT 管理本人的平台访问密钥。
- `/api/open/v1`：第三方通过 `X-API-Key` 调用，只开放本任务列出的 GET 业务接口。
- `/api/open/v1/docs`、`/api/open/v1/openapi.json`：开放接口文档与机器可读描述，不展示用户数据，不要求先登录才能阅读；实际调试请求仍要求 Key。
- 新增前端 `/settings/api-keys`：创建、编辑、显隐、复制、撤销与文档入口。

不开放写入、删除业务资源、AI 生成、后台任务、管理端、模型配置或完整项目备份。

## 2. 密钥数据与生命周期

新增独立模型模块 `backend/app/models/api_key.py`，在 `app.models` 导出，并新增 Alembic 迁移（当前 head 是 `0021`，实施时复核唯一 head，预计新增 `0022_api_keys`）。遵循无 ORM relationship 的现有约定。

### api_keys

| 字段 | 含义 |
| --- | --- |
| id、user_id | 主键、所属用户；用户外键 CASCADE，按 user_id 查询 |
| name | 名称，去首尾空白后 1–100 字符 |
| key_hash | 完整随机 Key 的 SHA-256 摘要，64 字符、唯一索引，用于鉴权查找 |
| key_enc | 复用 SecretCipher / Fernet 保存完整 Key 的密文；撤销后清空，允许 null |
| key_masked | 创建时生成的掩码，列表不需要为生成掩码解密 |
| all_projects | 是否授权本人的全部小说，包括之后新建的小说 |
| expires_at | 可空 UTC 时间；null 表示不过期 |
| revoked_at | 可空 UTC 时间；非空后永久失效，不能重新启用 |
| last_used_at | 最近通过认证和额度检查的请求时间；不用于判断业务内容是否修改 |
| rate_window、rate_count | 每分钟固定窗口计数，支持多进程共享同一个数据库计数 |
| created_at、updated_at | 与现有 TimestampMixin 相同的时间戳 |

### api_key_projects

- `(api_key_id, project_id)` 为复合主键，分别外键 CASCADE 到 api_keys / projects。
- 仅在 `all_projects=false` 时有意义。授权所选小说时至少选择一部；输入去重，且每个 ID 必须属于当前用户。
- 所选小说被删除后，授权行自动消失；空集合仍表示没有小说授权，绝不能退化为全部授权。
- `api_key_projects` 引用 projects，必须登记在项目备份注册表的 `NOT_EXPORTED`，理由为访问凭据授权不随小说迁移。`api_keys` 自身是用户级表、不登记。备份及 EPUB 都不能携带密钥或授权。

### 生成、查看、撤销

1. 使用 `secrets.token_urlsafe(32)` 生成随机部分，前缀 `anv_`；完整 Key 不使用用户可预测的 ID 或名称。
2. 同一创建事务保存摘要、Fernet 密文、掩码及授权关系。响应可包含一次完整 Key；后续列表和编辑响应均不带完整 Key、摘要或密文。
3. 用户主动点击眼睛图标或复制按钮时，调用仅现有网页登录认证可用的 `POST /api-keys/{id}/reveal`；按 `user_id` 校验后解密。刷新页面后可再次调用。
4. 查看 / 复制过期但未撤销的 Key 允许，界面保留过期提示；撤销的 Key 禁止查看和复制，后端清空密文，仍保留掩码和撤销记录。
5. 修改名称、授权小说及有效期不重新生成 Key；过期但未撤销的 Key 可调整有效期恢复使用。已撤销的 Key 不允许修改。
6. Key 独立于 JWT 的 `token_version`：修改 / 重置登录密码、登出不自动使 Key 失效；账号禁用或删除后立即不能认证，重新启用后未过期且未撤销的 Key 可继续使用。最终摘要会说明这一生命周期。
7. `ENCRYPTION_KEY` 改变会影响旧密钥的再次显示；解密失败返回安全的中文 400，列表仍可读，仍可撤销并重建。鉴权按摘要进行，不依赖每次解密。

### 管理接口

所有响应涉及密钥元数据或明文时加 `Cache-Control: no-store`；创建和 reveal 的完整 Key 不进入日志、错误原文、URL 或浏览器持久存储。

| 方法与路径（前缀 /api/v1） | 行为 |
| --- | --- |
| `GET /api-keys` | 本人密钥列表，含掩码、授权小说 IDs、有效期、状态、创建和最近使用时间 |
| `POST /api-keys` | 创建，201，返回元数据及完整 Key；body 为 name、all_projects、project_ids、expires_at |
| `PATCH /api-keys/{id}` | 修改名称 / 小说授权 / 有效期；未传字段不变，expires_at=null 表示不过期 |
| `POST /api-keys/{id}/reveal` | 返回 `{key}`；仅本人，不能用 API Key 或其他用户的登录态调用 |
| `POST /api-keys/{id}/revoke` | 204；幂等撤销，清空密文，后续开放请求失效 |

管理界面创建默认选择“指定小说”，需要明确选定小说；可切换“全部自己的小说”，说明包含未来新增小说。有效期默认 90 天，提供 30 / 90 / 180 天、指定到期时间、不过期。API 的 expires_at 显式 null 为不过期，省略时默认 90 天；时间必须带时区且晚于当前时间。修改按最终有效配置校验，不能用部分字段绕过授权校验。

## 3. 开放接口鉴权与额度

- 使用 `APIKeyHeader(name='X-API-Key', scheme_name='PlatformAPIKey', auto_error=False)`，让实际运行鉴权和 OpenAPI 安全定义一致。
- 新增独立开放请求依赖，**不调用 get_current_user，也不读 Cookie 或 Bearer JWT 作为后备认证**。没有或无效 Key 统一中文 401。
- 在服务层按摘要查询，联合检查用户存在且 is_active、Key 未撤销、未过期；再构建最小上下文（key_id、user_id、是否全部小说及授权范围）。完整 Key 不传入下游业务服务。
- 每个小说路径先验证小说属于 Key 用户，再验证它在授权范围；不存在、他人小说、本人但未授权小说统一 404。子资源还要校验路径小说与资源 ID 的一致性。
- 有效 Key 不能进入内部管理或修改接口；仅 Cookie 不能进入开放读取接口。
- 增加 `OPEN_API_RATE_LIMIT_PER_MINUTE`（默认 120、正整数），只影响新增开放业务接口。计数和 last_used_at 使用数据库条件 UPDATE 原子更新并提交，不能使用 Python 先读后加，也不能用进程内字典冒充多 worker 全局额度。
- 固定窗口按 UTC 分钟计数，达到上限返回 429 和 Retry-After；文档说明分钟边界允许窗口突发。有效认证但业务返回 404 / 422 的请求同样消耗额度；无效 Key 和文档不消耗有效 Key 额度。
- 认证计数事务先短提交，再进行只读业务查询，不在下载期间持有密钥行写锁。并发撤销 / 到期需重验更新条件；失败不能一律误报 429。
- 可用 HTTPException 携带 Retry-After，复用现有处理器保留 headers 和中文 detail，不需要改变 AppError 全局格式。

## 4. 读取契约

新增开放响应 schemas 和 service 适配层。路由不查询 ORM；复用现有归属、序列化和导出能力，但不先读取全部列表再做 Python 分页。

统一约定：

- 对外小说路径和响应字段使用 `novel_id`；小说主键是现有 Project.id，不另造第二套 ID。子资源返回 `id` 与 `novel_id`，不同时暴露 project_id。
- 列表返回 `{items, total, page, page_size}`；page 从 1 开始，page_size 默认 20、范围 1–100，数据库 COUNT / LIMIT / OFFSET。
- 保留业务排序并以 id 打破平局；过滤后计算 total。所有 q 字面匹配需要转义 `%` / `_`，最长 100 字符。
- 对外时间为 UTC ISO 8601，带 Z / 时区；SQLite 返回无时区值时按既有 UTC 约定归一化。
- 只允许明确列出的领域字段，排除模型配置、提示词、向导进度、阅读进度、运行时任务及写作注入开关等内部信息。
- 章节读取含空章节与 draft / generating / done 状态；目录不带正文，单章 content 可为空。导出沿用现有跳过空章节与 `X-Export-Warnings` 行为，文档明确说明。
- 设定均返回当前保存值，可能包含后续剧情；不提供按阅读章节回溯历史状态。

### 小说、章节、封面和下载

以下均为 GET，前缀 `/api/open/v1`。

| 路径 | 输入及响应重点 |
| --- | --- |
| `/me` | Key 的 id、name、只读权限、授权小说范围、expires_at；不返回完整 Key、摘要、密文、账户管理信息 |
| `/novels` | q（书名）、status、分页；仅列授权小说，元数据及批量章节统计，避免 N+1 |
| `/novels/{novel_id}` | title、genre、premise、synopsis、theme、tags、selling_points、audience、target_words、status、chapter_count、total_words、created_at、updated_at、content_updated_at、cover_updated_at、cover_url |
| `/novels/{novel_id}/chapters` | 分页、updated_after（带时区时间）、status；排序 `(chapter_no, id)`；id、novel_id、chapter_no、title、status、word_count、updated_at、卷号 / 卷名，不含正文 |
| `/novels/{novel_id}/chapters/{chapter_id}` | 目录元信息 + content，校验小说 ID 与章节 ID |
| `/novels/{novel_id}/cover` | 真实图片 MIME 和字节，无封面 404；cover_url 为该受保护相对路径，不含 Key |
| `/novels/{novel_id}/export` | format=txt/md/epub，默认 txt；可选 chapter_ids 和 include_synopsis，复用 export_service；作者采用当前 Key 用户的用户名，保留下载文件名和空章节提示 |

`updated_at` 明确表示项目元数据更新时间，`content_updated_at` 为现存章节更新时间最大值（没有章节为 null）。不把 Project.updated_at 误称为全部小说内容的更新时间，不添加不可靠的小说级增量游标。updated_after 在首版仅用于章节行更新，语义为严格大于；删除和派生字段变更需要完整目录比对，不承诺完整增量同步。

### 创作设定

前缀 `/api/open/v1/novels/{novel_id}`；列表均分页，详情均有完整领域字段和行时间戳。具体字段白名单与复用入口见 `research/read-contracts.md`。

| GET 路径 | 行为与顺序 |
| --- | --- |
| `/characters`、`/characters/{character_id}` | 人物档案，列表可按 q 匹配姓名；排序 `(sort, id)` |
| `/world`、`/world/{setting_id}` | 世界观，可按 category 筛选，排序 `(sort, id)` |
| `/outlines`、`/outlines/{outline_id}` | 大纲及分卷字段，可按 volume_no 筛选；排序 `(volume_no, sort, chapter_no, id)` |
| `/outlines/{outline_id}/plans` | 细纲列表，排序 `(seq, id)`；复用 plan_outs 过滤已删除人物，并返回建章引用及 has_content |
| `/organizations`、`/organizations/{organization_id}` | 组织档案，含 parent_id 和 member_count；q 名称筛选，排序 `(sort, id)`；member_count 为全部状态成员数 |
| `/organizations/{organization_id}/members` | 成员及人物 / 组织名字，status 可选，默认全部状态；排序 id |
| `/relationships`、`/relationships/{relationship_id}` | 单条关系不拆成重复的双向记录；嵌入人物名字和类型称呼；可按 character_id / status / category 筛选，保留视角 self_role / other_role 语义；排序 id |
| `/relationship-types` | 全局预置 + 当前小说自定义类型，不能出现其他小说自定义类型；预置优先，再按 `(sort, id)` |

新增适配层以现有序列化函数作为派生字段的唯一来源，分页后再批量补充成员数量、关系人物和类型等信息。不能把 schema 中暂缺 updated_at 当作 ORM 没有该字段。细纲的 JSON 人物引用必须经过现有清理，不能直接返回已删除人物 ID。

## 5. OpenAPI 描述与文档

- 开放读取路由使用独立 APIRouter，注册在同一个 FastAPI app；与当前 app.state / db / 错误处理复用，不额外建立缺少状态和 lifespan 的子应用。
- 用 `get_openapi(..., routes=open_router.routes)` 生成并按 app 实例缓存专属描述；不替换现有 app.openapi。只收录开放业务接口，文档路由标记 include_in_schema=False。
- `/api/open/v1/docs` 通过 `get_swagger_ui_html` 指向上述 JSON，提供 Authorize 和 Try it out；关闭 persistAuthorization，不在页面代码或示例内注入真实密钥。
- `/api/open/v1/openapi.json` 可独立下载、导入第三方工具；所有业务操作带正确 security、operationId、参数边界、响应 schema、401 / 404 / 422 / 429 错误，图片与下载声明实际 media type 和二进制格式。
- 请求根路径遵守应用部署 root_path，路径前缀只出现一次；文档和读取路由在 SPA fallback 前注册，避免生产构建返回 index.html。
- 中文描述覆盖分页、稳定排序、授权小说、空章与导出、当前剧情设定、时间戳及更新筛选边界。提供使用 `X-API-Key` 的 curl 示例，示例仅用占位符。
- 前端管理页有“接口文档”和“下载 OpenAPI JSON”入口；README 增加对接说明和 Key 与模型供应商凭据的区别。

## 6. 前端交互

- 路由级懒加载 API Key 管理页，加入全局导航的资源区域；沿用 StudioUI、主题 token、useIsMobile、现有 api client 与 App.useApp。
- 列表展示名称、掩码、小说授权、状态、有效期、创建 / 最近使用时间和操作；移动端使用可折叠卡片或原项目已有响应布局，375px 不横向溢出。
- 显示与复制分别有可访问名称的眼睛 / 复制图标。点击显示才请求 reveal；点击隐藏立即移除本地明文。复制在隐藏状态也能主动请求 reveal 后复制，不强制先显示。
- Clipboard API 不可用或失去用户激活时，使用明确的只读密钥弹窗和可选择文本供手动复制；不能只提示失败而不给可完成的路径。不得使用 toast / 日志回显完整 Key。
- 明文仅保存在本页面局部状态；刷新、离开、登出、撤销或隐藏后清除，不写 localStorage / sessionStorage / 全局 store。
- reveal 请求尚未结束时用户隐藏、离开或撤销，迟到响应不能重新显示 Key；按请求标识 / AbortSignal 丢弃失效结果。操作有 loading，失败显示既有中文错误消息。
- 新建 / 编辑弹窗支持小说范围与有效期；撤销需要明确操作确认，撤销后按钮禁用且立刻移除本地明文。

## 7. 兼容、风险与回退

- 迁移只新增凭据及授权表，不改现有用户会话和小说结构。降级删除新表；先撤下新入口与路由，再降级，原有业务可继续使用。
- ENCRYPTION_KEY 必须备份；已保存 Key 的再次展示依赖其稳定性。原值更换后无法解密，用户可撤销并重建。
- 基于数据库的固定窗口会增加开放请求的一次短事务；默认 120 次 / 分钟，不新增 Redis。该约束不会影响现有页面请求。
- 明文可恢复是用户明确要求；通过所有者专用 reveal、默认掩码、no-store 和前端局部状态控制暴露范围。
- `api_key_projects` 必须排除项目备份；文档 schema 不含任何密钥管理响应和内部管理端点。
- 不建立发布权限、公开阅读、历史剧情快照、webhook 或完整同步事件流；如后续需要，单独扩展契约。

## 8. 设计依据

- 仓库证据：`research/current-inventory.md`、`research/read-contracts.md`、`research/key-auth-docs.md`。
- [Fernet 官方文档](https://cryptography.io/en/latest/fernet/)：现有 SecretCipher 可完成经过认证的加解密。
- [Python secrets 官方文档（3.12）](https://docs.python.org/3.12/library/secrets.html)：使用显式 32 字节安全随机 token。
- [FastAPI OpenAPI 扩展](https://fastapi.tiangolo.com/how-to/extending-openapi/)、[文档 HTML 工具](https://fastapi.tiangolo.com/reference/openapi/docs/)、[APIKeyHeader](https://fastapi.tiangolo.com/reference/security/#fastapi.security.APIKeyHeader)：从实际路由生成描述与交互文档，复用安全依赖。

## 9. 客户开发文档扩展（用户追加）

- 公开懒加载路由 `/developers`，与 `/` 一样置于 RequireAuth / AppLayout 之外；首页官网导航在“常见问题”后增加“开发文档”。
- 文档延续 StudioBrand、主题 token、首页纸面 / 墨绿色 / 衬线标题；采用页头、文档导言、桌面目录与正文的阅读布局，移动端目录可折叠或自然换行，不隐藏主要内容。
- 开发指南涵盖快速开始、API Key、20 个 GET 接口、统一分页 / UTC 时间 / 筛选、实际成功响应示例、错误与 Retry-After、封面和三种导出语义。详细字段以现有 Swagger 为权威，并提供直达入口及 JSON 下载。
- 代码只使用 `YOUR_API_KEY` 等明显占位符；默认 Base URL 使用当前网站 origin 的 `/api/open/v1`，不把真实 Key 收集到文档页面，也不在文档页发送业务请求。需要实际请求时转到 Swagger 由客户主动授权。
- 代码复制只复制公开示例，失败时保留可选中的代码及明确反馈；无需引入新依赖、通用文档框架或 Markdown 渲染器。
- 具体边界、文件所有权、内容核对和验收见 `research/public-developer-docs.md`。
