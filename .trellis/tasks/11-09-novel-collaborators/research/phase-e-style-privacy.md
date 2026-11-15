# 阶段 E：项目风格提示词与投影基础

## 实施范围

- `project_service.project_info` 和 `style_service.default_description` 强制传入 `actor_user_id`；默认风格仅在真实 actor 本人或全局预设可见时进入提示词。新 `visible_default_style` 在 `no_autoflush` 下查询并拒绝无效 actor。
- 章节上下文、向导、实体、大纲、分析、Agent 会话和拆书任务的调用点分别透传真实请求用户、任务用户或会话用户。原严格作者 AI 服务入口未改动，协作者 AI 任务、模型发送及授权轮次仍须后续接入。
- 封面默认提示词不再读取任意绑定的 `WritingStyle`；绑定对 actor 不可见时隐藏名称、描述和基调，并返回通用 `warning`，字段始终为字符串或 null。前端封面弹窗由前端代理接入显示。
- 项目列表和单项/写响应复用统一投影判定：本人及预设可见；管理员保留对该作品作者风格的原代管能力；异常绑定第三人风格、缺失风格或投影中途绑定变更时将风格及 ID 置 null。该投影不会修改持久化的原绑定。
- 同步更新 `prompts-styles.md` 的实际签名与可见性；未放宽原作者 helper，不改变项目备份或用户私有资源权限。

## 验证

下列命令均在 `backend/` 执行，数据库为测试自建 SQLite，模型请求仅由项目内 FakeLLM 接收：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_style_privacy.py \
  tests/test_styles.py::test_wizard_and_entity_use_description_only \
  tests/test_styles.py::test_legacy_style_field_creates_original_style \
  tests/test_project_cover.py::test_cover_prompt_and_generate_preview_only \
  'tests/test_context_builder.py::test_context_read_does_not_flush_unrelated_changes[chapter]' \
  tests/test_agent_context.py::test_broken_override_keeps_safety_in_system_and_project_data_untrusted --tb=short
# 10 passed；执行时该测试文件含 5 项，后面两项项目投影回归分别执行。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_style_privacy.py::test_project_views_filter_foreign_style_without_overwriting_binding --tb=short
# 1 passed；真实 HTTP 验证作者、编辑者、管理员与管理员兼编辑者的详情、书架、修改和封面响应。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_style_privacy.py::test_project_view_hides_changed_style_binding_during_projection --tb=short
# 1 passed；模拟同一响应投影期间绑定变化，不代表跨连接竞争测试。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_invites.py::test_pending_changes_invalidate_old_revision_and_private_style_is_redacted \
  tests/test_admin_projects.py::test_admin_style_references_and_generating_lock_preserved --tb=short
# 2 passed。
```

本范围 Ruff 与 `git diff --check` 通过；`analysis_service.py` 的单行 actor 透传已通过首组回归，随后按协调释放给规划复核代理，不重复修改。

## 边界

- 章节上下文和 Agent 等服务层用例通过直接传 actor 构造场景，部分 Job 构造测试仅在测试中替换旧严格作者获取函数，以验证尚未开放的协作调用链；不是协作 AI 已开放或真实协作发送通过的证据。
- FakeLLM 实际捕获作者路径上的向导、实体、章节生成、分析和大纲请求，验证损坏的他人风格不会进入消息，并验证作者本人风格仍进入消息；未调用外部服务。
- 封面已返回不可继承的通用警示；其他 AI 入口将来对协作者开放时仍需在其任务／界面上提供明确的不继承提示，不能把空风格误描述为已继承。
- 章节 `manual_chapter_views` 的异常第三人 `style_id` 投影已告知主代理，需要在章节范围另行处理。PostgreSQL 跨连接竞争和全部后端回归由主代理错峰执行。
