# 阶段 E：分析执行身份、提交收据与派生索引

## 审查与修复

- 独立分析从引擎领取事务冻结的 `TaskExecutionIdentity` 构造执行快照，固定原任务 UUID、实际 actor、原令牌版本、作品和章节、入队时间、执行密钥、授权与原输入摘要；批量分析还冻结当前步骤和原分析引用。授权、输入、轮次不匹配时失败关闭，不能使用当前整数 ID 查到的任务续签旧执行。
- 当前仅允许作者分析。原文本模型和共享向量选择由任务受理时固定，服务端用严格反序列化与模型实例 UUID、配置指纹校验；缺原 token、缺绑定或旧版绑定不能从当前用户/当前配置补签。协作者仍需真实模型、派生归属和向量授权链闭环后才能开放。
- 文本出站使用统一 `for_task` 及真实 SDK 逐跳 lease，每次持认证、可见性、作品、任务及两种模型配置屏障并复验；最终保存和任务事件发布另持写屏障，核对原章节版本、任务、分析引用、可见性和模型，分析、设定回写及剧情记忆在同一事务中提交。无法撤回已发送的内容。
- 独立分析在任何可能模型或向量出站前写随机身份的 `_analysis_pending`；成功写入事务以 `_analysis_completion` 原子替换，收据绑定原执行、输入摘要、分析行、结果摘要及内容版本。结果提交确认丢失时，只按本次精确收据和完整结果证明已提交，否则保留未知屏障，禁止自动重发。终态裁剪/删除也须验证原收据与持久任务结果。内部标志由个人任务投影过滤。
- 批量分析的每次发送和结果写入均核验当前 cursor、阶段、章节、冻结的 `rich_analysis` 和运行中收据。派生索引时要求已完成收据和分析行，并沿真实 retry 链核验同 actor/作品/批量任务及原来源；连续重试中间任务合法写入的分析可以继承，断链、来源删除或伪造来源失败关闭。
- 记忆向量任务的旧执行器尚不能逐批继承并核验原用户、token、共享向量选择与发送/写入身份，故分析成功后暂只验证收据、**不创建索引任务**；`AnalysisIndexOutcome` 明确区分关键词模式与向量索引暂不可用，独立分析结果 warning 向用户说明，批量章节 warning 正在接入。待索引执行链闭环，再由受控 KB 入口恢复派生，禁止使用新默认配置为旧执行补签。
- 作者自动分析由真实源生成任务及其完成收据继承原 actor/token/模型；接受提案的 HTTP 路径使用本次认证身份并在同事务冻结模型；拆书路径验证原任务与可信步骤/断点后继承其身份和模型。任何缺证路径均不假冒作者派生。
- 对同一次执行内输入被替换的反例，原实现会继续发送、保存或接受终态收据；现将类型敏感的规范 JSON 摘要纳入每跳、写入和收据验证，不在收据中暴露原输入。
- 为避免模型逐字输出时每个字符都执行一次持锁检查，分析只合并已经同步就绪的同流文本，不等待网络或定时器；错误前文本先发，未完成预读在取消时结束。合并后的每帧仍在任务引擎认证及授权门禁内发布。

## 验证记录

以下命令在 `backend/` 执行。测试配置、任务引擎和代码持续集成，较早结果不能代替最终复验。

- 新增输入替换的发送前、流中和终态收据 3 项先实际失败，修复后连同批量多次重试及作者端到端 `tests/test_memories.py::test_memory_vectors_task` 共 **5 passed in 7.19s**。该端到端经真实 HTTP 和任务引擎走分析、`on_success`、索引任务和向量检索；模型在此测试中使用 FakeLLM。
- 补充批量发送前/中输入替换及独立分析索引派生前输入替换：**3 passed in 4.51s**。
- 新输入摘要之前的 SQLite 组合：`tests/test_collaboration_analysis_execution.py`、`tests/test_analysis.py`、`tests/test_collaboration_analysis_task_visibility.py`、`tests/test_memories.py::test_memory_vectors_task` 和 `tests/test_task_receipt_deletion_guard.py::test_proven_completion_allows_project_deletion` 共 **110 passed in 116.38s**。更早的批量身份/收据组合 **15 passed**，批量及提案组合 **11 passed**；后续代码变化须重跑。
- 输入/token 摘要之前的索引专项 **49 passed in 96.68s**；旧向量子任务路径目前已关闭，此结果不能代表新索引执行链通过。
- 作者分析经统一 guard 的独立/批量身份、真实 SDK、禁用未受控索引和提交确认丢失组合 **17 passed in 18.15s**；原模型及共享向量绑定、旧版/缺失绑定、原 token 和拆书步骤替换共 **16 passed in 14.05s**。真实 SDK 的 HTTP 307 首跳后以同一整数 ID 重建文本或向量配置，第二跳均未发送：**2 passed in 2.81s**。
- `tests/test_collaboration_analysis_execution.py` 在当前 v2 绑定/事件门禁集成过程中 **109 passed in 183.30s**；后续配置选择门禁锁序调整后，作者分析、事件撤销、提交收据篡改、拆书来源、真实 SDK 和 batch retry 组合 **15 passed in 27.19s**。索引结果显式区分关键词与暂不可用的补充用例 **2 passed in 1.92s**。
- 非等待就绪合并接入后完整组合：`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_analysis_execution.py tests/test_analysis.py --tb=short`，**129 passed in 176.57s**。
- 同步就绪文本合并的原文/错误顺序、网络阻塞立即返回和预读取消清理，以及令牌撤销、任务取消、真实 SDK 重定向与模型 ABA 共 **11 项通过**；未以延长超时替代原行为验证。
- HTTP 提案采纳的正常派生和令牌中途失效回滚由 review_proposal 报告 **2 passed**；章节完成事件、作者润色自动分析和自动分析状态由 series_check 报告 **3 passed**。这些结果均在并行集成中取得，最终仍须复验。
- 含之前执行身份版本的隔离 PostgreSQL 合并实跑：**6 passed in 26.67s**（本分析专项 5 项、索引专项 1 项）。该结果早于持久收据与 batch attempt fencing，不代表新版本已经通过。
- 新 PostgreSQL 分析专项 **21 项已收集、未在本代理运行**，覆盖真实双连接、提交确认丢失的成功/回滚/替换/破损、两种隔离级别下步骤/输入/token/管理员取消标记与模型 ID 复用，以及先持锁保存的赢家；须由主会话错峰运行。真实 SDK 逐跳重定向另由 SQLite 本地 HTTP 用例验证，FakeLLM 不构成真实网络发送的证据。
- 范围内 Ruff 通过；后端未配置独立 Python 类型检查器。

## 待集成

分析 handler 已接收领取锁内生成的 `TaskExecutionIdentity`，并从自己确认的 pending/completion 推进事件和终态快照；SSE 源证明、索引安全链及其他协作入口仍在集成。须按最终代码重跑 SQLite、独立 PostgreSQL 和范围内质量检查。当前协作者分析入口持续关闭，不能据此声明整个协作 AI 验收已完成。
