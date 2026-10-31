# 任务监控查询、隐私与交互现状

日期：2026-10-31。阶段：Phase 1.2，仅研究，不代表实施批准。

## 1. 事实来源与边界

- `backend/app/models/entities.py:588`：Task 继承带 `created_at/updated_at` 的 TimestampMixin；持有 `statistics_key`、用户/作品/章节 ID、类型、状态、输入、输出、阶段、错误、断点及开始/结束时间。
- `backend/app/services/task_service.py:256`：普通 `get_owned_task` 同时限定 user_id 与作品生命周期；他人/不可见任务统一 404。管理员监控必须新增独立服务/路由，不能放宽它。
- `backend/app/services/task_service.py:89`：普通 TaskOut 包含断点、Skill 等非监控必需字段，且活动任务会从匹配 UUID 的 Hub 取进度/阶段。管理员不能直接复用普通 Out 或 SSE 输出作为传输契约。
- `backend/app/services/task_service.py:292`：普通列表仅按用户读取、limit 截断，非全站分页服务。
- `backend/app/services/task_service.py:458`：常规结束任务超过每用户 200 条会被裁剪；回收站历史不参与普通裁剪。用户还可单删/清理，作品彻删会级联移除 Task。
- `backend/app/models/dashboard.py::TaskStatistic`：只有 UUID、类型、来源、状态与事件时间，没有用户、作品、正文及原始错误。它用于独立累计，不是可恢复的任务明细。

用户已确认：所有管理员可人工取消任意未结束任务（确认、原因、审计）；只查现存明细；只显示安全错误类别与固定说明；5 秒可关闭/隐藏暂停刷新；不新增阈值告警或自动取消。

## 2. 不显眼的隐私入口

- `backend/app/ai/providers/base.py:91` 的 `describe_error` 截断 SDK 异常原文，不能据此认定 Task.error 已适合跨用户展示。
- `backend/app/services/task_engine/engine.py:295` / `:351` 把 AppError.detail、runner error.detail 和 stage 事件写入任务/Hub。
- **原始 Task.title 也不能一律视为安全元数据**：`backend/app/services/task_engine/handlers/agent.py:58` 用 `req.content[:20]` 生成智能体任务标题，包含用户指令片段。
- `backend/app/services/agent/runner.py:96` 会把私有 MCP 插件的 external.label 写入阶段；`:190` 也包含工具名称。
- `backend/app/services/book_import/reverse.py:718`、`backend/app/services/batch_service.py:410` 的阶段具有数字进度和固定模板；不能把所有原始 stage 无差别放行。

建议管理员 DTO 使用固定任务类型标签/任务编号代替原始任务标题；阶段只保留有限内部固定文案或严格的数字模板，未知阶段回退到状态标签。搜索同样不能搜索原始 Task.title/input/error 形成信息侧漏。允许查询的自由文本仅匹配已获管理权限的用户名、作品标题，按现有 SQL 字面量转义规则处理。

安全错误摘要为有限 `error_code` 与固定 `error_message`：可靠的应用内固定错误可分类，其他全部回退到执行失败。不对任意上游文本猜测鉴权/额度/限流，也不输出原文、正则脱敏片段或完整调用地址。

## 3. 排队时间不能直接用 created_at 或 updated_at

- `task_service.py:193` 的 resume 把同一条 paused 任务改回 queued，不重建任务、不重置 created_at，故“当前排队时长”不是距创建时间。
- TimestampMixin.updated_at 会受其他 Task UPDATE 影响；统计 UUID 初始化、未来取消请求标记也可能更新它，不能成为稳定的入队计时契约。
- `task_engine/engine.py:268` 每次领取都会写 started_at，适合当前 running 一轮的开始时间，不等于含审核等待的累计运行时间。
- 通用创建在 `task_service.py:154`；另有直接创建 Task 的 `analysis_service.py:568/:612`、`kb_index_service.py:129`。新增 ORM 的可空 `queued_at` 默认值可覆盖这些新建入口；resume 必须显式刷新它。

建议新增 Task.queued_at（UTC、可空、ORM 新建默认当前时间），仅在排队中计算本轮 queued_seconds，仅在运行中计算本轮 running_seconds。老记录不能可靠重建时留 null，界面显示“未记录”，不能补零或用错误时间冒充。时长排序按状态优先、有效时间升序及稳定 ID 兜底；未知时间置后。

