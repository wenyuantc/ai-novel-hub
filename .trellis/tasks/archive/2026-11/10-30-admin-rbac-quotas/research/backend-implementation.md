# 后端实施记录

日期：2026-11-02；实施者：`rbac_backend_implement`。

## 实际接口

固定契约见同目录 `backend-api-contract.md`。前端字段没有在实施中改名。新增两组路由为 `/api/v1/admin/roles`、`/api/v1/admin/users/{id}/roles|quotas`；目录仍为固定33项。

## 实际实现

- `User.is_admin` 是唯一超管事实。四个内置Role元数据持久化，super/ordinary成员投影，不保留第二套super成员权威。UserRole仅存内容管理员/审核员/自定义真实分配；多角色union。
- 目录、严格schema、角色复制/整体编辑/分配、内置只读、在用删除冲突、Role ID不复用、账号/全局revision CAS及管理目标保护均已接入真实HTTP服务。旧管理员创建/PATCH的 `is_admin` 字段拒绝422。
- `AuthorizationRuntime` 为同一sessionmaker/manager/TaskEngine共享的可重入短临界段；授权单例实际UPDATE作为DB gate，User实际UPDATE按ID升序，管理动作在业务写前重读原认证token版本/启用/权限/目标状态。
- 授权投影支持可组合批量单SELECT。用户列表/详情与角色目录不再逐行补新revision，也不把旧账号/Role元数据拼接新授权状态。用户详情保持安全单SELECT，不读取password_hash/token_version。
- 人工作品/子资源按projects.read/edit/recycle拆分；回收恢复/清理、任务取消、审计清理、系统设置、工坊每动作、全局Skill每动作独立授权。严格owner-only边界未改成管理权限。
- 原有已采集动作按management account决定审计，不仅检查is_admin。新增role/assignment/quota及窄内部bootstrap审计；其他未采集人工子模块不扩成全量审计。
- 四个Project真实创建入口在owner User屏障下检查全生命周期现存数，锁持到原事务整体提交；quota失败保留导入preview token，拆书按User→BookImport顺序并重验revision。
- TaskEngine把预候选和真实admitted分开，claim在共享runtime/DB gate/User屏障下重读最新用户配额与全局硬上限。0保持queued，降低不取消已运行；DB终态但on_success未退出仍占位。
- runtime持到claim commit返回及admitted同步发布。提交确认故障按原UUID/queued_at重读，不盲重放或启动事实未确认的外部执行。
- 所有Settings load/reload/正常修改/即时未知提交恢复/后台恢复发布共享runtime。即时未知提交重读也不先释放runtime；若事实读取不可用则暂停新准入，成功发布后恢复notify，dispatch入口避免拒领热循环。
- 领取的活驱动CAS/commit窗口只登记取消/关停意图，不直接cancel游标；确认后仍由原执行体安全finalize。UUID/轮次、取消持久意图、drain和owner-only任务访问保留。
- `0029`只新增RBAC/额度四表和目录种子，不改User父表。新语义数据/审计存在时拒绝有损downgrade。

## 新增回归与已修正的竞态

`test_rbac_permissions.py` 对全部33项逐项发送真实HTTP：最小授权放行，去掉本动作只保留依赖读权拒绝。工坊非作者visibility沿原404隐私边界，不把404改成信息更丰富的403。

`test_rbac.py`/`test_user_quotas.py`覆盖角色union/撤权不登出/复制super不变super/管理目标保护/严格模式与有界数值、作品生命周期与四入口、超管配额、任务inherit与显式覆盖及全局硬顶、新审计故障整体回滚。

`test_rbac_concurrency.py`使用Event/Gate和独立session，覆盖：

1. Role先撤权→已通过入口的后到管理写拒绝；管理写先获gate→Role修改等待并因旧全局revision拒绝，fresh重试成功。
2. Role定义把原普通目标变成管理账号→等候状态写在业务前识别并拒绝。
3. Role edit/edit、delete/assign CAS，DB RESTRICT防御；User删号ID复用后旧quota revision请求不能命中新账号。
4. 普通/灵感两个创建入口争最后名额，仅一个成功。
5. 两个预候选先登记、后降额；真实claim重验最新值。
6. DB running已提交但commit await尚未返回时，第二领取仍被runtime阻挡，不观察未发布admitted。
7. DB succeeded但成功回调未结束仍占真实执行名额。
8. claim commit“未提交失败”和“已提交确认丢失”均只执行一次。
9. Settings已提交但确认与事实读取均失败时，暂停领取；load确认发布后恢复且只执行一次。
10. Settings load不绕过共享runtime发布；owner取消恰落claim commit-return窗口时不cancel活驱动、无LLM副作用且最终安全cancelled。

