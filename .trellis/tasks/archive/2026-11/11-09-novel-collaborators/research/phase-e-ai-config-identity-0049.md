# 模型配置实例身份：0049 独立复核

## 实施与边界

- `AIConfig.instance_key` 是服务端生成、全局唯一的规范小写 UUIDv4；配置内容、整数 ID 或创建时间相同不能证明是同一配置。API 创建和更新中的同名字段被忽略，响应不返回该值。
- 0049 在 SQLite 和 PostgreSQL 中逐行给旧配置分配独立身份，不重建有外键引用的配置表、不改变旧任务输入，也不从当前配置为旧 v1 任务补签。普通 ORM 创建和直接 SQL 插入均能得到独立身份；数据库拒绝修改已有身份。
- SQLite 受原位加列限制，列反射为可空；已存在行回填后不为空，新插入在语句内由触发器生成并校验。随机碰撞时，`INSERT OR IGNORE`、`INSERT OR REPLACE` 与多行插入都会安全失败，不保留 NULL，也不替换原配置。直接 SQL 的 `INSERT ... RETURNING` 在 AFTER INSERT 触发器前求值，可能返回 NULL；服务端 ORM 显式生成 UUID，正常提交后读取有真实值。PostgreSQL 列为 NOT NULL，由 BEFORE INSERT 触发器为直接 SQL 生成 UUID。
- 降级先取得 SQLite 写锁或 PostgreSQL 相关表排他锁。无依赖的配置与终态 v1 任务可保留并移除内部列；活动或身份未定的执行、v2 模型/共享向量绑定、未决收据、未知索引和无法证明安全的候选分块阻止降级。含转义的损坏 JSON 在无法证明内容时失败关闭。保守阻断分块不代表内容本身已经损坏。
- 部署前停止新受理并排空旧执行体与外呼，做整库备份，再迁移并仅启动相应新版本。回滚前同样停机、备份并核对依赖；整库恢复时先停止快照之后的全部旧进程，再恢复同一快照并启动与其 schema 匹配的代码。不能把迁移回填的配置 UUID 作为旧任务曾经使用该实例的证据。

## 验证

- SQLite：`tests/test_ai_config_instance_migration.py` **11 passed**，覆盖 503 条旧配置、外键及扩展引用保留、空库、安全与受阻降级、升级/降级中断重试、数据库约束、SQL 冲突策略、SQLite 整库快照及 API 伪造字段。
- 既有单书备份、模型配置及备份隐私专项 **12 passed**；新增两处单书 `app_schema` 断言及历史设定往返 **2 passed**。上述版本断言现已交还主会话，以便后续迁移递增 schema 标记。
- 主会话在隔离 PostgreSQL 16.15 以同版本 `pg_dump` / `pg_restore` 实跑 `tests/test_ai_config_instance_migration_postgresql.py`：**5 passed in 26.67s**。覆盖空库及旧库、`create_all`、数据库约束与直接 SQL、跨连接降级竞争，以及完整数据库快照后恢复随机 schema 并核对原 UUID 和不可变触发器。
- 本分工涉及文件 Ruff、`compileall` 与定向 `git diff --check` 通过；本仓未配置 Python 静态类型检查。本记录不代表模型绑定 v2 的其他调用链、协作者 AI 或全仓质量门禁已完成。
