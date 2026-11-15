# E6：协作者关键词索引的可独立交付切片

## 范围与未完成边界

- accepted editor 且 `allow_owner_ai=false` 的知识库上传在持认证/User 与项目写屏障的同一事务中解析、分块和建立关键词；不创建 Task，不解析或发送任何向量模型。响应为现有 `{document, task}`，其中 `task=null`、文档 `ready_keyword_only`，并附关键词提示；作者上传仍走原后台任务。
- 编辑者请求文档重建或全书重建，授权关闭时先返回 `403 owner_embedding_not_allowed`，不修改文档、分块、向量或任务。授权开启时当前返回 `409 collaboration_embedding_unavailable`，不偷偷改成关键词任务，也不代作者排队。授权开启但作者未配置模型的情况同样暂时关闭，待固定选择的完整协议落地。
- 关键词 KB/记忆搜索只接受真实请求的 `AuthenticatedActor`，冻结原邀请 UUID/revision，并在新事务中先取得认证/User → Project 写屏障。命中的索引行、业务来源、正文、引用、enabled/pinned/章号及关联分析均需与同作品一致；作者搜索也在输出前重新取锁并复核命中。撤权、令牌失效或数据不一致均失败关闭，不返回原内容。
- 记忆人工编辑继续只清理被修改记忆的旧向量，其他向量不变；编辑者不会自动派生作者名下 `kb.index`。现有普通作者记忆索引与分析派生的作者收据路径保留。
- **E6 尚未完成：** 同步共享向量查询、`knowledge_for_project` / 章节 / 向导 / 实体 / Agent 的 editor 上下文、编辑者后台 document/memory/rebuild、授权 actor 派生、向量来源固定、每批发送 lease 以及按原执行/文档 claim 的写入 CAS 均未接通。`TaskExecutionIdentity` 与原文档 claim 需共同进入 IndexJob；协作 `for_task` 真实发送 lease 尚在改造。不可开放 editor 向量入口或标记 E6 完成。作者现有向量路径亦不能当作协作者闭环证据。
- 向量授权开启后暂时拒绝而非静默转关键词；前端需识别此阶段性限制。关键词上传写入已提交后若原 token/授权失效，返回 401/404/403；数据已经写入，重试会触发正常文件去重，不自动重放。

## 红灯与验证

- 变更前 `cd backend && uv run pytest -q tests/test_collaboration_shared_index_e6.py`：4 failed，真实 TCP HTTP 的无授权上传/检索、跨书或伪造正文、授权情况下失败关闭均因原严格作者入口返回 404。
- 实现中一次真实 TCP HTTP 基础回归：4 passed（33.21s）；同时原 KB、记忆、检索写入回归曾得 83 passed（125.25s），该轮运行中继续完善了最终复核，不能作为最终树验证。
- 追加人工写竞态前的 SQLite 兼容快照：`cd backend && uv run pytest -q tests/test_collaboration_shared_index_e6.py tests/test_kb.py tests/test_memories.py tests/test_collaboration_retrieval_callers.py` 为 86 passed（97.27s）。新加人工写/上下文保护后仍须最终复验。
- `cd backend && uv run pytest --collect-only -q tests/test_collaboration_shared_index_e6_postgresql.py`：4 collected。主会话已在专用本机隔离 PostgreSQL 错峰实跑该文件：4 passed（15.29s）；这是当前关键词切片的运行时快照，后续统一身份及共享向量接线修改后仍须复验。
- 上述 PostgreSQL 用例分别验证撤权先提交、关键词上传提交后且响应前撤权、作者搜索命中后正文在另一连接修改、关键词搜索持原授权时撤权阻塞；检测实际阻塞 PID 和项目锁，不以 SQLite 锁语义代替。
- 人工记忆提交后降级或撤销再重邀的红灯为 4 failed、4 passed；修复后 `tests/test_collaboration_memory_write_e6.py tests/test_collaboration_shared_context_e6.py` 为 22 passed（36.06s），覆盖创建/修改/上传在原身份捕获前后，以及直接上下文遇跨书引用的失败关闭。主会话复跑 `tests/test_collaboration_memory_write_e6_postgresql.py tests/test_collaboration_shared_index_e6_postgresql.py`：13 passed（55.97s）；后续 IndexJob 接线后仍须整体复验。

## 重要并发与接线限制

`snapshot_embedding_inputs` 自身只校验来源/正文，不包含可见状态与筛选字段；调用端额外检查索引元数据、源文档 inject、记忆 chapter/分析所属作品，以及原始 Hit 文本。关键词搜索在取得项目锁后做最终校验，避免撤权或正常写入越过返回结果；作者搜索在外部请求完成后开启新事务取锁，再重新核验先前命中，而不在网络发送期间持项目写锁。现有不接受真实 actor 的通用 `knowledge_for_project` 只能用于已证实严格作者的调用方，不能据此启用协作者 AI 上下文。真实 provider/收费与每批向量写入在本切片未验证。
