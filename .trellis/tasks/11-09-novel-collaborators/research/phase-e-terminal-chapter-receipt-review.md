# 单章成功终态收据复核

## 发现与修复

- 真实 TCP HTTP 红灯：单章任务已真实生成、状态为 `succeeded` 且执行密钥已清空，删除任务仍返回 409；现有清理门禁无法区分成功完成和待核对收据。
- 新建单章 v2 收据在生成正文、版本、本人成功日志及原 claim 释放的同一事务中固定原 task UUID、入队轮次、execution UUID、claim UUID、章节与版本，以及规范 JSON 的输入、不可变版本和最终结果 SHA-256。最终 warning 在计算结果指纹前加入，终态持久结果必须与指纹一致。
- `completed_terminal_chapter_receipt(db, task)` 在原作品→任务→章节→版本→日志屏障内重新核对 actor/作品/任务/章节身份、规范 UUID、时间轮次、状态与取消标记、服务端任务输入、收据严格类型和结构、本人成功日志、版本内容/场景以及终态结果。调用方已暂存的目标 ORM 值不会被覆盖或提前 flush；不能证明时返回空值或向上抛错，由清理入口失败关闭。
- 原输入指纹只排除合法竞争时由 finalize 精确验证并清理的 `_admin_chapter_cancel` 一次性意图；成功终态仍不得带该意图。运行中提交确认未知继续按 `completed_chapter_receipt` 核验原 v2 收据，不重发或重写。
- 后续正常编辑当前章节，或该章节由新任务取得不同 claim，不会改变生成时版本和结果证据；删除旧任务不会释放新 claim。旧 v1 缺少指纹、版本或日志已丢失、结果无法验证时保持失败关闭，不能根据补发的统计 UUID 猜测原执行。
- 当前单章生成仍使用严格作者受理和作者 claim 协议；本验证器也不猜测携协作授权的旧记录。未来开放协作者单章生成时，须另行在收据中固定并复核原邀请轮次，不能直接放行现有无该证据的收据。

## 验证

- 真实 TCP HTTP + SQLite 定向：**23 passed**，覆盖有效成功删除、17 类身份/轮次/内容损坏、旧无凭据、作者后续编辑、场景目标、自动续写 warning、客户端伪造和管理员取消竞争。
- 后续补测 **4 passed**：结果和场景 ID 用布尔值伪装整数，以及改动正文后重新计算 result/version 哈希，仍因语义或版本不一致而拒绝。
- 新任务占用同章期间删除旧成功任务：**1 passed**，新 claim 完好且新任务最终成功。
- 当前引擎代码下，原成功提交确认丢失、确认不可用保留运行状态及管理员成功竞争：**3 passed**。`task_review` 对真实成功单章的作品软删及管理员 purge 另验证通过（其记录为准）。
- `uv run ruff check app/services/generation_service.py tests/test_chapter_terminal_receipt_identity.py tests/test_chapter_terminal_receipt_identity_postgresql.py`：通过。项目没有独立配置的 Python 类型检查器。

## PostgreSQL 阶段验证

主会话在隔离 PostgreSQL 16.15 上运行 `tests/test_chapter_terminal_receipt_identity_postgresql.py`：**4 passed in 17.54s**，覆盖真实生成任务整数 ID 复用，以及生成版本被篡改，在校验锁前和锁后两种次序下的隔离。后续协作者接线尚在进行，该结果仅适用于运行时的作者代码快照；完整生命周期、迁移及全局阶段门禁仍需最终验证。
