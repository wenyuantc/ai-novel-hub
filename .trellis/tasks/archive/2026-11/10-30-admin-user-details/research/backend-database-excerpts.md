# 本任务相关规范摘录：backend/database-guidelines.md

来源：`.trellis/spec/backend/database-guidelines.md`

规划时 SHA-256：`3088af7b30ed58b99cd01c1b4daab3141a047e2105a45de98f077eded515274d`

仅摘录相关原文，完整规范是权威来源；源文件变化时重读相应段落。本摘录不是新增规范。

## 原文 L30–L76

### Engine 与 session 配置

`backend/app/core/database.py`：

```python
def create_engine(database_url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    engine = create_async_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        # SQLite 默认不启用外键约束，开启后 ON DELETE CASCADE / SET NULL 才生效
        @event.listens_for(engine.sync_engine, "connect")
        def _fk_pragma(dbapi_conn, _record):
            ...
            cursor.execute("PRAGMA foreign_keys=ON")
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False：提交后仍可读取属性，避免流式响应期间触发隐式查询
    return async_sessionmaker(engine, expire_on_commit=False)
```

`create_app()` 将 `engine` 和 `sessionmaker` 存放在 `app.state` 上；engine 在
lifespan 中释放。每个请求的 session 来自 `backend/app/core/deps.py` 中的 `get_db`：

```python
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session
```

流式任务**不得**复用请求 session：路由在 prepare 步骤之后调用
`await db.close()`，SSE 生成器通过 `async with sessionmaker() as db:`
（`SessionmakerDep`）打开自己的 session。

### 事务

范围内管理员写入使用 `AuditOperation.commit` 把业务与成功审计同事务提交；它在审计 flush 前先做业务 flush。章节必须传 `flush_fn=version_service.flush_versioned` 保持原版本409，Skill传 `_flush_unique`；审计错误不能误译为业务唯一键冲突。失败先整体回滚再写安全失败，通常提交后再refresh；工坊approve为保留原锁契约，先flush/refresh并生成output，再audit.commit，不得移动其既有投影时机。不得以HTTP响应成功后补写替代同事务契约，详见[审计规范](./admin-audit.md)。

没有 unit-of-work 包装。每个有写操作的 service 函数都以显式的
`await db.commit()` 结束，若需返回对象，则紧接着 `await db.refresh(obj)`。
当同一函数中后续查询需要看到变更时，使用 `await db.flush()`
（`ai_config_service.delete_config`）。

---

## 查询模式

## 原文 L130–L170

### 原子状态迁移

对并发敏感的状态迁移使用条件 `UPDATE ... WHERE`，并检查
受影响行数（`backend/app/services/generation_service.py`）：

```python
result = await db.execute(
    update(Chapter).where(Chapter.id == chapter.id, Chapter.status != "generating").values(status="generating")
)
await db.commit()
if result.rowcount != 1:
    raise ConflictError("该章节正在生成中，请稍候再试")
```

回退使用同样的守卫条件：`... where(Chapter.id == chapter_id, Chapter.status == "generating")`。

### 聚合

计数/求和在 SQL 中完成，而不是在 Python 中：`project_service.chapter_stats_subquery()` 使用
`func.count(Chapter.id)` 和 `func.coalesce(func.sum(Chapter.word_count), 0)`，并通过 `outerjoin` 关联。

### 部分更新

更新 schema 使用可选字段。对于可空列，使用 `data.model_fields_set`
区分「未传」与「显式传 null」
（`"base_url" in fields`、`"outline_id" in fields`）；通用更新则使用 `model_dump(exclude_unset=True)`
（`project_service.update_project`）。

### 字数规则

`word_count` 始终由服务端通过 `chapter_service.count_words` 计算：

```python
def count_words(text: str) -> int:
    """字数统计：按非空白字符计（中文惯例）。"""
    return len(_WS_RE.sub("", text or ""))
```

每当 `content` 变化（创建、更新、生成 `done`）时都要重新计算。
客户端从不发送 `word_count`。


## 原文 L230–L249

## Migration

