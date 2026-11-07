# 首轮完整后端门禁：旧人工入口测试适配

## 本轮变更边界

- 依据主会话首轮完整后端结果（18 failed / 2640 passed）及 `backend-full.log`，只处理分配给 E2 的五个既有测试文件、六个失败。
- 行为落点已经由场景权威存储、结构版本与公共位置 schema 实现。本轮不改产品逻辑，只让旧人工调用显式遵守这些合同；不放宽 API、不删安全断言、不运行全量测试。
- `test_admin_audit_logs.py`：清空正文的审计必须关联真实新建的空正文结构版本；核对版本、事件身份与隐私，保留原来的 changed / missing 及删除后快照断言。另一个结构版本恢复请求显式读取当前章修订号并提交 CAS。
- `test_admin_projects.py`、`test_mentions.py`：结构版本恢复前 GET 最新章，显式传 `expected_content_revision`，保留管理员权限、原文与提及重算断言；不改真实旧纯文本无 body 兼容测试。
- `test_context_builder.py`：只在实际调用整章生成的 ORM 目标章种子处显式初始化场景，保留全部时间投影、后期资料隔离和只读 ORM 断言。
- `test_setting_context_preview.py`：实际日志显示失败仅因章级 `position` 新增 `scene_id: null` / `scene_order: null`。保持纯章初只读种子及 no-autoflush 未提交修改屏障不变，严格补齐完整 JSON 断言；不为统一形式给只读种子补场景。
- 不改 AI owner 的测试、产品源码、迁移、依赖、README / TODO / spec，不提交、不 finish / archive。`test_planning_context.py` 的旧 E501 已由 AI owner 修复，按主会话指示不重复写入。

## 验证记录

在 `backend/` 执行：

```text
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest \
  tests/test_admin_audit_logs.py tests/test_admin_projects.py \
  tests/test_context_builder.py tests/test_mentions.py \
  tests/test_setting_context_preview.py -q --tb=short
```

结果：**79 passed in 54.92s**，退出码 0；五个原文件完整运行，未筛掉其余原用例，零失败、零跳过。

```text
uv run ruff check tests/test_admin_audit_logs.py tests/test_admin_projects.py \
  tests/test_context_builder.py tests/test_mentions.py tests/test_setting_context_preview.py
```

结果：**All checks passed!**。这五个测试文件的 `git diff --check` 无输出、退出码 0；本新增报告另行通过 `git diff --no-index --check /dev/null <报告路径>` 检查。

## 保留的保护与交接

- 审计清空用例现在精确核对新版本身份、递增 seq、空正文 / 字数、单场景结构快照、完整版本关联（含空正文 SHA-256）、真实 actor / owner、事件类型及隐私。原版合并导致 `changed`、删项目后 `missing`、删除用户后 actor / owner / 项目标题快照断言均未删除。
- 三个新结构版本恢复请求均读取当前章 `content_revision` 后显式提交；原角色、管理员人工内容、提及数量从 4 到 1 再到 4 的断言保持。未修改旧纯文本恢复兼容测试或 API。
- 场景初始化仅新增于 `test_chapter_and_analysis_use_story_time_settings` 的 `ch5` ORM 写种子，此章随后真正被整章生成消费；同函数另外两个只读章及 no-autoflush 专项测试未统一补写。
- 设定预览仍严格核对原章初 / 章末隔离、零 AI / embedding 调用和零数据库写入；未提交作品 / 章名仍留在 dirty 集合，数据库原值不变。只补充章级位置两个必然为空的场景字段。
- 本轮没有发现需改产品的回归。未改本轮所有权外文件，未运行完整后端 / 前端 / 浏览器或新增 PostgreSQL 检查；最终完整门禁由主会话统一执行。本轮测试文件已冻结，可交接。
