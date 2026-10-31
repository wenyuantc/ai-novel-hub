# 平台 API Key 与开放读取

## 1. 范围与触发

适用于平台生成的访问 Key、`/api/v1/api-keys` 管理端、`/api/open/v1` 只读接口及专属 OpenAPI 文档。新增对外读取资源、调整凭据生命周期、授权范围或文档路径时必须遵守本契约。

平台 Key 与 `ai_configs` 中用于调用模型供应商的凭据分别管理；网页登录继续使用原 `CurrentUser`（Cookie / JWT），不要让全部内部接口接受平台 Key。

## 2. 签名与存储

```python
# backend/app/core/open_api_deps.py
OpenApiUser = Annotated[ApiKeyPrincipal, Depends(get_api_key_principal)]

# backend/app/services/api_key_service.py
async def authenticate_key(db, raw_key: str | None, limit_per_minute: int) -> ApiKeyPrincipal
async def get_authorized_project(db, principal: ApiKeyPrincipal, project_id: int) -> Project
```

`ApiKeyPrincipal` 仅含 key_id、name、user_id、username、all_projects、project_ids、expires_at，不携带完整 Key、密文或摘要。开放路由位于 `app/api/open/`，业务查询与序列化位于 `app/services/open_api/`，字段白名单位于 `app/schemas/open_api.py`。

0024回收站接入后，KeyInfo/ApiKeyPrincipal及密钥列表的project_ids仅投影正常作品；软删除不销毁底层api_key_projects授权，纯改名也不撤销隐藏授权，恢复后按原权限重新可用。列表/正文/封面/下载仍需独立生命周期校验，不能只靠保留的grant放行。详见[作品回收站](./recycle-bin.md)。

前端 `buildApiKeyInput` 仍负责创建（指定范围至少1部），编辑使用 `buildApiKeyUpdate` / `hasApiKeyScopeChanged`：按all_projects及规范化ID集合比较可见范围，未变时PATCH只提交元数据，省略all_projects/project_ids，投影为空也可改名/有效期；明确修改范围才完整替换。不能继续编辑整表必传scope，否则只含回收站的Key无法改名、混合Key会丢隐藏授权。表单说明“未调整范围保留原授权（含暂不可见作品）；更改范围按当前选择替换”，测试覆盖空/混合投影、顺序/重复ID和创建校验。

迁移 `0022_api_keys`（前驱 `0021`）：

- `api_keys`：user_id CASCADE、name、key_hash（唯一 SHA-256）、key_enc（Fernet，可空）、key_masked、all_projects、expires_at、revoked_at、last_used_at、rate_window、rate_count、时间戳。
- `api_key_projects`：`(api_key_id, project_id)` 复合主键，两个外键均 CASCADE。项目删除后授权可为空，仍是指定模式，不得解释成全部授权。
- api_key_projects 引用项目，登记在项目备份 `NOT_EXPORTED`；api_keys 只引用用户，不登记。任何导出都不携带访问 Key 或授权。

### 管理接口（前缀 `/api/v1`）

| 方法与路径 | 响应 |
| --- | --- |
| `GET /api-keys` | 本人 ApiKeyOut 数组，仅掩码 |
| `POST /api-keys` | 201 ApiKeyCreateOut，元信息加完整 key |
| `PATCH /api-keys/{key_id}` | 更新 name / all_projects / project_ids / expires_at，返回 ApiKeyOut |
| `POST /api-keys/{key_id}/reveal` | `{key: string}`，本人主动查看 / 复制 |
| `POST /api-keys/{key_id}/revoke` | 204，幂等永久撤销 |

ApiKeyOut 包含 id、name、key_masked、all_projects、project_ids、status（active / expired / revoked）、expires_at、revoked_at、last_used_at、created_at、updated_at。expires_at / revoked_at / last_used_at 可空；响应不含 key_hash / key_enc。

### 开放读取接口（均 GET，前缀 `/api/open/v1`）

- `/me`；`/novels`；`/novels/{novel_id}`。
- 小说下 `/chapters`、`/chapters/{chapter_id}`、`/cover`、`/export`。
- 小说下 `/characters` 与单条详情、`/world` 与单条详情、`/outlines` 与单条详情、`/outlines/{outline_id}/plans`。
- 小说下 `/organizations` 与单条详情、`/organizations/{organization_id}/members`。
- 小说下 `/relationships` 与单条详情、`/relationship-types`。
- `/docs` 为 Swagger UI，`/openapi.json` 为专属机器描述；不属于需 Key 的业务操作。