- 位置：`backend/alembic/versions/`：`0001_initial_schema.py`（`down_revision = None`）、
  `0002_ai_config_model_params.py`（`down_revision = '0001'`，新增 `thinking_level`，
  `server_default='off'`，以及可空的 `context_window_k` / `max_output_k`）、
  `0003_user_admin_fields.py`（`down_revision = '0002'`，新增 `users.is_admin` / `is_active` /
  `token_version`，通过 `server_default` 回填；`test_0003_backfills_existing_users`）、
  `0004_ai_config_model_type.py`（`down_revision = '0003'`，`ai_configs.model_type` 回填 `text`；
  `test_0004_backfills_model_type`）、`0005_chapter_versions.py`（`down_revision = '0004'`，只新建
  `generation_logs` 与 `chapter_versions` 两张表，不回填；`test_0005_chapter_versions` 断言外键规则与
  `(chapter_id, seq)` 唯一约束）、`0006_tasks.py`（`down_revision = '0005'`，新建 `tasks`，给 `generation_logs`
  加可空 `task_id`；`test_0006_tasks` 断言外键规则、部分唯一索引确实只约束活动任务、存量日志 `task_id` 为空，
  `downgrade -1` 后存量日志保留）、`0007_story_entities.py`（`down_revision = '0006'`，只改结构不调 AI：
  `world_settings.category` 回填 `other`，`characters.always_in_context` 对 `role_type` 为「主角」/`protagonist`（trim + lower）回填 true，
  插入 20 个全局预置关系类型；`test_0007_story_entities` 断言简介 / 旧关系备注原样保留、回填结果、预置类型与
  `PRESET_RELATIONSHIP_TYPES` 一致、外键规则、`(pair_low, pair_high)` 唯一，`downgrade -1` 后存量数据保留）、
  `0008_careers.py`（`down_revision = '0007'`，只新建四张表、不回填；`test_0008_careers` 断言外键规则、两个唯一约束、
  删除职业时阶段 / 挂接 / 历史在同一语句内级联删除，`downgrade -1` 删除四张表且存量角色保留）、
  `0009_chapter_analysis.py`（`down_revision = '0008'`，新建 `chapter_analyses` / `analysis_changes`，`projects.auto_analyze`
  `server_default=true` 回填，`tasks.serial_key` 可空 + 索引；`test_0009_chapter_analysis` 断言外键规则、回填、删章节级联、

## 原文 L523–L537

## 常见错误

- 在面向用户的 service 中仅按主键查询（`db.get(Chapter, id)`）：
  这会跳过归属校验。`db.get` 只在归属已被证明的地方使用
  （任务运行阶段使用创建时已校验归属的 `chapter_id`，且 `build_job` 仍按 `task.user_id` 走 `get_owned_*`；`auth_service.get_user` 按 token subject 查询）。
- 手动构建 SQLite engine 时忘记 `PRAGMA foreign_keys=ON` —— CASCADE/SET NULL 测试会以错误的方式静默通过。
- 将 AI 生成的字符串插入 `String(n)` 列时没有截断。
- 在 `StreamingResponse` 生成器或后台任务中复用请求的 `AsyncSession`。
- 新增任务类型时在 `validate_and_claim` 里 commit：章节抢占与任务插入必须在同一事务，否则撞唯一索引时抢占无法回滚。
- 部分唯一索引只写 `postgresql_where`：SQLite 测试会建成全表唯一索引，已结束任务也会阻止新任务（`uq_skills_global_slug` 同理：只写 PG 条件时，SQLite 上所有 Skill 的 slug 都变成全表唯一，私有 Skill 不能再与全局或他人的 Skill 同名）。
- 以为普通唯一约束能约束可空列为 NULL 的行：`(owner_user_id, slug)` 对全局 Skill（owner 为 NULL）不生效，需要额外的部分唯一索引。
- 将某个会被向导批量替换的表的外键改为 CASCADE。
- 重新读写 `projects.style`：它自 0013 起只为 downgrade 保留；写作风格走 `writing_styles` + `default_style_id`（见 prompts-styles.md）。
- 直接改 `Relationship` 字段而不经 `apply_change`：没有历史、绕过角色对唯一与同项目校验。
- 直接改 `CharacterCareer.stage_id` / `note` 而不经 `career_service.apply_assignment`：没有挂接历史，绕过主 1 副 3 与类别校验。