## 4. 队列位置不是开始承诺

`task_service.py:119` 以更小 ID 的 queued 数量计算位置。`task_engine/engine.py:201` 还受用户/全局并发、serial_key 和作品生命周期约束，不保证所有 queued 严格逐条开始。

如返回队列位置，必须明确是前方排队记录数，不展示“预计第 N 个开始”或等待时间估计。取消请求等待收尾时要另外显示取消中，不能将其当成可领取的普通排队任务。

## 5. 查询口径建议

- 独立 `/api/v1/admin/tasks`，每个端点使用当前 AdminUser；对仍存在的 Task 做投影，包含回收站/删除协调中的作品任务，但不给出绕过作品权限的创作操作。
- 过滤：用户 ID、作品 ID、任务 ID、任务类型、多个任务状态、用户名/作品标题字面量搜索、带时区的创建时间半开区间；分页默认 20、最多 100。
- 列表、total、状态计数共用同一条件；计数覆盖过滤后的所有页，不冒充历史累计。单页请求返回统一 as_of；轮询反映真实变化，不承诺跨多个 HTTP 请求的冻结快照。
- 稳定排序：创建倒序 / 排队最久优先 / 运行最久优先，ID 兜底。
- 管理端暴露专用 task_key（来自 statistics_key），详情/取消按 UUID 定位，整数任务 ID 仅用于展示/筛选。原 UUID 消失返回 404，不把旧目标重定向为同 ID 新任务。
- 详情仍仅安全元数据，不包含 input/result/checkpoint/output_text/原始 error、原始任务标题或私有插件名。

## 6. 可复用前端模式

- `frontend/src/api/client.ts::request` 已支持 Cookie、AbortSignal、数组 query、统一 401 和中文错误；无需新 fetch 包装。
- `frontend/src/api/tasks.ts` 已提供 TASK_STATUS/TASK_KIND_LABELS；只复用标签，不复用 taskApi、TaskPanel、taskLink 或输出组件，以免跨用户请求普通任务/私有资源。
- `frontend/src/components/dashboard/dashboardController.ts:25` 是页级请求所有权与失效模式；同文件的 startDashboardPolling 支持 visibility 与可选 interval。可复用 5_000 间隔，不能顺手改变首页 60 秒默认值，也不必抽象成全局通用数据框架。
- `frontend/src/utils/auditLogs.ts` 有本地日期转 UTC 半开区间、目标请求 gate、分页回退等真实模式；适当复用，不为少量页面专属逻辑修改大范围契约。
- `frontend/src/router.tsx:59` 起的管理员页面在 RequireAdmin 下懒加载；`AppLayout.tsx:40`、`:46`、`:100` 分别维护面包屑、账户菜单、桌面/移动共用侧栏。
- 监控页用已有 Ant Design/StudioPage，桌面紧凑表格、375px 卡片/可折叠筛选，不引入营销式 hero 或新的图表依赖。
- 前端状态属于页面实例，不能塞进个人 useTasksStore；按 actor ID key 重建，401/403 清空并停轮询。筛选/排序/页码变化使旧响应失效，慢请求不得被每次定时刷新无限中止。

## 7. 首页接入

首页累计失败/中断（含已移除任务）与监控页现存明细不同。新增入口标明“查看现存任务/失败任务”，不能把创建时间过滤硬接成累计终态 24h 明细，也不能让链接暗示统计条数与现存列表必然一致。

## 8. 验证关注点

- 双用户与当前管理员权限、降权、已删除 UUID 与复用整数 ID、回收站任务、分页稳定性、组合条件和字面量搜索。
- 任务原始标题含指令、stage 含私有插件名、error 含密钥/URL/正文时，管理员 API 与 UI 均不外露；未知类型/阶段/错误使用固定回退。
- 新建、paused→queued、重复暂停/继续的本轮时间；旧数据 null；不把 paused 计为占用执行并发。
- 列表失败保留旧数据但标过期；筛选/账号/降权/卸载/关闭详情使旧响应失效；5 秒开关、隐藏/恢复、慢请求及重复取消。
- 桌面/375px、浅/暗色、键盘操作、确认取消和失败反馈的真实浏览器验证；静态渲染只补结构，不能替代交互验收。