## 3. 可执行契约

### 密钥与身份

- 生成 `anv_` + `secrets.token_urlsafe(32)`；密文用于再次显示，摘要用于索引鉴权，掩码用于列表。不得改为只显示一次，也不得把明文默认载入全部列表。
- 创建默认指定小说并要求非空选择；all_projects=true 表示本人的全部小说和未来新建小说，并清空无意义的 project_ids。指定的每个项目须归属本人。
- 名称去首尾空白后 1–100 字符；创建 expires_at 省略默认 90 天，null 不过期；时间必须带时区且晚于当前时间。PATCH 未传保持原值，expires_at=null 清空；非空字段显式 null 拒绝。
- 过期但未撤销的 Key 可再次显示和调整有效期。撤销后清空 key_enc，禁止再次显示或修改；需要轮换时创建新 Key。
- 账号禁用 / 删除阻止认证；Key 与 JWT token_version 独立，登出、改密 / 重置不自动撤销。重新启用账号后，未过期 / 未撤销的 Key 可继续使用。
- 管理端仅现有登录认证，按 user_id 校验，即使管理员也不能查看其他人的 Key。所有密钥管理正常响应 `Cache-Control: no-store`。
- 开放端只接受 `X-API-Key`，APIKeyHeader scheme_name 为 PlatformAPIKey；Cookie 或 Bearer JWT 不作后备，Key 也不能用于内部接口。
- 每个小说路径先 `get_authorized_project`，再校验小说与子资源 ID；跨用户、未授权和不存在均为 404。

### 限流与时间

- `OPEN_API_RATE_LIMIT_PER_MINUTE` 默认 120、正整数；只影响新增开放业务接口。
- 数据库条件 UPDATE 原子完成有效性检查、固定 UTC 分钟窗口计数和 last_used_at，随后短提交再读取业务；禁止 Python 先读后加或进程内计数。调用统计不更新 Key 配置的 updated_at。
- 已存窗口只能前进：仅在 rate_window 小于本次窗口时重置计数；迟到的旧分钟请求按已存的新窗口扣额度，不能用 `rate_window != window` 把窗口倒退并绕过上限。last_used_at 取非递减值；429 根据实际受限窗口和提交后的当前时间计算 Retry-After。
- 数据库锁等待可能跨越到期时间：事务提交后用最新时刻再次校验 expires_at；额度更新失败后也用最新时刻区分失效 Key 与 429，不能只用请求开始时间。
- 达上限 429，Retry-After 给出至少等待秒数；无效 / 已失效 Key 不误报 429。已认证的业务错误同样消耗额度。窗口边界可突发，不能承诺滑动窗口。
- 对外时间为 UTC ISO 8601；数据库中无时区时间按既有 UTC 约定归一化。

### 领域读取

- 列表 `{items, total, page, page_size}`；page ≥1，page_size 默认 20、范围 1–100；SQL COUNT / LIMIT / OFFSET，不先读全量再切片。q 为字面量子串匹配，转义 `%` / `_`。
- 子资源输出 novel_id 映射 project_id，不同时暴露两套字段；仅显式领域字段，不返回运行时任务、模型配置、提示词、向导 / 阅读进度或写作注入开关。
- 小说 updated_at 仅元数据，content_updated_at 为现存章节更新时间最大值；无章节为 null，不等于完整小说变更游标。
- 章节包含空章及 generating 状态，目录无正文，单章 content 可为空；排序 `(chapter_no, id)`。updated_after 要求带时区、严格大于，仅行更新筛选，不包含删除或关联字段变化。
- 下载复用 export_service 的 txt / md / epub、文件名及跳过空章提示；封面 / 下载仍需 Key，URL 不携带 Key，响应 no-store。
- 大纲排序 `(volume_no, sort, chapter_no, id)`，细纲 `(seq, id)`；细纲复用 plan_outs 清理已删除人物引用。
- 组织成员默认全部状态，member_count 为所有状态总数；关系一对人物仅一条，视角角色称呼复用 to_outs，类型限全局预置 + 当前小说。
- 所有创作设定是当前保存值，可能含后续剧情；不要承诺按阅读章节回溯状态或完整增量同步。

### 文档与前端

