# 管理员用户详情：执行与验证计划

状态：实现、独立最终检查、真实浏览器与规范同步完成；用户已批准按计划提交和收尾，两批工作提交、归档与会话记录均已完成。

## 0. 实施前门槛

- [x] 复用现有任务，读取现存代码/规范/任务上下文。
- [x] 用户确认基础统计（仅作品、章节、正文总字数）及正常/回收站分组。
- [x] 用户确认成功密码登录、注册自动登录的最近登录口径，不新增最近活跃。
- [x] 明确删除中为独立过渡状态、账号全生命周期 project_count 不能替换为正常数。
- [x] 持久化 PRD、技术设计、执行计划及研究证据，整理实现/检查上下文。
- [x] 向用户展示完整精简规划摘要，获得后续“开始”的明确批准。
- [x] 批准后执行 task.py start，记录 implementation_approved=true。
- [x] 记录 Git 实现基线并建立 codex/admin-user-details 分支；迁移 head 实施时再次核实。

## 1. 后端纵向实现（trellis-implement）

所有 dispatch prompt 以 `Active task: .trellis/tasks/10-30-admin-user-details` 开头。优先 native Codex 上下文注入，未注入时按 Trellis 指导拉取 implement.jsonl。工作者不是唯一编辑者，不得回滚他人的修改。

后端所有权：`backend/app/models/entities.py` 的 User 字段、新的 Alembic revision、`backend/app/services/auth_service.py`、`backend/app/schemas/admin.py`、`backend/app/services/admin_service.py`、`backend/app/api/v1/admin.py` 以及对应后端测试。不得修改前端、任务规划或规范。

- [x] 先加回归：nullable 历史登录、用户详情权限与精确响应白名单、三种生命周期计数。
- [x] 添加 nullable last_login_at 与原位迁移，测试升级/回退不丢失用户与子行。
- [x] 在注册专用提交边界记录自动登录；密码登录使用条件更新、单调 UTC 时间及被核验的版本快照；保存失败不返回成功 Cookie。
- [x] 新增只读详情 service/schema/route，一条 SQL 做安全账号投影及完整分组聚合，零资源正确返回。
- [x] 保留现有列表/写操作字段、认证 Cookie/JWT、审计/自身/最后管理员/删号及会话失效契约。
- [x] 定向测试和 ruff 通过后记录实现与测试报告于 task/research/。

建议定向验证（工作目录 `backend/`，每条命令独立执行）：

```bash
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_auth.py tests/test_admin.py tests/test_admin_user_details.py tests/test_migrations.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_admin_projects.py tests/test_recycle_bin.py tests/test_project_lifecycle.py
```

新测试文件名以实际实现为准；不能因文件名调整而遗漏对应覆盖。

## 2. 前端纵向实现（trellis-implement）

可在详情 API 契约按 design.md 固定后与后端分离执行；若同时工作，只分配不重叠文件所有权。

前端所有权：`frontend/src/api/types.ts`、`frontend/src/api/admin.ts`、`frontend/src/router.tsx`、`frontend/src/pages/AdminUsersPage.tsx`、新的详情页及窄共享账号动作组件/测试；如确有 owner 深链错误，再包括相关管理页的最小同步修复。不得修改后端、规范或规划。

- [x] 增加 AdminResourceUsage / AdminUserDetail 类型和 getUser API，测试路径、Cookie、原错误透传及固定 owner 分页查询。
- [x] 在 RequireAdmin 下增加懒加载详情路由、用户名/详情入口及返回列表，目标 ID/actor key 重建隔离。
- [x] 页面基本信息、正常/回收站对比与非零删除中提示；null 登录文案、零资源、错误/加载/重试。
- [x] 正常作品列表始终绑定目标 owner，分页可用，提供正常目录/回收站深链；复用归属提示和原工作区权限。
- [x] 窄提取账号操作供列表/详情复用，新建留在列表；补齐所有写动作的目标/身份/请求代次保护、防双击及本组件确认框卸载清理。
- [x] 保持列表末页删除回退及已有 self/project_count/冲突收窄规则；详情删号成功返回列表。
- [x] 加 SSR/UI 单测及账号操作实际异步控制路径的延迟/换目标/卸载回归；不以仅检查源码字符串代替行为验证，不引入 jsdom 或新测试依赖。
- [x] 明暗主题 token、375px、键盘标签及无整页横滚通过检查；报告写 task/research/。

验证（工作目录 `frontend/`，独立执行）：

