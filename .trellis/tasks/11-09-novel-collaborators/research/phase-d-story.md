# 阶段 D：人工故事、实体与设定路径

## 授权和调用边界

- `story` 世界观、角色、大纲的人工 CRUD 与列表/详情使用 `content.read` 或 `content.edit`；具体实体查询限定项目。`get_owned`、`get_manageable` 保持旧边界，内部调用默认仍严格按原作者或管理员规则。
- 职业及阶段、角色职业挂接/历史、组织及成员、关系类型/关系/历史/图谱、伏笔及统计/历史/快捷操作、设定时间线/基础值/变化/自定义文本字段等人工路由显式传 `manual=True`。服务默认值为旧权限；写入取得项目父行锁并在锁后重验 `content.edit`。
- 章节出场角色和伏笔提醒分别在明确人工入口使用 `get_manual_chapter` 的读/写能力，章节和引用角色均限定在父作品内。
- 作品设定目录、字段定义/值、分类、标签的人工路由按内容读写；历史组件与 Agent 共用的读取及字段 `_editing` 均默认旧权限。带 AI 可见性变更的字段定义、旧字段可见性及旧字段绑定保留原权限，移交阶段 E。
- 角色旧 `always_in_context` 和 `role_type` 的人工修改按内容编辑能力，相关写入获取发送写门禁与父行锁；原有 legacy 生效判断和可见性版本失效逻辑保持。人工新建角色也取发送写门禁，内部调用默认行为不变。
- `series_read.visible_series_ids` 仅允许系列作者或 accepted 系列成员看到系列 ID。只获单书权限时返回匿名 book origin；原管理员人工旁路及管理员兼单书协作者继续看到匿名 `scope=series`，但无系列 ID 与系列编辑能力。角色/世界观直接修改、删除以及时间线共享基础冲突显式传入真实 actor；纯单书协作者无系列权限时得到不含系列身份的通用 409，作者、系列成员及原管理员保持原响应。
- `content.edit` 锁通过 `project_access.require_project(..., for_update=True)` 完成，不能从 HTTP 方法或 `lock=True` 反推能力。涉及发送策略的写使用 `visibility_write=True`。

## 留待后续阶段

- 卡片导出携带 `ai_settings`、隐藏字段与策略；导入预览含系列来源冲突提示；v6 导入修改 AI 可见性与关联，旧版本可影响 legacy 可见性。三条卡片路径保留原作者限制，阶段 E/F 完成策略投影、匿名冲突及事务后再迁移。
- 职业批量、关系批量、带关系的新角色、带成员的组织四个组合保存入口目前保持旧权限；它们被现有代码标记为 AI 预览确认，应由阶段 E 逐条确认其人工能力与 AI 来源绑定后接入。
- `setting_ai_visibility` 的独立 GET/PATCH、字段 `ai_hidden` 变更及旧字段绑定/可见性暂保留旧权限。`wizard`、系列、AI/任务、个人资源及跨书操作均未通过新人工 helper 扩权。
- 新项目能力、章节/场景、规划/知识库、分析与任务路径分别由其他阶段负责，本记录不代表其全部完成。

## 验证

- `tests/test_collaboration_story_access.py`：真实 HTTP 测试角色矩阵、跨作品角色/时间线/伏笔/职业/组织/成员和章节出场角色 ID，默认内部调用边界、管理员与 viewer 角色并集、单书/系列协作投影和冲突脱敏。
- `tests/test_collaboration_story_postgresql.py`：PostgreSQL 独立 schema 中，让另一事务持项目父行锁并先撤权或降级，验证人工角色、时间线、伏笔、职业、组织写实际等待项目行、锁后返回 404/403 且无业务写入；旧 AI 注入策略另做同类竞争。
- `ruff check`（本阶段所有改动的服务、路由和测试）及 `git diff --check` 通过。
- `pytest -q tests/test_collaboration_story_access.py tests/test_setting_progressions.py tests/test_foreshadows.py tests/test_careers.py tests/test_organizations.py tests/test_relationships.py tests/test_cards.py`：110 passed（之后字段/分类补充显式人工参数，相关历史测试又以聚焦组合复验）。
- 角色创建发送门禁变更后，`tests/test_collaboration_story_access.py`、`tests/test_setting_ai_visibility.py`、管理员人工设定回归合计 15 passed。
- 最后字段/分类默认权限调整后，`tests/test_collaboration_story_access.py` 加字段定义、管理员审计、字段 Agent、分类 CRUD/跨项目快照、分类 Agent 和系列投影保护共 16 passed。
- 专用 PostgreSQL 项目行锁竞争 10 passed；补充 legacy AI 可见性撤销/降级竞争 2 passed。
- 两次更宽的重复组合测试在未报告断言失败时主动中断，未完成，因此不计为通过；上述聚焦组合已完成。中断时 pytest 异步 fixture 清理产生 KeyboardInterrupt/pytest stash 的附加栈，不作为产品测试失败。
- 独立复核的分类 PostgreSQL 快照四个竞争参数和分类 Agent PostgreSQL 用例，随后在专用 `fields_verify` 隔离库补跑共 5 passed，不再将该组作为跳过项。
