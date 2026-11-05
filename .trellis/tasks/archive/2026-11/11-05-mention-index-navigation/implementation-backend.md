# 提及后端实施记录

日期：2026-11-05。实施代理：mention_backend_implement。未提交、未归档；产品代码已移交主会话/最终检查代理。

## 已完成

- 新增 `app/schemas/mentions.py`，与父 design 及前端 API 一致：catalog、preview、分页 mentions、summary、source window。
- 新增 `app/services/mentions/{matcher,context,sources,query,summary,window}.py`：按当前原文读取四来源，码点半开范围，区分大小写、ASCII词边界、同起点长词优先、稳定歧义候选。匹配和 hash 在线程中执行。
- 原文按游标小批次读取（知识库一份/批），列表只保留当前页片段并完整计数。跨度 LRU 上限为4096来源、总200000跨度、每来源20000跨度；超限来源完整流式扫描但不缓存，零命中可缓存。缓存不保存正文、权限或标题/章号。
- 项目人工内容权限先验证；聊天只读取项目拥有者的本人会话中的已保存 user/assistant 文本，工具载荷不计。管理员视图聊天计数恒0、catalog 不提供聊天类型，显式访问聊天来源404。
- source window 输出原文≤8000码点、绝对跨度；hash不符时 stale=true、target=null，显示新原文开头，不盲用旧位置。跨作品、已删除源和工具消息404。
- summary 先按条目类型、ID、category、q筛选再分页；只扫描当前筛选范围的章节，确定/歧义次数分离，空章不计未提及间隔；last_chapter_no/gap使用同一范围。
- `WorldSetting.aliases` 模型/API/迁移0033（依赖0032）、备份JSON注册及 APP_SCHEMA=0033。复用现有 Aliases 清洗和世界观基础编辑 metadata revision 守卫。
- 项目导入补世界观别名约束和新笔记 revision/标题/正文约束，笔记预览标签为中文。旧备份缺 aliases/笔记兼容。
- 智能体旧v2世界观快照没有aliases时，仅在当前别名仍为[]时兼容；新增别名后旧撤销仍拒绝，避免丢掉作者修改。
- 测试 fixture 清理提及缓存，跨用户总清单加入新路由与合法 preview 请求体。

## 文件范围

新文件：
- `backend/app/schemas/mentions.py`
- `backend/app/api/v1/mentions.py`
- `backend/app/services/mentions/`（7文件）
- `backend/alembic/versions/0033_world_aliases.py`
- `backend/tests/test_mentions_matcher.py`
- `backend/tests/test_mentions.py`
- `backend/tests/test_mention_compat.py`
- `backend/tests/test_mentions_scale.py`

既有文件修改：`models/entities.py`、`schemas/story.py`、`api/v1/__init__.py`、`services/agent/effects.py`、`services/project_transfer/{registry,exporter,importer}.py`、`tests/{conftest,test_projects_and_story,test_project_transfer,test_agent_api}.py`。共享注册文件在 notes_implement 明确交接后修改，保留其笔记内容。

## 验证证据

环境命令前缀：`LC_ALL=en_US.UTF-8 UV_CACHE_DIR=/tmp/ainovel-mention-uv uv run --offline`。
默认 macOS 沙箱无法读 ~/.cache/uv，默认 C.UTF-8 也会使 Alembic 中文配置按 ASCII 解码；验证使用上述可写缓存与有效UTF-8 locale，不修改应用配置。

1. TDD 首轮匹配测试因尚无模块导入失败；实现后 `pytest tests/test_mentions_matcher.py -q`：6 passed。
2. HTTP 测试首轮 catalog 路由404；接入后 `pytest tests/test_mentions.py tests/test_mentions_matcher.py -q`：16 passed（当时HTTP10项）。
3. 旧v2效果兼容用例先复现 undo_conflict，修复后与迁移/备份通过；`pytest tests/test_mention_compat.py tests/test_mentions.py tests/test_mentions_matcher.py tests/test_notes.py -q`：25 passed（当时收集数）。
4. 扩展无autoflush、线程、窗口上限后：`pytest tests/test_mentions.py tests/test_mentions_matcher.py tests/test_mention_compat.py tests/test_mentions_scale.py -q -s`：23 passed，11.73秒。
5. 相关回归：`pytest tests/test_project_transfer.py tests/test_setting_progressions.py tests/test_agent_effects.py tests/test_agent_api.py tests/test_projects_and_story.py tests/test_migrations.py tests/test_notes.py -q`：127 passed、1 skipped、1 failed，59.37秒。唯一失败是新加入 preview 的跨用户总清单未提供 text，422发生在归属校验前；补合法请求体后重新验证。
6. 加入真实 AI 保存→版本恢复、删除会话/笔记/资料、跨本人作品源ID隔离后：`pytest tests/test_projects_and_story.py::test_cross_user_isolation tests/test_mentions.py -q`：16 passed，12.41秒（HTTP15项+总隔离清单）。没有跳过或删除失败断言。
7. `ruff check .`：All checks passed。`git diff --check` 无输出。

## 性能样例

`test_mentions_scale.py` 通过真实HTTP完整扫描（返回分页1条但统计全部）：
- 500章、5000000码点正文；10份资料、500000码点；202条目；510来源。
- 正文1000次、资料100次，完整总计1100；缓存510来源/1100跨度。
- 本机 SQLite 合成语料：冷请求0.2329秒，热请求0.1100秒。
- 这是当前样例测量，不代表知识库理论上限200×300万字或高密度一字词的耗时。每次请求仍有读取/hash成本，不宣称常数时间。

## 交接与后续

- 全套后端/前端检查及真浏览器验收由主会话统一执行。相关回归中既有 PostgreSQL 条件测试因环境跳过；本轮迁移实际覆盖SQLite。
- 0033为原位ADD/DROP列，SQLite回退测试证明world子时间线及外键完整性未受影响；没有对真实用户库执行降级。
- 最终检查可接手全部本代理后端文件。本记录之后如有改动，应补充最终检查证据，不能把本记录当作最终全量测试结论。
