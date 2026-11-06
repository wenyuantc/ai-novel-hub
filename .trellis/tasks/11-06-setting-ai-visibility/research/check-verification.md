# 独立检查：结构化设定可见性

检查范围：作者配置、章节/规划、分析、非章节生成、Agent 工具与历史、检索、三家模型真实传输、备份与效果恢复。全量门禁、发布状态和真实浏览器逐项证据由主会话另外汇总。

## 路径检查

| 验收点 | 代码边界与检查证据 |
| --- | --- |
| AC1、AC7：模式、手动引用与单跳 | `setting_ai_context.build_scope` 先过滤不可发送模式，再以不可变 `direct` 展开有向边；普通引用不扩展资格。`context_builder` 与 `planning_context_service` 共用同一章初范围；`test_generation_projection_masks_builtin_and_custom_and_does_not_walk_two_hops` 使用真实手动出场与 FakeLLM 捕获，包含已允许的首跳和禁止的第二跳。分析入口的正向规划资格缺口另报主会话，不影响 `never` 拦截。 |
| AC2：字段与人工原值 | 检查 `AiScope.entry_fields` 对旧字段、共享字段、当前/起始引用值的投影，及角色、世界观、职业阶段、成员/任职两端的渲染。Agent 目录/定义先过滤再分页；详情、时间线、伏笔和分析变化不带被禁止的关联身份，确认回执与人工完整效果分离。 |
| AC3：提及 | `mentions.context` 在一次查询读取四类实体及追踪配置，组织/职业与六类世界观复用同一原文匹配。`test_tracking_configuration_reuses_nine_categories_without_turning_legacy_entries_on` 验证已启用类型和兼容旧范围。新增 Agent 竞争测试实际使用同一匹配器。 |
| AC4：兼容与格式 | `test_setting_ai_visibility.py` 断言旧规则、新默认、权限、CAS 与隐藏字段原值；`test_setting_ai_visibility_formats.py` 覆盖 v6 句柄重映射、旧 v5 SQL NULL、非法载荷拒绝、效果 v4/v5 恢复原子性。完整备份不携带 Agent 会话/消息/提案和项目运行时代次。 |
| AC5：实际出站 | 静态核对写作、分析、大纲、向导、实体生成、Agent 普通/摘要请求及设定派生向量入口；不为纯正文润色、风格试写、连通性测试、拆书原文和作者上传资料添加设定数据。MCP 是独立外部出站；其结果重入模型时仍经 Agent epoch 与模型传输门禁。剧情记忆内部检索索引可用相关元数据排名，但对模型仅返回记忆原文本身。 |
| AC6：重放与保存竞态 | `test_ai_visibility_dispatch.py` 使用 OpenAI 兼容、Anthropic、Gemini SDK 和本地 HTTP 服务检验真实发送，包含 PostgreSQL 事务锁、SQLite 写事务、成功保存前后、取消、失败与重定向；`test_agent_visibility.py` 覆盖普通请求、摘要、确认、重试、工具处理和空会话采纳当前代次。 |

## 检查发现及修复

匹配候选会竞争。把另一条目的追踪从关改开、改为忽略大小写、移除排除短语，均可能让以前唯一的检测命中变成歧义。原先这三种变更没有推进 `ai_history_epoch`，旧 Agent 工具回执仍可重放。策略写入现保守封存有实际匹配规则变化的旧上下文；追踪始终关闭、等效排除拼写、仅放宽档位/字段不触发该分支。旧世界观改分类而跨越兼容追踪范围，及 Agent 效果恢复同样处理。

新增三种真实 HTTP + FakeLLM 回归：变更前唯一命中的角色回执确实含可见哨兵；变更后历史人工可读，旧会话继续返回 409 `ai_history_sealed` 且无新模型调用；新会话尝试同一词时不再自动取得该角色。修复前 3 例均在 `history_sealed` 断言处失败。另测 v5 效果撤销恢复旧世界观追踪会推进 epoch。

## 负控制

用独立 pytest 进程的临时 autouse fixture 逐项 `monkeypatch`；不修改工作树实现，不用真实厂商或作品。三个实验均按预期返回测试失败：

1. 把 `DispatchHooks._before_request` 替换为异步 no-op → `test_author_save_is_atomic_against_real_send_window` 实际收到 PATCH 200，而契约要求响应头仍阻塞时 409 `ai_visibility_busy`。
2. 将 `never` 暂加入 `_SENDABLE_MODES` → `test_generation_projection_masks_builtin_and_custom_and_does_not_walk_two_hops` 在第 21 章实际发往 FakeLLM 的手动出场和引用中检测到「档案禁用者」。
3. 在 Agent 角色读取边界临时把 `entry_payload` 换成完整 `serialized` → `test_agent_scope_filters_directory_fields_links_and_hidden_tool_writes` 捕获下一轮请求里出现本应隐藏的 `background`。

## 本轮实跑

- `uv run ruff check .`、前端 `npm run lint`、`tsc -p tsconfig.app.json` / `tsc -p tsconfig.node.json`：通过。
- 独立专项：`test_agent_visibility.py`、`test_setting_ai_visibility.py`、`test_setting_ai_visibility_formats.py`、`test_ai_visibility_dispatch.py` 合计 43 通过（包含隔离 PostgreSQL，零跳过）；随后新增效果撤销测试 1 通过。
- 模型替身和环回 HTTP 仅验证本应用的受控请求，不声称回收已经发出的数据、净化作者自由文本或阻止独立第三方访问。