```bash
npm test
npm run lint
npm run build
```

build 包含 tsc 类型检查；核对新增页面按需成 chunk，不通过提高 chunk 上限掩盖问题。

## 3. 重点回归矩阵

| 验收 | 必需证据 |
|---|---|
| AC1 / AC7 | 时钟受控的注册/登录更新；错误/禁用/GET me/改密/管理员创建/bootstrap 不更新；并发验密时禁用/改密；迟到请求不倒退；提交失败不成功发 Cookie |
| AC2 / AC3 | 两个 owner、多作品、多章、空作品、零资源、历史版本；正常/删除中/回收站/过期保留/恢复/物理删除；全生命周期计数等于三组之和；分页不影响总用量 |
| AC4 / AC8 | 列表→详情→所属作品工作区及回收站，直达/刷新、owner切换、分页、归属提示；无他人私有资源/AI输出/阅读进度请求 |
| AC5 / AC6 | 列表/详情重置/启停/角色/删号；本人/最后管理员/有作品/待收尾任务；用户名精确确认；延迟响应、切目标/切身份/离页、StrictMode、确认框清理、防双击 |
| AC7 | 401/403/404/422、安全 detail，精确响应字段集合无哈希/版本/私密字段；读请求无任务/模型副作用 |
| AC9 | 真实存量用户+项目/子行的迁移往返、旧时间 null、外键检查；现有会话与列表字段兼容 |

## 4. 集成与质量门禁（trellis-check）

- [x] 所有实现结束后派 trellis-check，使用 check.jsonl 和全范围 diff（含新增/未跟踪产品文件），不能只评审已暂存 diff。
- [x] 检查修正仅限本任务；范围外观察只报告，不顺手改全局代码。
- [x] 后端全套：工作目录 backend，分别执行 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .`、`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest`。
- [x] 前端全套：工作目录 frontend，分别执行 `npm test`、`npm run lint`、`npm run build`。
- [x] 浏览器实际验证桌面/375px、明暗主题、空/null/失败/404、直达/刷新/切目标，正常作品与回收站 owner 深链、账号确认/取消/成功路径。
- [x] 浏览器用隔离临时账号/数据，不用真实用户密码；网络检查管理他人作品不触发被禁止的私有/AI/进度请求。
- [x] 浏览器/运行环境若受限，记录准确阻碍和未验项，不以静态通过冒称完整验收。
- [x] 修复后重跑受影响门禁；汇总 research/full-scope-check.md 与 verification.md，标记实际通过/未验/阻碍，不预填成功。

## 5. 规范、提交与交付（主会话）

- [x] 对照真实实现更新 auth-admin.md：最近登录字段/入口、详情 GET/白名单/生命周期及测试，保留原会话规则。
- [x] database-guidelines.md 补充实际 migration 及无历史回填/不重建父表；前端 component-guidelines.md、type-safety.md 精简补充实际契约。
- [x] 本任务未发现需扩大范围的产品修复；并发认证/旧响应防护已固化为契约，既有SSE偶发超时保留原始证据，未改任务引擎或放宽测试。
- [x] 对比Git基线并经用户一次确认，按计划提交产品84688d7、规范/验收ee64804；四项用户原有改动保持原样，未amend或push。
- [x] 按trellis-finish-work完成当前任务归档（113ade5）与会话记录；工作提交84688d7、ee64804在先，独立记账在后，原有四项改动保持不变。

## 风险与回滚点

- 最高风险：认证时间写入与禁用/重置并发、签发版本快照；先完成定向行为测试再扩展 UI。
- 迁移只能原位修改 users；发现工具会重建父表或影响子行时，先修方案。
- 删除中不属于回收站，不允许用 total-active 伪造回收站数。
- 共享提取不能保留未受保护的确认框/异步回调，也不得扩展成无关通用框架。
- 应用回滚优先保留 nullable 列；降级前停用新应用并按需导出新增登录记录，不撤销业务用户/作品数据。
- 实现若需改变已确认范围、权限边界或统计含义，回到规划更新产物并重新获得审阅批准。

## 最终验证摘要

后端最后一次获批沙箱外标准完整pytest为1350 passed、1 warning，exit0；前端658测试、lint、两份TS和等价构建通过。先前失败/环境限制、原始日志、24个产品文件hash及浏览器细节以verification.md和research报告为准，不隐藏历史失败。用户已明确批准两批工作提交和收尾；产品提交为84688d7、文档提交ee64804；归档（113ade5）及会话记录均已完成。
