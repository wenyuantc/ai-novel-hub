# 规范阅读与新旧契约边界

调研日期：2026-11-01。此导航保留实施前的新旧契约比较，2026-11-02已完成实际规范同步；当前执行契约以新rbac-quotas.md及已更新领域规范为准，验收见../verification.md。

## 优先关系

本任务已确认需求见 `prd.md`，已批准的技术方案见 `design.md`，执行/验收见 `implement.md`。以下规范记录的是当前已交付系统：只有二元授权、旧布尔角色HTTP字段、尚无用户配额等部分会被本任务有意替换；不得将这些旧事实误当作禁止实现新RBAC的约束。Cookie/token_version、登录认证快照、真实actor/owner、owner-only私有能力、审计原子性、生命周期、取消/UUID/轮次、系统设置持久发布等不变量不因角色替换失效。

实现者按自身backend/frontend所有权加载对应规范全文；检查者按修改面加载。不要仅阅读index或本导航就写代码。研究文件中的代码锚点是调研时快照，实施时核验实际行号。

| 规范 | 必须保留/对照的契约 |
| --- | --- |
| `.trellis/spec/backend/index.md` | FastAPI/service/ORM分层、规范发现与质量命令 |
| `.trellis/spec/backend/database-guidelines.md` | 无ORM relationship、显式SQL、事务/聚合/迁移、FK与SQLite父表保护 |
| `.trellis/spec/backend/error-handling.md` | 安全AppError、422/500、SSE/任务错误，不泄露敏感输入 |
| `.trellis/spec/backend/quality-guidelines.md` | ruff/pytest、隔离DB、FakeLLM、安全认证与SSRF边界 |
| `.trellis/spec/backend/auth-admin.md` | token版本/禁用/重置、bootstrap、最近登录认证快照、用户安全投影和详情原统计 |
| `.trellis/spec/backend/admin-projects.md` | 人工内容例外清单、严格owner、私有配置与模型副作用/阅读进度隔离 |
| `.trellis/spec/backend/admin-audit.md` | 显式AuditOperation、真实actor、安全有界摘要、成功同事务、提交不确定、清理nonce/高水位 |
| `.trellis/spec/backend/admin-task-monitoring.md` | 原会话版本与User锁、取消持久受理/真实排空、UUID/轮次、CAS与提交未知 |
| `.trellis/spec/backend/system-settings.md` | 六项白名单/默认来源、revision、相同Settings引用、shield与所有发布/恢复路径 |
| `.trellis/spec/backend/recycle-bin.md` | 两阶段删除、生命周期父行屏障、30天代次/系统expire、恢复不复制及User RESTRICT |
| `.trellis/spec/backend/project-transfer.md` | 导入整体事务、预览token、注册表反射、私有数据不随作品迁移 |
| `.trellis/spec/backend/book-import.md` | 解析revision/start整体事务、任务checkpoint、quota拒绝不删解析记录 |
| `.trellis/spec/backend/inspiration.md` | 灵感转作品/应用已有作品的差异和状态、无隐式模型副作用 |
| `.trellis/spec/backend/skills-mcp.md` | 全局/个人Skill可见和动作、MCP与模型仍严格owner，不能因任意管理权扩张 |
| `.trellis/spec/backend/workshop.md` | 审核/发布/作者匿名/隐藏/上下架、普通投稿和管理动作区分 |
| `.trellis/spec/frontend/index.md` | 前端布局/主题/类型/状态各规范入口 |
| `.trellis/spec/frontend/component-guidelines.md` | 具体表单/Controller复用、权限相关工作区、主题token/375px、局部确认框拥有权 |
| `.trellis/spec/frontend/state-management.md` | auth/private状态、真实owner、异步epoch/gate、切账号与失权清理 |
| `.trellis/spec/frontend/type-safety.md` | schema镜像、Cookie/ApiError/AbortSignal、401和业务403区分、未知数据收窄 |
| `.trellis/spec/frontend/quality-guidelines.md` | Node Vitest、lint/build/test、懒加载/包体积、无新增测试框架 |
| `.trellis/spec/guides/cross-layer-thinking-guide.md` | 统一边界类型/事实源、资源身份/轮次、提交已成功但确认丢失 |
| `.trellis/spec/guides/code-reuse-thinking-guide.md` | 改值前搜索、共享解码/目录、复用现有controller，不建无关通用CRUD框架 |

## 调研到实现的交接

- 后端权限完整调用方、授权表示与锁序：`backend-rbac-contracts.md`。
- 四处作品创建、调度真实占用与发布窗口：`backend-quota-contracts.md`。
- 前端全部布尔触点、人工只读范围、跨域读取依赖、权限刷新与浏览器矩阵：`frontend-rbac-quota-contracts.md`。
- 上述研究是证据与备选建议，发生候选名称/存储/锁方案差异时以最终批准的design为准，不把未采纳建议各自实现成第二套机制。

## 当前验证状态

此句仅描述2026-11-01调研时刻：当时只完成静态代码/规范调研和规划。现已完成产品测试、隔离迁移和浏览器验收，PG仍按用户要求暂缓，见../verification.md。SQLite User ID/旧JWT复用为既有风险，见后端研究与design风险节；新授权revision不能被宣传为JWT实体隔离修复。
