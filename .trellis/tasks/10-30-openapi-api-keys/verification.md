# 实施与验证记录

## 阶段

- 用户已批准最终方案，分支 `codex/openapi-api-keys`，任务状态 `in_progress`。
- 三个 implement 子代理分别拥有密钥基座、开放读取与文档、前端管理页；主会话负责 README、规范、集成与收尾。
- 初始工作区仅有已有未跟踪文件 `pelican-bicycle.html`，不属于本任务。

## 实施结果

- 五个实施切片均完成，原范围全量门禁和追加公开开发文档的最终检查均完成，未发现剩余需修复的本任务产品问题。
- 规划产物和上下文清单经过 task.py validate；原长规范的任务相关段落已逐字摘录，避免单文件注入截断。
- README 和项目规范已按最终实现补充开放接口、密钥配置、调用示例、生命周期、限流边界和文档入口。
- 新增 0022 迁移完整升降级通过；备份注册表明确排除 API Key 及其授权关系。
- 用户于2026-10-30明确授权“提交 归档”；工作提交按commit-plan.md四组执行，之后归档当前任务并记录日志，不推送或部署。

## 验收环境

- 使用单独的本地 8766 端口、独立临时 SQLite、测试用户与虚构作品，不使用现有 8000 端口上的实例或用户真实作品。
- 复用 FakeLLM，任何意外触发的生成不访问真实模型；不读取用户真实凭据。
- 浏览器使用当前聊天的隔离 IAB；检查桌面 / 375px、明暗主题、显隐复制、刷新、Swagger 调试和撤销结果。
- macOS 宿主的 `LC_ALL=C.UTF-8` 不可用；验证命令采用 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`，不为修复本机 locale 改产品代码。

## 全范围检查中的发现

- 首次后端完整运行：1059 项收集，1058 passed / 1 failed。失败发生在既有 `test_cross_user_isolation` 的准备阶段：知识库 kb.index 任务等待 5 秒超时，而不是资源隔离断言；该用例不改代码单独复跑 1 passed（1.33 秒）。保留失败证据，未通过放宽超时或跳过用例处理。
- 额度复核发现实际边界：暂停旧分钟请求，先执行新分钟请求，再放行旧请求，会把 rate_window 倒退并再次重置新分钟额度。limit=1 时三次请求均 200，复现确认；已以窗口单向推进、迟到请求扣现存额度、last_used_at 非递减和准确 Retry-After 修复，新增 limit=1 / 2 两组回归先失败后通过。
- 修复后的密钥 / 开放 HTTP 41 项通过（28.25 秒），Ruff 通过。第二次修复前完整运行已在 817 passed 后主动停止，退出 2，不作为最终门禁；旧索引超时用例在该次运行亦通过。
- 所有产品行为与已批准范围保持一致；最终仅运行一份修复后完整后端门禁。

## 浏览器证据

- 在独立 8766 临时实例上创建只授权小说 A 的 Key，默认列表为掩码；眼睛图标显隐、隐藏状态复制到剪贴板完整值、刷新后再次显示同一把 Key 均通过。
- Swagger UI 用该 Key 真实请求小说列表：200，仅一部授权小说；请求同用户另一部未授权小说：404；撤销后原 Key 请求授权小说：401，界面显隐 / 复制 / 编辑按钮禁用。
- 1280px 桌面与 375px 移动端、明暗主题均无页面横向溢出；管理页 console 未出现 warn/error。截图保存于本聊天的 visualizations 输出目录，未包含完整 Key。
- OpenAPI JSON 实际 HTTP 下载、解析和路径 / 安全检查通过：20 个 GET 操作、X-API-Key、无内部管理路径，并保存独立可下载文件。
- IAB 对管理页 `<a download>` 的 download 事件监听超时，页面未变化且无错误；不把工具事件支持情况当成产品缺陷。文件本身的 HTTP 下载与 Swagger 加载已验证。
- 剪贴板受限回退的自动化测试覆盖 Clipboard API 不存在、NotAllowedError 和返回可供手动复制的完整值。额外尝试 HTTP `0.0.0.0` 以及仅 QA 服务的 `Permissions-Policy: clipboard-write=()`，IAB 仍显示复制成功，未能触发手动复制弹窗；不声称该弹窗已完成真实浏览器实测。
- QA 标签页、视口覆盖和浏览器剪贴板均已清理；8766 临时服务已停止，其独立数据库与加密密钥已删除。未重启或迁移原有 8000 实例。

## 最终门禁

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 后端静态检查 | `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .` | exit 0 |
| 后端目标回归 | `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_api_keys.py tests/test_open_api.py -q` | exit 0，41 passed，28.25 秒 |
| 后端完整测试 | `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest` | exit 0，1061 passed，380.72 秒；1 条既有 Gemini SDK 弃用警告 |
| 前端静态检查 | `npm run lint` | exit 0 |
| 前端类型与构建 | `npm run build` | exit 0，含 `tsc -b`，无包体积警告 |
| 前端完整测试 | `npm test` | 原四切片 exit 0，57 个文件、444 项通过；追加公开文档后的最终结果为58个文件、447项通过 |

前端门禁完成后未修改前端产品代码；后端最终完整门禁在限流修复后单独运行。详细审核见 research/full-check.md。以上两项浏览器工具观察限制已保留，不能当作已实测通过。提交与归档已获用户明确授权。

原四切片收尾复核：task.py validate 通过（implement 18 项 / check 15 项），git diff --check 通过；当时61个本任务变更路径全部匹配提交清单，唯一排除文件为原有 pelican-bicycle.html；当次8766实例已清理。

## 用户追加公开开发文档

- 用户要求首页增加面向客户的开发文档并保持 UI 风格一致，截图明确顶部“常见问题”之后的导航位置。已追加 R7 / A12 与切片 5，在同一任务继续实施，不变更后端权限或数据结构。
- 公开 `/developers` 指南由前端 implement 代理完成，主会话完成规范、README、独立浏览器与收尾；先前提交清单尚未执行，现已纳入追加文件。
- 追加后的上下文 validate 通过（implement 19 项 / check 16 项）。开放契约复核 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_open_api.py -q`：21 passed，16.77 秒。
- 本切片前端最终门禁：`npm run lint` exit0、`npm run build` exit0（包含tsc -b，独立DeveloperDocsPage chunk，无依赖新增或包体积警告）；`npm test` exit0，58文件/447项通过，3.66秒，新增3项公开代码复制测试。
- reviewer完成全任务影响复核，没有新增产品修复；AST比较文档目录与实际路由20路径精确一致，公开Page/Chapter/422样例通过实际Pydantic schema。详见research/public-docs-check.md。
- 独立8766实例实测：匿名首页“常见问题”后进入/developers、直接刷新、返回首页；原首页登录和开始创作欢迎弹窗保持可用。
- 首页与文档各自检查320/375/768/1024/1440px明暗主题：document.scrollWidth与clientWidth相等，首页第五项可见、320px自然换行；长代码和接口表只在自身容器滚动。桌面目录和移动可折叠目录均可定位正文；#errors标题距顶部约67px，移动#endpoints约59px。
- 示例复制的最终实际剪贴板与Shell公开代码逐字一致，含换行与YOUR_API_KEY；Tab聚焦返回首页时focus-visible=true；Enter激活管理密钥进入登录，登录后回到/settings/api-keys。
- Swagger入口实际打开专属文档；公开指南HTTP200/text-html，OpenAPI JSON实际200/application-json，20个GET与X-API-Key安全定义正确。IAB再次尝试downloadMedia后未收到下载完成事件，保留该工具观察限制；不声称事件已通过，不改变正确的接口实现。
- 管理和文档控制台无新增warn/error。公开指南复制失败分支由自动化与代码审查覆盖，本次未在真实浏览器强制触发。
- 已保存 homepage-developer-link-light.jpg、developer-docs-desktop-light.jpg / dark.jpg、developer-docs-mobile-light.jpg / dark.jpg及developer-docs-320-light.jpg，位于本聊天visualizations输出目录。
- 本切片验收后恢复主题为跟随系统、退出临时账号、清空剪贴板、关闭QA标签、复原视口；8766测试进程已停止，独立数据库和加密密钥随后清理。未迁移、重启或改变原8000实例。
- 最终收口：task.py validate通过（implement 19项 / check 16项），git diff --check通过；77个任务变更路径全部匹配更新后的四组提交清单，原有pelican-bicycle.html仍排除；8766端口已无监听。未暂存、提交、推送或归档。

## 已授权提交与归档

- 用户于2026-10-30明确要求“提交 归档”，按已审阅的四组清单执行，不推送、不部署。
- 前三组产品提交完成；第四组暂存检查发现五份新规范摘录末尾多余空行（先前未暂存检查不会检查未跟踪文件）。仅删除多余EOF空行，正文和产品代码均未变，随后重新进行完整暂存检查。最终提交与归档信息在收尾日志中记录。
