# 复合分类读取的一致快照

- 日期：2026-11-05。
- 范围：为多批候选／字段／标签／规则读取核实事务边界；只规划，不修改全局 engine 或产品代码。

## 源码与官方依据

- `backend/uv.lock:1151` 锁定 SQLAlchemy 2.1.6；pyproject 的最低版本不是实际锁定版本。
- `backend/app/core/database.py` 目前只为 SQLite 配置 foreign_keys，没有设置显式 SELECT BEGIN 或全局隔离级别。
- `backend/app/core/deps.py` 提供 SessionmakerDep，可创建独立查询 session。CurrentUser 使用的请求 session 可能已经查询过，不能在它上面临时切换隔离级别。
- SQLAlchemy 2.1.6 官方 [Session 事务文档](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_6/doc/build/orm/session_transaction.rst) 要求在事务执行 SQL 之前设置隔离级别，不能在已开始事务中改变。
- 官方 [异步文档](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_6/doc/build/orm/extensions/asyncio.rst) 提供 AsyncSession.connection(execution_options=...)；[连接文档](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_6/doc/build/core/connections.rst) 说明隔离参数是连接／事务级且回池复原。
- 官方 [SQLite 方言文档](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_1_6/doc/build/dialects/sqlite.rst) 说明 legacy 驱动模式下 SELECT 不必然开启真实事务；仅有 Session.begin 不能未经验证便声称获得跨查询快照。

## 冻结实现边界

1. 分类复合查询使用独立只读 session，在任何 SQL（包括归属查询）之前建立一致事务；在该 session 内再次校验项目权限与生命周期。
2. PostgreSQL 用连接级 REPEATABLE READ；SQLite 在当前 legacy 驱动配置下显式 BEGIN，确保真实事务覆盖所有 SELECT。若项目事务控制已变化，先核实，不能重复 BEGIN。
3. 元数据、版本、全部候选批次和字段有效值都在同一事务内读取；只做 SELECT，no_autoflush，不创建空槽／绑定或更新版本。会话结束释放事务，不改变全局 engine 或其他写请求的隔离策略。
4. 字段普通读可沿用已有多查询读前／读后 revision 守卫；分类跨批次用一致快照防止早批与晚批来自不同编辑时刻。二者的响应版本始终对应其数据，不拼接最后读取的新版本。
5. 实现后使用两个独立连接和可控屏障更新定义、标签及字段事件，验证响应是同一快照或明确冲突；SQLite 和 PostgreSQL 均须验证，不能从 mock 或单连接测试推断。

## 限制

本次只有源码／官方文档核对，没有执行数据库并发验证。实际查询完成后仍需检查项目快照与真实 actor 的权限策略，沿用既有服务边界；此方案不授权读取其他作者资料。
