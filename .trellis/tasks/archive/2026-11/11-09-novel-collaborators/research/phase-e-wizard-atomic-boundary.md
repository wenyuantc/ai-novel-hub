# 向导替换的事务与作品边界

- 范围：向导替换、续写、作者任务/模型身份、输出与收据；代码快照为 2026-11-10 当前工作区。
- 入口：`backend/app/services/wizard_service.py`、`backend/app/services/task_engine/handlers/wizard.py`。
- 协作受理、执行者与模型选择、物理发送、共享检索和保存确认尚未接通。本记录不代表编辑者向导已开放。

## 实施与证据

1. 角色、世界、职业、大纲各自的替换可能先删除旧数据，随后创建、关联或构建输出失败。原实测在调用方捕获异常并提交时，角色旧记录被删除且新记录保留。现在入口先拒绝会在 SAVEPOINT 开始时自动 flush 的待写对象，锁定可见性与活动项目后，在嵌套事务内完成预检、无引用句柄清理、删除、新建、关联及输出投影。任何异常回滚整个保存点；外层再次提交不会保留半次替换。`TransactionSession` 的内部 commit 仍只有 flush，由真正的外层事务决定提交。
2. 初始真实反例发现其他作品中的组织成员、规划世界关联和角色职业关联可被向导删除目标时跨书级联删除。新增预检覆盖直接及级联 FK，角色关系双方、职业阶段、规划、章节、大纲、目标时间线及其主体、字段/事件、规划关系和句柄。`SettingTextField.base_value` 与 `SettingProgression.value` 的 JSON 句柄依赖单独检查，包含已停用历史；跨时间线的历史 supersession 和字段引用也被检查。异常只返回不含他书细节的冲突。ORM 元数据的受审查 FK 图新增或变化时失败关闭；句柄会被显式清理，因此也纳入图。
3. 大纲追加不删除旧大纲，不因旧大纲的异常跨书关联拒绝追加。大纲覆盖维持既有任务受理、保存阶段冲突事件与错误消息；其他类型按原有时机在受理和保存前检查。
4. 编辑者和查看者接受真实邀请，即使允许作者模型、编辑者有本人配置并传入伪造内部键，也不能创建四类向导任务；SQLite HTTP 专项核对了无 Task、无假文本或向量模型调用。假模型不能证明真实网络发送顺序。
5. 续写绑定客户端实际看到的来源 Task UUID，并固定来源输出、输入指纹及原 HTTP token 代次；缺证、来源被替换、不同本人/作品/类型、原 token 变化及重读均失败关闭。响应新任务仍产生新的 Task UUID。前端已按 TaskOut 原 ID/UUID 成对提交。
6. 文本模型使用受理时固定的服务端模型绑定，执行体由引擎领取事务冻结 actor/project/kind、原 UUID、入队和执行代次、输入、checkpoint 及原 token；真实发送在可信 `SendGuard` 的物理 lease 内重验模型、执行体和续写来源。发送后的事件交付、成功终态与最终写回都由短事务分别重验。模型配置删除复用、修改或切换默认配置不能让旧任务换用新模型。
7. 文本/向量可能出站前持久 `_wizard_pending`；结果、流哈希与 `_wizard_completion` 在内容替换事务中一起提交。提交确认丢失时只凭原执行体和 receipt 在短事务重验已提交结果；无法确认时保留凭据并阻断盲重试。成功后的常规裁剪/删除仅在成功终态的 task/result/input/流哈希及模型绑定证明齐全时放行；内部 checkpoint 不对用户投影。
8. 替换已有角色/世界/职业，或新增可追踪候选造成词典竞争，持发送写门禁在同一保存点提升可见性 revision/history epoch；非竞争新增不提升。句柄清理已经收紧时不会重复提升。

## 测试状态

