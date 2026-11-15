# E4 作者批量章节占用与提案来源

2026-11-10；单进程任务引擎，旧进程必须排空。当前实现仍只允许作品作者进入批量 AI；不得据此开放协作者批量入口。

## 实现

- BatchJob 接受引擎领取时的不可变 TaskExecutionIdentity，绑定 UUID、queued_at、execution_key、原 token_version、真实 actor/project、原 grant、input、checkpoint 和 cursor；不从稍后读取的任务行重签。已确认的 checkpoint 变化才推进同源副本。每章每次 attempt 用 `claim_batch_chapter` 创建独立章 claim；抢占及 checkpoint.current 同事务提交。`_batch_attempt` 每次产生独立 UUID，释放后仍保留：防止同一执行体中 A→B→current=None 的 ABA。
- 受理时固定可信文本模型与共享向量选择；续跑时复验原配置实例 UUID 和指纹，不重新选择默认模型。发送和落库持原 task/claim/attempt 及模型门禁，任务事件和最终结果也验证当前断点、活动章节占用、可见性、模型与已核输出；原 claim 缺失时不发布正文 delta。
- checkpoint 写入在用户/可见性/项目屏障和 Task 行内复验原执行身份，原 input、完整 checkpoint 和 cursor 以保留 JSON 类型的比较及 SQL CAS 约束。真实章节生成、文本/向量发送和结果保存传入本 attempt 的 `ChapterClaim`；重试或取消的清理只能使用原 claim。checkpoint 的内部 claim、attempt 和完成/分析收据从公共投影递归排除。
- 正文、版本、真实 actor 日志、当前章精确释放、`_batch_completion` 和 analyze 断点在同一事务提交。收据绑定原 task/queue/execution/claim/attempt/actor/project/grant、cursor、版本和内容 revision。未知提交只能凭相同收据与版本/日志确认，缺证保持 running。显式 retry 复制断点后重新验证来源 Task、旧轮次、版本、日志与内容 revision；原记录丢失或损坏时失败关闭。
- 每次正文成功同事务产生独立分析 ref（复用 `SceneReviewAnalysisReceipt`，不要求存在富文档提案）。分析真正出站之前登记 running；分析结果和记忆写入同事务完成 receipt。模型出站和分析落库均在锁内核验原 Task、cursor、章节、断点 ref 和 running 收据；记忆索引派生还核验 completed 收据及分析归属。恢复时 receipt 缺席才允许首次发送；running/failed 表示可能已经发送，不自动重发；completed 验证关联结果后只推进断点。旧分析的失败清理必须匹配原 checkpoint、完整 ref、内容指纹和未完成状态，不能修改另一轮收据。原 Task 的真实 Batch `AnalysisExecution` 用于分析及记忆索引派生，身份或授权失效直接上抛。
- 运行中清理的 `BatchAbortSnapshot` 在初次失败时不可变复制原 claim/input/checkpoint/cursor 和执行身份；恢复只能精确释放原 claim并验证原快照的预期变化。终态由任务引擎以原快照和原 Task/input/checkpoint/cursor 原子 CAS；旧 finally 不能结束新 attempt。启动恢复仅在旧进程排空后使用原持久任务；原始身份缺损的历史生成占用不猜测，保留人工核对屏障。
- 对确认旧执行已排空、但无法确定外部请求结果的孤立 batch，受控 API 在调用方事务内精确释放原 claim 并写入 `_batch_outcome_unknown`；其标记含原任务、执行、章节、claim、attempt、cursor 和管理员意图。任务状态转换和统计须由引擎在同一事务提交；ACK 丢失只能以全新会话核对完整原标记、原终态和章节不再持有原 claim。无论标记是否损坏，自动重试和清理均失败关闭，标记不会进入公开投影。引擎接线与管理员 drain 测试由任务引擎负责人完成。
- 章级待审 proposal 同事务释放原 claim并保存私有 `execution_receipt`；恢复前重新验证待审行、原队列/执行/grant、本人访问和分块完整性，缺证不能视为无提案重发。新 proposal 将生成时可信模型绑定写入私有分块；采纳时按原任务 actor/project、当前作品 owner、原 grant、config id 反序列化核验并传入日志。旧提案缺绑定时保持作者兼容但来源标为未知，不以当前配置猜测；已删除配置不改变原来源。SQL 错误转换为无原始异常链的安全错误，防止正文及配置指纹出现在日志。
- 单章待审事件验证领取 input 恰好删除原 claim、checkpoint 恰好新增原 proposal，并锁内匹配原 claim/queued/execution/token、提案身份和分块。采纳时自动分析只使用本次 HTTP 原认证快照：授权失效则原事务回滚；模型选择不可用则保留合法正文并返回固定提示，该提示保存在幂等结果中。分析完成后，批量任务将关键词模式或暂未开放的向量索引提示合并到章节 warning，不伪报分析失败。

## 范围与边界

- 批量分析复用的 receipt 名称历史上含 SceneReview，但其现在仅表示一次分析发送/结果，与是否存在提案无关。恢复提示中的 unknown/failed 不代表模型没有产生费用或副作用；它们均不会自动重发。
- 没有 claim/执行凭据或来源 receipt 损坏的旧任务不能由状态或整数 ID 反推，因而不会自动恢复。单独 Task 或项目清理仍需与 engine/task_service 的 unresolved receipt、admin intent 和项目 drain 保护一同评审。
- 协作者 batch 与单章入口尚未开放，真实多实例调度、跨实例 lease、远程 LLM 成本撤销、生产部署与迁移均不在本切片的证明范围。

## 验证

- 先红反例：旧批量 checkpoint 覆盖后来的 attempt；提交正文后第二笔断点失败导致 retry 重发；普通分析提交后断点失败导致第三次模型调用；旧 analysis 失败清理可修改更换内容指纹的收据；旧 proposal 错误 actor 绑定仍能采纳；SQL 异常可带出私密参数。上述均已补真实行为回归。
- SQLite 专项、作者/提案/读取回归、类型检查及 PG 测试的最终结果以主会话所运行命令输出为准。`tests/test_batch_chapter_claim_postgresql.py` 收集 7 项，`tests/test_collaboration_review_claim_postgresql.py` 收集 4 项；本代理未运行 PostgreSQL，由主会话错峰执行。
