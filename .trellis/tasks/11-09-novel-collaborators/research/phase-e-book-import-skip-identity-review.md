# 拆书跳过任务身份复核

## 发现与修复

- 红灯使用真实 SQLite 与 HTTP 请求：旧任务删除后，同一整数 ID 被另一个暂停的拆书任务复用；请求携旧 `task_key`，原实现仍返回 202，将新任务的断点推进并重新排队。
- HTTP 由路由传递客户端原有的 `task_key`；服务入口未收到身份时失败关闭，不能根据当前整数 ID 回填。首次本人权限查询按该 UUID 限定，持 User、作品父锁后重新验证任务、当前本人权限、初始授权轮次及入队轮次。
- 跳过断点的条件更新绑定原整数 ID、原 UUID、初始 `queued_at`、暂停状态及取消/执行占用状态；继续排队仍传递同一原 UUID。断点推进和继续排队在同一事务中提交；继续被拒绝时回滚断点修改。
- 历史空 UUID、缺失和错误身份均不能跳过；正确身份的作者恢复仍可用。HTTP 缺少参数为 422，UUID 错误或不匹配为 404。

## 验证

- `tests/test_book_import_skip_identity.py`：SQLite **5 passed**，覆盖真实 ID 复用、首次查询后替换或轮次变更、缺失/错误/历史空身份、正常恢复和继续被拒后的事务回滚。
- 主会话在本机隔离 PostgreSQL `collab_tasks` 上实跑 `tests/test_book_import_skip_identity_postgresql.py`：**2 passed in 8.40s**，覆盖首次查询后以及持父锁、CAS 前的两次真实跨连接身份替换。
- `uv run ruff check app/services/book_import/service.py app/services/book_import/reverse.py tests/test_book_import_skip_identity.py tests/test_book_import_skip_identity_postgresql.py`：通过；Python compileall 通过。项目未配置独立 Python 类型检查器。
- 旧拆书端到端的 4 个定向用例在 skip 之前因测试辅助函数未传任务流必填身份而返回 422；`task_review` 已接手迁移该文件的任务凭据链并重跑，其结果以该代理后续记录为准。

这些证据只覆盖本切片，不代表完整后端、前端、迁移或阶段门禁已通过。