- 原始红灯：角色/世界/职业跨书级联 3 failed（大纲已有保护 1 passed）；时间线主体、跨书 JSON 基础值、停用历史、跨时间线 supersession、追加模式共 5 failed。
- 最新向导业务、续写、可见性、原子性、跨作品、协作者关闭与规划专项：87 passed（SQLite，包含此前所列原有测试及 `test_wizard_visibility_fencing.py`）。
- 本切片 Python 文件与测试 `uv run ruff check`：通过。
- `tests/test_wizard_resume_identity_postgresql.py` 2 项与 `tests/test_wizard_atomicity_postgresql.py` 2 项已收集；最新代码等待主会话错峰在本机独立 PostgreSQL 实跑。原子性专项使用两个数据库连接验证项目行锁、中间状态不可见和回滚后原记录；JSON 专项走真实 HTTP 受理与 PostgreSQL 查询。它们都不验证真实外部模型发送。
- 执行身份与模型专项最初真实 HTTP 红灯包括模型流中复用 Task ID、更换 execution key/token、缺原 token 的续写、排队中模型切换、发送前身份/模型变化、保存 ACK 丢失后盲重试；最终 `tests/test_wizard_execution_identity.py` 19 passed（SQLite），含完成 receipt 的验证与损坏收据拒绝清理。
- 另补真实 HTTP 作者取消未提交运行任务：原记录保持，原 pending 被清理，定向 1 passed；与上条 19 项为错峰两次运行。
- 候选替换/竞争、无竞争、同事务不重复 bump，以及真实 OpenAI SDK + 本机 TCP 的旧快照零发送和响应头前写屏障，`tests/test_wizard_visibility_fencing.py` 阶段性 8 项通过；这些为 SQLite，不等同 PostgreSQL 竞争验证。
- 主会话隔离 PostgreSQL 实跑 `tests/test_wizard_dispatch_postgresql.py`、`tests/test_wizard_atomicity_postgresql.py`、`tests/test_wizard_resume_identity_postgresql.py`：8 passed in 30.00s。实际 OpenAI SDK + 本机 TCP、双连接涵盖 Task 替换零发送、token 写阻塞至响应头及后续保存失败、提交确认丢失两种事实、原子性与续写来源；运行中本代理追加未知 kind receipt 失败关闭，该结果只证明测试启动时的代码，最终集成须重跑。

## 仍需闭环

- `task_service.create_task` 目前坚持严格作者归属；中央组明确尚无面向向导的稳定协作者受理/运行接口，不能改作作者身份或开放入口。
- 当前任务执行身份及文本模型已接入统一领取快照与物理发送 guard；真实 editor 的 task 受理与 model/grant 绑定、共享向量逐跳防护尚未接入，不能据此开放。
- 服务端任务输入须同事务固定原邀请 UUID/revision、实际 actor、本人或获授权作者的文本配置及共享向量选择；旧续写只能继承可证实的原授权，不得用当前邀请补签。实际调用采用可信 provider/物理 lease，并保证响应头前撤权的两种顺序。
- 旧作者知识库仍使用原有 `provider:model` 向量空间以保持召回兼容；协作者不能把旧向量重标为 `shared:v1` 或采用自己的不同空间，必须配合中央共享向量选择、缓存 preflight 和经授权安全重建，链路完成前 editor 保持拒绝。作者原向量调用的完整执行身份/模型租约仍待共享检索协议接入。
- 替换预检持本作品锁并拒绝当前存在的异常跨书引用；若其他事务绕过应用层直接插入异常跨书 FK，数据库部分外键尚非复合项目约束，预检无法单独证明与其并发安全。ORM 元数据图不等同物理数据库约束检查；开放前应核实所有生产写入口及必要的数据库约束和 PostgreSQL 双连接竞争。
- 用户同时要求可用的编辑者完整链：还需真实 PostgreSQL 加实际 HTTP/provider 覆盖 editor、撤权/模型授权先后、受理身份、共享向量、自动派生及结果/输出隐私，再考虑开启。现有作者专项结果不能替代这些验证。
