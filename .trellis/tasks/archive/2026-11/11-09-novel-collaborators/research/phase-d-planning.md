# 阶段 D：人工规划、资料与分析

## 已迁移

- 笔记列表、详情、新建、修订、标记及删除；规划矩阵、条目选择、标签、支线、节点、交汇、批量编辑及绑定；手工大纲树、追加、排序、细纲、建章、快照、JSON 大纲导入预览/导入与导出。写入先取得认证屏障和作品父行锁，再重读身份；资源查询限定作品，已有修订 CAS 保留。大纲导出用 `export` 能力。
- 知识库设置、已有文档及分块读取、注入开关和删除；记忆列表、新建、更新、删除。跨用户记忆维护只同步本地关键词检索单元，不使用作品作者的 embedding 配置排队。文档上传、重建索引及向量检索尚未开放协作者。
- 提及目录、全文、汇总、来源窗口及纯本地字面匹配预览。作者和已接受协作者仅能查询当前 actor 自己的聊天；纯管理员仍不能查询聊天。作品单书协作者无系列权限时，规划条目的 `series_shared` 不显示；系列作者/接受的系列协作者和原有管理员人工读取规则保持原匿名标记。编辑/删除系列条目的安全冲突由 `series_policy` 和设定服务处理。
- 分析状态、历史、详情及人工确认/拒绝/撤销：作者与已接受的作品协作者可用，纯管理员仍依旧权限返回 404；只投影当前 actor 可访问的本人任务 ID。管理员兼协作者的人工写入按管理权限与协作权限并集计算。确认/撤销持可见性写锁，不排队分析、记忆或向量任务。分析、回写条目与章节必须属于同一作品；大纲细纲输出和删除也限定子章节的作品。
- `outline_tree`、`sync_plans`、分析状态与详情被 Agent 共用，默认仍保留旧严格作者/管理员路径；仅人工 HTTP 调用显式选择协作能力。`get_owned_project`、`get_owned_chapter` 等原严格 helper 未扩权。

## 阶段 E 仍须处理

- 知识库上传/文档重建/全量重建和知识库、记忆模型检索仍在旧作者路径。HTTP 前置 `content.edit` / `ai.use` 仅确保查看者失败关闭；编辑者在严格旧 helper 上仍为 404，不代表协作上传和检索已完成。阶段 E 要按真实 actor、自己的模型配置、可选作者授权和后台任务身份接通，增加关键词检索（包括查看者只读）及编辑者上传的端到端测试，不得消费作者模型配置或产生作者费用。
- 知识库索引状态仍严格作者，因为其包含作者 embedding 配置和索引状态；章节检索上下文预览仍严格作者。AI 续写、展开、章节分析、建议重写及任务队列不使用这里的人工 helper。
- `/projects/{id}/planning/units/context-preview` 是本地规划文本拼装，原先 `get_manageable_project` 允许管理员；当前保留其原行为，普通单书协作者仍无法进入。阶段 E 应重新确定协作者预览权限、管理员兼协作者能力与数据边界，并覆盖真实 HTTP 和调用链。
- 系列标记仅供有系列权限或已有管理员人工读取权限的 actor 查看；不因此授予系列 ID、系列编辑或作者个人信息访问权。

## 验证

- `tests/test_collaboration_planning_access.py`：真实 HTTP 覆盖查看者、编辑者、陌生人、管理员与协作者并集、撤权/降权、系列身份、私有聊天和任务、跨作品子资源、作者 AI/Agent 严格路径及无模型副作用。
- `tests/test_collaboration_planning_postgresql.py`：真实 PostgreSQL 隔离 schema、两个应用实例；仅占项目行的事务先提交撤权/降权时，等待的笔记修改在锁后返回 404/403；人工修改先提交时撤权等待后完成；另验证分析回写的 PostgreSQL 子行锁。
- SQLite 大纲/规划/分析组合：`LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_collaboration_planning_access.py tests/test_outline_pro.py tests/test_outline_snapshots.py tests/test_outline_transfer.py tests/test_planning.py tests/test_planning_access.py tests/test_planning_context.py tests/test_analysis.py tests/test_writeback.py`：100 passed。
- SQLite 资料组合：`LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_notes.py tests/test_memories.py tests/test_kb.py tests/test_mentions.py tests/test_mention_compat.py tests/test_planning_readonly.py`：82 passed。
- 最终分析/管理员回归：`LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_collaboration_planning_access.py tests/test_analysis.py tests/test_writeback.py tests/test_admin_projects.py`：44 passed；最后补充 AI 入口反例后另行运行 `test_analysis_reads_hide_other_actors_tasks_and_manual_writeback`：1 passed。
- PostgreSQL：设置本机专用测试库 `AINOVEL_COLLAB_POSTGRES_TEST_URL` 后运行 `uv run pytest -q tests/test_collaboration_planning_postgresql.py`：4 passed。当前改动文件的 `ruff check` 全部通过，`git diff --check` 无空白错误。

## 复核补充

- 分析章节、历史和回写在定位父作品、完成授权及检查子资源一致性前禁止 autoflush；纯管理员的分析身份限制在读取历史前生效。已暂存修改的分析或回写目标先返回 409 而不覆盖待写值。新增拒绝分支测试覆盖陌生人、纯管理员、查看者及异常子资源的待写会话。
- 规划矩阵与来源解析、按章节查询细纲同时核验细纲和大纲的作品归属。异常跨书的细纲/大纲关联不投影 ID、标题，也不可作为写入目标。
- 场景规划 raw/effective 和新快照捕获前逐项核验 POV、标签及五类引用的作品归属；异常或缺失目标返回匿名 409 `scene_reference_invalid`，保留数据库原状。历史版本已保存快照的读取防护由章节复核负责。
- 场景 POV 的 ORM 待写值也校验目标作品；`PlanningUnit` 投影刷新会保留普通待写字段，来源身份或绑定代次未提交时返回 409，不先刷新覆盖。SQLite 与 PostgreSQL 均验证合法和跨书待写 POV。
- `detach_outlines` 拦截异作品细纲、章节及规划单元引用造成的级联，`detach_sources` 拦截异作品对待删细纲的引用；细纲同步删除空章节前复用章节删除隔离预检。异常数据返回不含他书身份的通用 409，同事务内的快照和业务变更回滚。覆盖单纲删除、替换导入、快照恢复、向导覆盖和细纲同步。
- `test_collaboration_planning_foreign_cascades.py` 用 SQLite 逐项验证上述入口和异常引用；`test_collaboration_planning_postgresql.py` 使用隔离 schema 验证导入、细纲同步、删章级联回滚与分析回写撤权/降权竞争。真实 PostgreSQL 组合 13 passed，随后场景待写 POV 新增校验单独 1 passed；SQLite 分析/回写/规划/场景组合 55 passed，新增场景及只读专项组合 17 passed，最后规划及生命周期组合 83 passed。
- 后端未配置独立类型检查器；Ruff 已通过，Python 运行时测试不等同于类型检查。
