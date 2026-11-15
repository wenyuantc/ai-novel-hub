# 阶段 E：提示词风格身份复核

## 调用链与边界

当前 `project_info(db, project, *, actor_user_id)`、`default_description(db, project, *, actor_user_id)` 已强制传真实 actor，缺失不回退为作者。`visible_default_style` 只允许风格 owner 或全局预设。没有修改既有生产实现，也没有开放协作者 AI。

- 章节生成/建议改写：`Task.user_id → ChapterJob.user_id → build_chapter_context(actor_user_id)`；润色使用单独的设定上下文，不调用作品风格描述。
- 向导、实体、大纲、分析：各自 handler 使用 `Task.user_id` 构造 Job，Job 构造使用同一 `user_id` 调用 `project_info`。
- Agent 的系统提示词使用 `conversation.user_id`；拆书的摘要与综合提示词使用 `job.user_id`，该身份来自任务 `user_id`。
- 封面默认提示词也使用真实 actor 的 `visible_default_style`。现有测试覆盖真实作者任务的 FakeLLM 请求，以及服务层模拟协作 Job；这不表示协作者 AI 已开放。

## 负控制和回归

新增 `tests/test_collaboration_style_leak_regression.py`。真实 HTTP 接受 editor 邀请之后，使用章节上下文构造器执行负控制：临时模拟旧的作者身份回退，确认会将作者私人风格描述放入协作者上下文；恢复真实实现后，确认协作者看不到作者风格，本人能看到自己的风格，全局预设可见，缺失或无效 actor 抛错。测试不发送模型或 embedding 请求。

在 `backend/` 执行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_style_leak_regression.py tests/test_collaboration_style_privacy.py::test_project_info_requires_real_actor_and_only_reads_visible_default tests/test_collaboration_style_privacy.py::test_chapter_context_uses_real_actor_for_project_default 'tests/test_context_builder.py::test_context_read_does_not_flush_unrelated_changes[chapter]' --tb=short
# 4 passed in 5.34s

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check app/services/context_builder.py tests/test_collaboration_style_leak_regression.py
# All checks passed!
```

后端未配置独立 Python 类型检查器。本次仅验证 SQLite 与本地构造，未执行 PostgreSQL，也不将直接构造当作真实协作 AI 发送证据。

## 后续集成触点

`agent/tools/read.py:overview` 与 `agent/tools/write.py` 会将 `project_out` 直接作为工具结果返回；人工项目投影为 `projects.read` 管理员保留对作者风格的代管可见性。当前 Agent 服务仍严格作者，因此该分支不是现已开放的协作者泄漏；未来开放管理员兼协作者的 Agent 时，须对工具输入／回显另按真实 actor 过滤私人风格，不能直接复用人工管理员风格投影。
