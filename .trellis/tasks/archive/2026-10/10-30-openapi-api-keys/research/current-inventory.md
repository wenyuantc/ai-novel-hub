# 开放接口与 API Key 现状调研

调研日期：2026-10-30。当前仅规划，以下不是已实现能力；用户后续确认的范围以 prd.md / design.md 为准。

## 仓库证据

| 能力 | 现有证据 | 对本次规划的影响 |
| --- | --- | --- |
| 平台登录认证 | `backend/app/core/deps.py:60` 的 `_extract_token`、`:71` 的 `get_current_user`；Cookie 优先，其次 Bearer JWT，校验 `is_active` 与 `token_version` | 外部 API Key 应有独立认证入口，不能直接扩展为全部内部接口的通行证；也不应由已有 Cookie 掩盖无效 Key |
| 小说列表与详情 | `backend/app/api/v1/projects.py:13`、`:24`；`backend/app/schemas/project.py:114`；`backend/app/services/project_service.py:22`、`:151`、`:170` | 复用作品信息与用户归属校验，外部响应应限定字段，不直接暴露向导进度、内部配置及阅读进度 |
| 章节列表与正文 | `backend/app/api/v1/chapters.py:22`、`:43`；`backend/app/schemas/chapter.py:34`、`:51`；`backend/app/services/chapter_service.py:41`、`:49` | 可复用归属校验；外部目录不应携带所有正文；目录与正文需定义一致的可见范围 |
| 分卷目录 | `backend/app/api/v1/reader.py:16`；`backend/app/services/reader_service.py:37`；`backend/app/schemas/reader.py:9` | 现有目录仅列出正文非空的章节，带卷号、卷名；它没有更新时间和分页信息，不能直接作为完整外部契约 |
| 封面 | `backend/app/api/v1/project_cover.py:14` | 开放独立的受保护图片读取接口；不通过 URL 查询参数传递密钥 |
| 全文下载 | `backend/app/api/v1/projects.py:50`；`backend/app/services/export_service.py:96` | 已支持 TXT / Markdown / EPUB，可作为阅读和同步接入的直接补充，不等同于开放完整项目备份 |
| 创作设定 | `backend/app/api/v1/story.py:74`、`:75`、`:78`；`backend/app/api/v1/outline_pro.py:44`、`:109`；`backend/app/api/v1/relationships.py:42`、`:89`；`backend/app/api/v1/organizations.py:26` | 世界观、人物、大纲、细纲、组织、关系已有读取能力；适用于外部创作工具，包含未写剧情，应单独讨论开放范围 |
| AI 生成 | `backend/app/api/v1/chapters.py:74`、`:86` | 现有接口会创建异步任务并产生模型费用；若要开放，需要另定生成权限和任务状态查询，不能归入只读 Key |
| 配置入口 | `frontend/src/router.tsx:47` 的设置路由；`frontend/src/components/StudioNavigation.tsx:17` | 可新增全局 API Key 管理页，遵循现有页面布局与移动端导航 |

## 需要明确的契约

- 建议版本化对外前缀 `/api/open/v1`；以下为候选，只在范围确认后冻结：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /novels` | 已授权小说列表；关键词、状态筛选与分页 |
| `GET /novels/{novel_id}` | 书名、简介、类型、标签、状态、字数、章节数、更新时间等 |
| `GET /novels/{novel_id}/chapters` | 章节目录，含分卷、顺序、标题、字数与更新时间；支持分页和更新时间筛选 |
| `GET /novels/{novel_id}/chapters/{chapter_id}` | 单章正文，附稳定章节 ID、章号和元信息 |
| `GET /novels/{novel_id}/cover` | 封面图片 |
| `GET /novels/{novel_id}/export` | TXT / Markdown / EPUB 全文下载 |
| `GET /me` | 当前密钥名称、权限范围与有效期，供调用方测试连通性；不返回完整密钥和账户私密资料 |

- 现有列表为裸数组且无分页。对外分页契约可独立设计，保留内部页面接口兼容。
- `backend/app/models/base.py:19` 的时间戳只随对应行更新。`chapter_service.py` 的章节编辑没有同步更新 `Project.updated_at`。如提供小说的“内容更新时间”，必须从章节 / 封面等相关数据正确聚合，不能直接把项目更新时间当作章节更新时间。
- `updated_after` 只能表示尚存在记录的新增与更新；当前未发现删除事件、同步游标或 tombstone 机制。不得把该筛选承诺为完整增量同步，删除可通过重新读取完整目录比对或后续独立机制处理。
- 现有章节只有 `draft / generating / done` 状态，没有小说发布和公开阅读权限模型。API Key 开放读取仍是账号授权的私有内容访问。
- 现有仓库未发现通用接口限流实现。是否首版加入按密钥限流，需在正式设计中明确存储和部署约束，不默认引入新基础设施。

## API Key 管理建议

- 每个用户可生成多把 Key，命名以区分第三方应用，配置有效期和授权小说范围。
- 用户已确认创建后仍可再次显示 / 复制：默认列表只返回掩码、创建时间、到期时间、最后使用时间、撤销状态，通过所有者专用 reveal 获取完整 Key。
- 最新设计为“Fernet 密文 + 不可逆摘要”：密文用于用户主动再次查看，摘要用于索引校验。先前仅存摘要、只展示一次的建议已被用户要求替代。
- 管理端依赖现有网页登录认证；开放端只接受请求头中的平台 Key，并对每个资源同时检查密钥授权范围与账号所有权。
- 被禁用或删除的账号不能继续访问。最终设计将平台 Key 与 JWT 会话版本分离：登出、改密 / 重置不自动撤销 Key，显式撤销永久失效，详见 design.md 的生命周期。
- 首版只读，人物、大纲、世界观、组织、关系已确认纳入；生成 / 修改不在本轮范围。

## 官方资料（2026-10-30 核对）

- [FastAPI Security Tools / APIKeyHeader](https://fastapi.tiangolo.com/reference/security/#fastapi.security.APIKeyHeader)：请求头型 API Key 依赖可纳入 OpenAPI 安全方案；该工具负责提取，不替代密钥的签发、校验或资源授权。
- [OWASP REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html#api-keys)：每个受保护端点检查凭据，支持撤销，并在请求过快时返回 429。
- [OWASP 敏感请求信息](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html#sensitive-information-in-http-requests)：API Key 不应放进 URL，读取请求使用请求头。

## 最新确认与后续

用户已确认扩展只读范围和 OpenAPI 文档；密钥可再次显隐和复制。详见最终 PRD 与设计，下一步提交完整规划摘要审阅，获后续明确批准后实施。
