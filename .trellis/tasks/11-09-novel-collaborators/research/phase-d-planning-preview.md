# 规划上下文预览的协作读取

## 实现范围

- `GET /projects/{project_id}/planning/units/context-preview` 明确走 `manual_context_preview`：作品作者、已接受的 viewer/editor 和具备原 `projects.read` 能力的管理员均可调用；非成员仍为 404，管理员与协作身份重叠时保留各自来源的读取能力。内部 `context_preview(db, project, ...)` 保留原调用方负责授权的合同。
- 该预览经 `load_planning_context`、`chapter_task`、`prepare_chapter_settings`、`build_scope` 和规划文本格式化完成，均为本地查询与渲染，不调用模型、embedding、知识库／记忆检索或后台任务，也不调用会读默认写作风格的 `project_info`。仍按现有 AI 可见性规则过滤文本；角色、世界观、组织、职业、关系、章节、场景、细纲和支线均受作品条件约束。
- 构建完成后重新读取认证和作品能力。撤权后没有独立管理读取权限返回 404；仍为普通协作者但邀请 revision／UUID 已变化返回 409 `collaboration_changed`，不将新一轮邀请当成旧请求的授权。仍具备独立 `projects.read` 能力的管理员可按该来源读取。之后再次核对规划、字段、AI 可见性版本；场景来源还核对同作品活动场景及章节内容版本，变化返回安全冲突。已发生的历史读取不能因后续撤权而撤回；返回前复核不是对未来操作的授权。
- 章节或规划单元的异常数据可能把 `plan_id` 指向他书细纲。公共预览投影现在仅返回本书细纲及其所属本书大纲；已建章时还要求细纲与章节双向绑定且大纲一致。异常引用只投影为 null，不修改原始数据。查询使用 `no_autoflush`，不把调用方未提交的普通 ORM 变更写入事务。

## 验证

在 `backend/` 下运行，数据库与数据均为测试专用：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_planning_preview.py --tb=short
# 最初 12 passed in 28.28s；随后新增的实际 HTTP 撤权与重新邀请两项单独 2 passed in 4.11s。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_planning_access.py tests/test_scene_progressions.py::test_scene_http_boundaries_effective_and_previews_use_same_position tests/test_planning_context.py::test_context_does_not_pair_stale_data_with_new_revision --tb=short
# 3 passed in 5.15s

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  AINOVEL_COLLAB_PLANNING_PREVIEW_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_planning' \
  uv run pytest -q tests/test_collaboration_planning_preview_postgresql.py
# 2 passed in 9.12s；PostgreSQL 16.15，两个应用和随机 schema；由主代理执行。

uv run ruff check app/api/v1/planning.py app/services/planning_context_service.py tests/test_collaboration_planning_preview.py tests/test_collaboration_planning_preview_postgresql.py
# All checks passed!
```

SQLite HTTP 回归覆盖作者、viewer、editor、只读管理员、管理员兼协作者、非成员、未登录、四种来源的跨书 ID、异常细纲投影、合法细纲、未提交对象、无 SQL 写入与无模型／向量调用，以及渲染后版本、令牌和成员状态变化。实际撤权／重邀两客户端使用真实 HTTP 操作。PostgreSQL 以跨连接屏障覆盖章／景预览的版本改变、降级、重新邀请、令牌撤销和撤权；该用例完成后本切片产品代码和 PG 测试未再修改。

本记录仅覆盖本地规划预览，不代表章节向量检索预览、任何协作者 AI 发送、阶段 D 或整体验收已完成。
