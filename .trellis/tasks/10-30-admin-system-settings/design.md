# 系统设置技术设计

日期：2026-11-01。用户已授权规划完成后直接实施；产品边界以PRD为准。

## 1. 边界与选型

沿用FastAPI/service/SQLAlchemy、注入Settings、AuditOperation和AntD，无新依赖。新增实例级设置存储、窄运行时管理器、管理GET/PATCH、六项表单与公开配置同步。不改.env，不做通用配置平台或多实例广播。

请求、TaskContext和运行中智能体持有同一Settings对象。不能只替换app.state.settings或清空get_settings缓存；保持共享对象身份，只在确认提交后同步发布已验证的六字段。部署基线另存六字段快照，不能被覆盖操作污染。

## 2. 数据与来源

新增 `models/system_settings.py::SystemSettings`，表system_settings：固定id=1（CHECK）、revision正整数、overrides稀疏JSON、last_operation_id（UUID文本）、UTC updated_at。无业务FK，不进小说备份；它不属于项目相关表，不加入project_transfer.NOT_EXPORTED。

新迁移0027_system_settings，父0026；仅创建新增表。无行表示revision=0、无覆盖，不需要GET/迁移创建初始行。overrides只含六项有效bool/int，恢复通过删除key而非保存null。

来源枚举admin/deployment/default。管理器构建时从原始Settings.model_fields_set捕获显式部署/注入字段；代码默认从Settings字段元数据取得。deployment不声称能区分环境/.env/显式应用注入。有效值按是否存在覆盖判断，不能以truthiness忽略false。

## 3. 管理接口

新schemas/system_settings.py、services/system_settings_service.py、api/v1/admin_system_settings.py并注册；路由无ORM。前缀 `/api/v1/admin/system-settings`，AdminUser，Cache-Control:no-store。

GET返回 `{revision, updated_at, items}`，items恰好六项：key、value、source、deployment_value、deployment_source、default_value、overridden、requires_restart=false。bool-key和int-key用区分式类型，值/默认/部署类型一致；不返回其他Settings字段。GET不创建业务行或审计。

PATCH请求 `{revision:int>=0, changes:{六项可选字段}}`，每项bool/int/null；两层extra=forbid，changes非空。省略保持、false明确关闭、null取消覆盖。strict布尔及正整数，拒绝bool充当int、浮点或数字字符串；不新增单用户≤全局限制。开放API限额上限为数据库Integer计数真实边界2_147_483_647，管理输入/持久加载/部署基线使用一致校验，超大值拒绝而非截断，避免后续认证绑定溢出。revision过期409，结构/类型422，鉴权401/403，成功返回同一GET投影。公开auth/config保持原两字段，仅加no-store。

## 4. 原子写入与运行时

- 管理器挂app.state，持有应用写锁、部署基线及已确认revision/覆盖快照，通过窄依赖使用。
- 锁内重读持久行/当前管理员，核对revision，合并并验证候选，构建安全投影。首次唯一主键INSERT；后续revision条件UPDATE，竞争409。
- 同事务写设置和AuditOperation成功记录；确认commit后，在**无await**同步方法中发布共享Settings六字段及管理器快照，再TaskEngine.notify，最后返回成功。
- 普通失败回滚不发布。提交确认丢失须按revision/last_operation_id重读持久事实，协调发布已提交快照，不能相反审计或盲重发。请求取消不能拆断持久提交与发布；若使用shield/独立任务，必须服务自有会话、保留任务引用并由lifespan关停排空，不能借用已退出请求会话。
- 无法确认则不返回成功、不回退默认、不继续新的管理写；保留可追踪恢复工作，后续管理读取/恢复先核实持久状态。数据库不可用不承诺离线一致性。
- 启动先加载覆盖再引导业务/恢复任务/启调度；坏持久值/读库失败不能静默忽略。无行正常沿用部署。测试fixture不走lifespan，显式初始化同一管理器后再启动任务引擎。
- 不序列化完整Settings，不修改JWT/cipher/非白名单字段；单事件循环不会观察到一次同步发布的半套字段。

审计新增system_settings.update / target system_settings、固定目标「系统设置」。用局部、类型化、最多六项的设置摘要，记录key、覆盖前后、有效前后及来源（bool/int/null/有限枚举）；其他动作不得携带该摘要，不开放任意JSON/文本。失败不保留未提交after；同步TS、标签及审计详情。

## 5. 消费方与竞态

| 消费方 | 生效边界 |
| --- | --- |
| 注册/公开配置 | 现有SettingsDep读取共享对象；不影响登录/管理员建号/bootstrap |
| 任务调度 | 最新两级上限+notify；降低只阻止新调度，不取消运行任务；保持串行FIFO/资源键/取消协调 |
| 自动分析 | enqueue_auto及batch后续分析阶段读取最新全局/项目开关；不取消已排队/运行或手动分析 |
| MCP | 路由require_mcp之外，连接前、连接建立后实际工具调用前、智能体Worker出队后再次检查共享开关；不可捕获一次性bool |
| 开放API | authenticate_key每请求接收最新阈值，保留rate_count/rate_window，429和Retry-After不变 |

MCP已进入call_tool的调用不主动cancel；待执行调用关闭后拒绝。保留原连接回收、SSRF、超时、脱敏和external提案语义，不把拒绝记成成功。

## 6. 前端

- `/admin/system-settings` RequireAdmin懒路由；AppLayout桌面/移动/账户/面包屑加入口，保持Studio/AntD后台工作界面，不新增UI框架。
- 六项分组表单，显示当前/来源/部署/默认；编辑先暂存，保存只提交有变化字段。逐项恢复确认后标「待恢复」，保存才生效；不增加整站重置。
- busy禁冲突操作，dirty时刷新不覆盖草稿；409提示刷新核对而非自动覆盖；结果未知先读状态。账号/权限/卸载使旧请求失效，401/403撤下管理数据。
- api/types.ts镜像区分式DTO，api/systemSettings.ts用现有request/AbortSignal；安全审计标签仍唯一来自api/auditLogs.ts。
- auth store提供独立公开配置刷新/应用动作，不重新init/me，不改变登录态。App安装单一轮询：可见30秒、visibility恢复立即刷新，focus去重，隐藏停止；慢请求不重叠，失败保留最近成功值。
- 保存成功立即应用本页公开两字段并使更早配置请求失效，旧响应不能倒退新值。初始加载保留现有兼容语义。
- 现有startDashboardPolling已被dashboard/adminTasks复用；可抽出极小公共可见轮询工具，保留旧导出包装及60/5秒行为，避免App静态引入后台chunk。不做无关controller重构。
- 375px、浅暗主题、键盘/标签/焦点；不使用新的用户级全局store存管理表单。

## 7. 兼容与回退

无覆盖部署行为不变，get_settings缓存与非白名单部署字段不变。先迁移再启动，重启测试用新Settings/新应用而非原对象。回退优先回退代码保留表；旧代码将恢复纯部署行为，提前提示。真正downgrade先停服务/保留覆盖快照，明确删除后台覆盖，不能损伤用户/作品/审计。沿用现有单进程前提，不新增隐蔽多worker承诺。
