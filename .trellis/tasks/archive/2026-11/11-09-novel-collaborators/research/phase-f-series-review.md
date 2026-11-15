# 阶段 F 独立审查：系列和作品响应

2026-11-10 在本机 `collab_test` 隔离数据库（PostgreSQL 16.15、随机 schema）执行 `AINOVEL_COLLAB_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_collaboration_series_read_race_postgresql.py --tb=short`：**14 passed in 68.53s**，验证作品及系列读取中的撤权交错。

随后使用独立 `collab_test` 和 `series_verify` 测试库运行 `tests/test_collaboration_series_postgresql.py tests/test_series_postgresql.py`：**8 passed in 33.72s**，覆盖系列写入锁和原有系列引用竞争。最终全后端及前端回归仍待运行。

## 已修复

- 系列详情、条目详情／列表原先先计算可见成员书和引用，后续只重验系列权限。实际双客户端 HTTP 反例确认，单书撤权提交后旧响应仍包含该书标题、ID 或引用；影响预览同样可能返回已失权书的影响。现在输出前以一条最终 SQL 同快照核对真实账号、RBAC 代次、系列邀请轮次、系列版本和涉及作品的当前权限：详情／条目过滤失权书，影响预览对缺失逐书 editor 授权返回匿名 `series_book_edit_required`。作者保留原有作品与回收站语义。最终 SQL 之后不再等待数据库。
- 书内角色／世界观列表和详情原先在第一次系列可见性检查后，仍可能于返回前经历单书或系列撤权。现在最终同一 SQL 同时核对当前作品、账号和系列身份：失去作品权限返回 404，失去系列权限的纯单书协作者不再得到系列来源 ID；管理员已有匿名系列来源维持原行为。
- 作品书架最初查询后还读取统计与进度，作品或系列授权在此期间变化时会把旧权限和旧系列摘要发给客户端；作品详情也可能在输出投影期间撤权后返回旧正文。现在最后一个批量 SQL 同快照重验账号、作品身份与生命周期、真实协作能力及系列摘要。书架不增加管理员旁路、不逐作品查询；旧邀请轮次或能力变化时不输出旧书架条目。详情失权返回 404，仍可读但原能力／邀请轮次变化返回 `stale_project_access` 409；管理员撤掉协作后仍可通过独立管理权限重新读取。投影查询在 `no_autoflush` 下运行，保留调用者待写对象，不更改数据库。
- 系列列表此前在初次认证与最终查询之间不能识别令牌撤销；现在最终系列查询同时读取当前账号状态和原 token version，令牌失效时返回 401。系列身份、成员和列表仍在同一查询中判断。

## 真实 SQLite HTTP 与定向验证

新增 `tests/test_collaboration_series_read_race.py`：两个真实客户端在详情、条目、列表、影响预览、书内设定和书架读取的中途撤销系列或单书授权；另覆盖作品降级、撤权后重邀、原管理员和事务内待写标题。首次执行前已分别复现系列详情泄露、书内系列来源泄露、作品详情撤权后 200 和书架系列摘要泄露的红灯。

已执行（在 `backend/` 下，均使用隔离测试数据库）：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_series_read_race.py tests/test_series_authorization_version.py
# 18 passed；此后新增并单独验证了待写数据、管理员与系列 token 边界。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_series_read_race.py::test_project_projection_does_not_flush_or_discard_pending_fields \
  tests/test_collaboration_series_read_race.py::test_project_projection_retains_independent_admin_access_after_revoke \
  tests/test_collaboration_series.py::test_series_entry_page_batches_hidden_use_projection
# 3 passed

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_invites.py::test_http_project_responses_use_the_actual_actor \
  tests/test_collaboration_invites.py::test_project_invitation_revision_lifecycle_and_access \
  tests/test_collaboration_series.py::test_series_editor_requires_book_editor_for_ownership_changes \
  tests/test_collaboration_series.py::test_recycled_use_and_admin_cannot_bypass_series_or_book_rights \
  tests/test_collaboration_series.py::test_hidden_book_propagation_conflict_does_not_disclose_dependency \
  tests/test_series.py::test_promote_keeps_original_id_and_basis_then_reprojects_each_book
# 6 passed

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_series_read_race.py::test_series_list_checks_token_version_in_final_query \
  tests/test_series_authorization_version.py \
  tests/test_collaboration_series.py::test_recycled_use_and_admin_cannot_bypass_series_or_book_rights
# 7 passed；在系列列表最终 token 检查后执行。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_agent_tools.py::test_project_update_allowlist \
  tests/test_agent_tools.py::test_read_tools_and_chapter_pagination \
  tests/test_collaboration_style_privacy.py::test_project_view_hides_changed_style_binding_during_projection
# 3 passed

uv run ruff check app/services/project_service.py app/services/series_service.py \
  app/services/series_read.py app/services/series_policy.py app/services/series_authorization.py \
  app/schemas/series.py tests/test_collaboration_series_read_race.py \
  tests/test_collaboration_series_read_race_postgresql.py
# All checks passed!
```

项目未配置独立 Python 类型检查器；不能将 Ruff 或测试称为类型检查。上述各组均是定向证据，部分组早于后续无行为变更的代码整理或系列 token 补强，不代替最终全量门禁。

## 待验证与限制

- 新增 `tests/test_collaboration_series_read_race_postgresql.py` 的 **14** 个 PostgreSQL 用例，覆盖两个应用实例下作品与系列撤权、降级、重邀和令牌撤销；已收集，按主任务错峰安排**尚未执行**。可在独立本机测试库按以下命令运行：

  ```sh
  LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='<隔离 PostgreSQL URL>' \
    uv run pytest -q tests/test_collaboration_series_read_race_postgresql.py
  ```

- 本次未重新运行完整系列 PostgreSQL 锁竞争、全后端／前端、迁移或浏览器门禁。`read_book_settings` 和系列最终复核都按所需的少量批量查询执行，但包含成员作品的 SQL 结果规模随系列内书籍数量增加，未进行大系列压测。
- 本次只核对分配的系列和作品响应，其他章节、导出及 AI 路径是否有同类跨异步读取竞态不在此审查结论内；系列 Gate F 仍待完整独立验证，不可单独部署。