旧生命周期FK末层故障注入测试原只省略一次User UPDATE。新增全局gate和升序User屏障已会阻挡该人为竞态，因此测试显式模拟省略全部授权/User UPDATE，继续保留原409/FK/作品不级联删除断言；相邻真实屏障串行测试仍保留真实DB锁。

## 实施阶段检查（已交接独立checker，以下不是最终门禁）

所有uv命令使用：`env UV_CACHE_DIR=/private/tmp/ainovel-uv-cache LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run --no-sync ...`。每条检查独立执行。

| 检查 | 真实结果 |
| --- | --- |
| `ruff check .` | 通过，All checks passed（2026-11-02） |
| 新增五组：`test_rbac.py test_rbac_permissions.py test_rbac_concurrency.py test_rbac_migrations.py test_user_quotas.py` | **45 passed in 59.11s**；日志 `/private/tmp/admin-rbac-focused-final.log` |
| 任务/取消/设置：`test_rbac_concurrency.py test_tasks.py test_admin_task_cancellation.py test_admin_task_adversarial.py test_system_settings.py test_system_settings_runtime.py` | **120 passed in 96.22s**（新增最后两个role竞态/最后一条quota默认继承回归之后另由45项覆盖）；日志 `/private/tmp/admin-rbac-task-final.log` |
| 管理用户/详情/既有审计 | 先77过1失败（不存在目标在审计前抛404），修为存在User升序锁/目标404留在审计上下文；失败项后续定向通过，最终全量复验 |
| 作品人工/回收/工坊/Skill | 较早版本62 passed；最终全量复验 |
| `alembic heads` | 单head `0029` |
| 最终完整 `pytest -q --tb=short` | **历史快照已停止**；session 56890被checker后续修正取代，日志 `/private/tmp/admin-rbac-stable-pytest.log` 不计最终门禁 |

沙箱内较早全量运行包含了中途修改，不能作为最终门禁。它暴露的4项MCP测试失败在 `tests/mcp_fakes.py` 的 `sock.bind((127.0.0.1,0))`：PermissionError / Operation not permitted，已单项复现，并改以正式 `require_escalated` 跑最终全量，不放宽断言或跳过测试。

## 验证边界

- **PostgreSQL未实测**：用户在2026-11-01明确决定“先继续，暂不做PostgreSQL验证”。未启动Docker、未连接开发.env数据库。SQLite独立session/Event测试不冒称已验证PG行锁。
- 现有单进程TaskEngine部署边界不变；本任务不声称支持多实例全局并发租约。
- SQLite旧JWT sub/ver与User ID复用是原有会话身份边界，本次仅以全局revision防止新增授权/额度陈旧写命中新账号，没有扩大成JWT身份迁移。
- 本代理未提交/push/归档/改任务状态，未修改前端/规范或原有四项用户修改。

最后锁序修正：最后启用super检查改为共同gate下exists，不在actor/target之后无序锁全部super；helper显式校验session已有gate。新增互相降级竞争与原最后/多超管HTTP流程3项通过（7.80s），完整回归再次覆盖。

额外检查：4项原受沙箱loopback限制的MCP集成测试正式escalated复跑，4 passed in 5.95s。临时SQLite中0028→0029存量迁移实证通过：启用super/禁用super/ordinary及各自Project/Chapter/私人AI密文全字段保留、FK无错；详见backend-migration-stock-proof.json。

冻结说明：最终稳定全量运行前的65项backend改动/新增文件hash记录于backend-frozen-snapshot.json。主会话派checker接管后，后端实施者停止产品文件编辑；checker若修改由主会话协调重跑，不将旧快照结果冒称为修改后的最终门禁。

## 独立checker交接（最终状态）

主会话已派独立trellis-check接管全部产品代码与最终门禁。checker发现领取commit/admitted完成后，原实现会在session.__aexit__的await前清除_claiming；owner取消仍可能在尚未进入_run.finalize时中断执行体并留下孤立running。checker负责将保护延长到会话退出及无await交接_run，并补回归；本实施者不与其并发修改。此前取消窗口定向通过不能证明此更窄窗口已闭合。

session56890及backend-frozen-snapshot.json仅代表实施阶段历史快照，不能作为checker修改后的最终验证。最终定向/全量测试、后续发现与残余风险以checker及主会话最终报告为准。PG仍按用户要求未实测。