- 由实际开放 APIRouter routes 生成专属 get_openapi，按 app 实例缓存；不替换原 app.openapi。文档不含管理 / reveal / 生成端点。
- 文档可匿名查看，实际业务始终 Key 鉴权；Swagger persistAuthorization=false，示例只用占位 Key。部署 root_path 只拼一次，路由在 SPA fallback 前注册。
- `/developers` 为官网风格的公开客户开发指南，首页“常见问题”后提供入口；与受保护的工作区路由分离。指南覆盖接入、鉴权、20 个 GET 接口、分页 / 时间、错误 / 限流与图片 / 导出，详细字段和调试链接到现有 Swagger，JSON 下载链接不变。
- 公开指南只显示说明和占位示例，不读取私人作品或密钥，不收集或持久化真实 Key；代码复制只处理公开示例。调用示例以实际路径 / schema 为准，不能鼓励将长期 Key 打包进公开前端或放进图片 URL。
- `/settings/api-keys` 是懒加载管理页；列表默认掩码，点击显示 / 复制才主动请求 reveal。复制在隐藏状态也可操作；受限时提供可选择文本的手动复制弹窗。
- 明文只放页面局部状态，隐藏、离开、登出、撤销后清除；迟到 reveal 响应不得恢复明文。不要放全局 store 或浏览器持久存储。
- 新图标按钮有可访问名称，主题 / 375px 遵守前端组件规范；管理请求继续使用统一 API client。

## 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 缺失 / 无效 Key、过期、撤销、禁用或已删除用户 | 401，中文 detail，不回显 Key |
| Cookie 有效但没有 X-API-Key | 401 |
| Key 访问内部需登录接口 | 401，不能用 Key 替代 JWT |
| 他人 Key / 小说、未授权小说、路径小说与子资源不符 | 404 |
| 指定授权为空 | 400，提示选择小说或全部授权 |
| 名称非法、显式 null 非空字段、无时区 / 过去到期时间、分页越界 | 422 |
| 撤销后 reveal / PATCH | 400；重复 revoke 仍 204 |
| ENCRYPTION_KEY 更换导致 reveal 解密失败 | 400 安全文案；掩码列表和摘要认证不依赖解密，仍可撤销重建 |
| 当前 Key 分钟额度用完 | 429 + Retry-After，其他 Key 独立 |
| 无封面 / 无可导出正文 | 分别为 404 / 400，沿用已有业务语义 |

## 5. 正例、基准与反例

- 正例：创建只授权小说 A 的 Key → 刷新后显示 / 复制 → X-API-Key 读取 A → 请求 B 为 404 → 撤销后 A 为 401。
- 基准：改登录密码后 Key 仍可使用；禁用账号后 Key 不可使用；过期后可显示并通过编辑延长有效期。
- 反例：把平台 Key 加进 CurrentUser 的后备认证，会把内部写入和管理接口一并开放；不能这样实现。

## 6. 必需测试

- `tests/test_api_keys.py`：创建密文 / 摘要、列表掩码、再次 reveal、no-store、跨用户及管理员隔离、范围 / 过期 / 撤销、空授权不扩权、账号状态与会话独立、并发额度和跨分钟恢复、迟到旧窗口不得重置新窗口、数据库等待跨到期、加密密钥变化。
- `tests/test_open_api.py`：20 个操作的实际响应、逐资源隔离与混用 ID、分页 / 筛选 / UTC、细纲引用、组织和关系派生字段、封面与三种下载、专属 schema 精确路径 / 安全定义、root_path、SPA fallback。
- `tests/test_migrations.py` 与项目备份注册表：0022 升降级、唯一摘要、外键级联，授权表不入备份。
- 前端 apiKeys / apiKeySecrets 测试：接口契约、显隐 / 复制异步失效、范围与有效期转换；实际浏览器验证刷新后查看、复制、文档调试、撤销、主题和移动布局。

## 7. 错误与正确实现

```python
# 错误：Key 只鉴权，遗漏小说授权；或直接复用内部 CurrentUser。
return await chapter_service.get_project_chapter(db, user.id, novel_id, chapter_id)

# 正确：独立 Key 主体，先 owner + Key scope，再做嵌套资源校验。
await get_authorized_project(db, principal, novel_id)
return await chapter_service.get_project_chapter(db, principal.user_id, novel_id, chapter_id)
```

```typescript
// 错误：默认获取所有明文，或把迟到结果重新展示。
localStorage.setItem('apiKeys', JSON.stringify(keysWithPlaintext))

// 正确：仅主动请求 reveal；局部状态在隐藏 / 撤销 / 离开时失效并清理。
const result = await apiKeyApi.reveal(keyId)
// 只有仍匹配本次用户意图的请求才允许进入局部显示状态。
```
