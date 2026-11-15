# E4 作者单章逐次占用接线

## 本切片

作者 `chapter.generate/polish/revise` 的校验先按严格作者检查定位作品，再按认证/User → 可见性写门禁 → project 顺序取屏障。参数/模型/场景校验不再先写 `generating`；`task_service` 在 Task UUID 和 queued_at 已 flush 后，同事务调用章节 handler 的 `bind_queued_claim`，新建随机 claim 并同时写 Task 和 Chapter。抢占失败回滚整个任务受理，禁止信任客户端提供的保留字段。

引擎持共同屏障，在领取前 `capture_queued_claim`，同事务 queued→running CAS 后激活该原身份。运行体、保存、系统清理均收到原不可变 running claim；恢复时只有旧执行体已停止并且持久 pointer、queued/execution、actor 和 Task UUID 全部完整，才可调用 `capture_running_claim`。旧无 claim 的历史记录不可用后补统计 UUID 冒认。

人工 PATCH 状态先持认证和可见性写门禁，再精确清空章节原 claim；旧执行体不能借新的占用保存正文、保存提案或回退状态。单章正文保存和版本/日志/原 claim 释放在同一事务；失败清理不写正文，且原生 asyncio 多次取消不能打断 SQLite 驱动游标和日志收尾。管理员取消已持久化时，失败日志按原任务与执行轮次记为 cancelled。

普通正文同事务写 Task.checkpoint 私有 `_chapter_claim` 完成收据，绑定原 task/actor/project/chapter、queued_at、execution/claim UUID、原状态、章节版本 ID 和内容版本。只允许从原运行身份重新确认同一持久收据、对应的成功日志与章节版本。提交确认丢失但查证成功时返回原进程在提交前准备的结果；查证失败抛 `ChapterCommitUncertain`，不清理、不把任务设为普通失败，也不自动重发。重启发现原私有收据或损坏收据时保留 running/execution 与项目收尾屏障，待核对；收据由个人投影递归过滤。提案及 batch 分别由对应实现者在原事务持久化身份并维护断点。

单章及 batch 的 `ChapterJob` 持原 claim。章节文本和向量通过章节专用可信 provider factory/guard，每次真实发送在同连接 lease 内验证原 actor/项目/Task/Chapter；响应头后释放门禁，不锁整段流。文本与向量配置各自在 Job 构造时从**同一不可变配置快照**构造 provider、捕获指纹；配置或默认向量模型变更后拒绝后续发送。已配置但无效的向量模型失败关闭，未配置仍可用关键词。文本与向量均把原 claim 绑定到 guard，向量缓存 preflight 和 context builder 的重复包装不能脱离原绑定。

## 已验证与仍需闭合

- `tests/test_chapter_claim_author_wiring.py` 在可信 SDK 私有快照、模型来源列及任务身份协议接入后 **12 项通过**：覆盖手工复位后新 batch claim 不被旧 abort/保存覆盖、伪造及历史无证据任务、客户端注入、提交确认丢失、不可核实后的重启屏障、损坏 receipt、向量来源 A→B→A、配置损坏和模型/传输变更后的出站拒绝。Fake 只用于 SQLite 功能断言，不能证明真实网络时序。
- 管理员取消并发的正文 `RETURNING` 与 commit 两种顺序，由对应测试所有者在最新代码验证 **2 项通过**；活 SQLite 游标未被取消，成功赢家的正文/日志/版本与失败侧取消日志均符合原事务边界。旧作者 `tests/test_chapters.py` 曾在任务公开身份协议更改前 9 项通过，最终适配由测试负责人复跑。
- 文本与向量每次操作均使用可信 factory 创建的私有 SDK，实际 request hook、同一 lease 和原 claim 必须同时通过；Fake 的预检与本机 HTTP/PG 并发测试承担不同验证范围。模型默认选择仍按作者旧行为在 Job 构造时进行，E1 的受理时持久绑定未完成。
- `tests/test_chapter_claim_author_postgresql.py` 两项已收集：真实 SDK 和本机 HTTP 在文本或 context builder 的向量响应头前阻止作者复位；响应头后复位并新建 claim，旧执行体不能续写、发送后续文本或覆盖新章。该测试只允许由复用的协作 PG fixture 在 `AINOVEL_COLLAB_POSTGRES_TEST_URL` 指向的本机隔离 `collab_test` 库随机 schema 运行；尚无最终通过结果。
- 可信 provider 的实际请求与不可变模型/凭据绑定已实现，最终真实 PG 测试仍待执行；不能把 Fake 的短预检或普通对象字段比较当成恶意请求 ABA 的证明。系统旧任务恢复、提交状态长期不可核实的人工修复、用户删除/裁剪、协作者模型授权和各派生任务仍受整体任务门槛约束；协作者章节 AI 未开放，E4/E1 尚不能据此宣称完成或部署。
