# 客户开发文档扩展

## 授权与边界

用户在当前任务实现后明确追加：“我还需要在首页增加一个给客户看的开发文档，ui风格要一致”，并在首页顶部“常见问题”之后标出位置。延续当前任务，直接实施这项公开文档扩展，不更改 API 权限或数据结构。

当前缺口是：首页没有开发文档入口，Swagger 有完整字段参考但未提供首页风格的客户接入指南。行为位于公开路由、HomePage 导航及新增文档页面。

## 实施所有权

trellis-implement 负责：

- `frontend/src/pages/HomePage.tsx`：在“常见问题”后插入指向 `/developers` 的导航，必要时增加页脚同一入口。
- `frontend/src/pages/HomePage.css`：仅为新增导航在各断点的排列与焦点反馈做必要修改，不能影响现有功能。
- `frontend/src/pages/DeveloperDocsPage.tsx`、`DeveloperDocsPage.css`：公开文档页，语义 DOM、现有主题 token、StudioBrand。可将内容数据 / 代码块拆到 `components/developerDocs/`，保持组件职责清晰。
- `frontend/src/router.tsx`：公开懒加载 `/developers`，保留此前 API 密钥页及所有路由。
- 文档有需要独立验证的示例生成 / 复制逻辑时，允许在 `utils/developerDocs.ts` 与相邻测试中维护；静态内容和低影响样式不写镜像测试。

主会话负责：任务文档、README、规范、提交分组、真实浏览器验收。你并不独自在仓库工作，不得回退他人的既有修改；不提交或暂存，不修改后端或真实用户实例。

## 页面与视觉

- 标题“开发文档”，简短说明为阅读器与内容工具接入已授权作品；不是又一页营销首页。
- 首页已有 `HomePage.css` 的 `.public-home`、`.home-header`、`.home-navigation`、`.home-button`、`.home-section` 是视觉参考，StudioBrand 为品牌唯一实现。样式必须有作用域、使用 token、支持暗色；不要新造颜色或重置全站样式。
- 页头保留品牌 / 返回首页与开发文档当前状态；正文桌面左目录、右文章，目录包含快速开始、鉴权、接口、分页、响应、错误与下载等；移动端目录有可用路径。
- 标题用 `--font-display`，正文用现有字体，清晰节次、细边框和适当留白；长端点与代码块在自身容器滚动，页面不溢出。
- 锚点有语义链接、可见焦点、正确 h1/h2/h3 与 reduced-motion；新增首页导航在320px也不能溢出。

## 内容契约（以代码为准，必须核对）

- 对外前缀 `/api/open/v1`；所有业务接口 GET，`X-API-Key` 必需；Cookie/Bearer 不能替代，平台 Key 与模型供应商 Key 不通用。
- 引导客户登录后进入 `/settings/api-keys`，创建命名 Key，选小说 / 有效期；创建默认指定小说、90天；之后可眼睛显示和复制。登出 / 改密不撤销，过期 / 撤销 / 账号不可用返回401，撤销不可恢复。
- 20 路径精确来自 `backend/app/api/open/__init__.py`，不能把内部 `/api/v1` 管理接口混入目录。列表 / 详情可分组但路径全部可找到。
- 至少给出 curl 检查 `/me`、获取小说和章节正文示例；可提供 JavaScript 服务端示例，密钥仅占位符。获取小说实际 id 后再获取该小说的章节实际 id，不引导客户以 chapter_no 充当 chapter_id。
- `Page` 为 `{items,total,page,page_size}`，page默认1，page_size默认20/最多100；不要承诺所有接口有相同筛选参数。
- `updated_after` 仅用于章节行严格晚于指定带时区时间；不能涵盖删除或关联字段变更。`updated_at`为小说元数据，`content_updated_at`为现存章节最大更新时间。
- 默认额度120请求/UTC分钟/Key，可由部署配置更改；429按Retry-After等待。身份认证成功后的404/422也计数；分钟边界可能突发。
- 当前设定可包含后续剧情；章节目录无正文、可能有空章/生成中；只读，无生成或写入。
- 封面需带Key读取字节，不能直接把带密钥URL放img，也不能在URL中传Key；导出format=txt/md/epub，生成中和空白章节跳过，EPUB提示头`X-Export-Warnings`为百分号编码。
- 错误400/401/404/422/429的说明和422的实际结构需查 `backend/app/schemas/open_api.py` 与实际错误处理；不要虚构 `data` / `code` 包装。
- Swagger `/api/open/v1/docs`、下载 `/api/open/v1/openapi.json` 保留；公开指南无需加载schema即可阅读，不增加新的 API 请求或认证状态门禁。

## 验证

- 前端 lint / build / 全量 test；代码复制或示例格式等非平凡逻辑有针对性测试，静态文案不写字符串镜像测试。
- 浏览器匿名：首页入口、公开直达、刷新、目录锚点、复制公开代码、返回首页、登录后管理密钥入口及原首页登录 / 创作行为不变。
- 320/375/768/1024/1440px 明暗主题、键盘可见焦点、console无新错误、独立路由懒加载和无包体积警告。
- 原完整后端1061项已通过且本扩展不改后端；必要时跑现有 OpenAPI 契约测试核对页面数据，不无理由重跑后端全套。
