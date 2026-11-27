# 短剧插件逐项验收

日期为 2026 年 11 月 28 日。后端工作流测试使用真实 HTTP、SSE、任务引擎和数据库，外部模型由可重复的测试响应代替。浏览器与本地开源模型的结果另行记录。

| PRD 验收项 | 检查路径及观察结果 |
| --- | --- |
| 1. 启停和数据保留 | 管理员启用后可建剧，公开配置与作品导航显示短剧。停用时接口返回 403，重新启用后可读到原剧。见 `test_drama_registration.py`、前端 `drama.test.ts`。 |
| 2. 章节快照 | 用连续两章建集，核对原文的正文与顺序。改章或删除来源章节后仍保留原文。跨书章节与不连续选择被拒绝。见 `test_drama_episodes.py` 和 `test_drama_workflow.py`。 |
| 3. 改写、取消和手改 | 从浏览器看到逐段输出，取消后仍保留已保存剧本。改写成功后手动修改，刷新读取到保存内容。并发手改后生成结果作为草稿，需要明确采纳。见 `test_drama_task_guards.py`、`test_drama_tasks.py`。 |
| 4. 资产和小说设定 | 提取角色、造型、地点和道具。只在本书匹配姓名或别名，重名保留未关联。带入外貌，在短剧改名及删除小说设定后仍可读取短剧资料。见 `test_drama_assets.py`、`test_drama_setting_deletion.py`。 |
| 5. 分镜 | 用剧本生成含画面、景别、时长和素材引用的有序分镜。验证逐条编辑、拖放、上下移动、新增、删除，以及重新拆分的二次确认。过期整集排序返回 409。见 `test_drama_episodes.py`、前端 `shots.test.ts` 和浏览器走查。 |
| 6. 提示词 | 给造型、场景、道具和分镜生成提示词，通过读取接口核对写入目标。资产与镜头编辑器允许手改；镜头生成使用实际引用的素材。见 `test_drama_workflow.py`、`test_drama_prompts.py` 和 `test_drama_tasks.py`。 |
| 7. 权限和用量 | 查看者可以读取，修改和 AI 操作被拒。编辑者借用作者模型需授权，用量页能查到该次调用。运行中撤权、取消和停用均保留原稿。见 `test_drama_task_guards.py`、`test_drama_workflow.py`。 |
| 8. 文档导出 | 下载 Markdown 和 DOCX，核对剧本、依序的镜头文字及素材引用；Word 使用原生表格。见 `test_drama_episodes.py`、`test_drama_workflow.py`。 |
| 9. 作品备份 | 两个独立 SQLite 数据库，目标库先占用旧 ID。经 HTTP 预览、导入，核对来源章节、小说设定、素材、分镜、提示词与新 ID。v18 导入为空短剧资料。另用 SQLite 和 PostgreSQL 16 验证迁移和原生备份。见 `test_drama_lifecycle.py`、`test_drama_transfer.py`。 |
| 10. 作品彻底删除 | 正式回收站入口将作品移入回收站后，短剧接口返回 404。恢复后数据一致，管理员彻底删除后，逐表查询确认 13 张表均无该作品资料。见 `test_drama_lifecycle.py`。 |

## 完整门禁

| 范围 | 最终结果 |
| --- | --- |
| 后端全量 | `uv run pytest -n 4 --dist loadfile -v --tb=short -ra --durations=20` 退出码 0。8,067 项中 7,902 通过，146 条件跳过，19 预期失败，21 条警告，用时 41 分 54 秒。全库 Ruff 通过 |
| 跳过及预期失败 | 默认 SQLite 环境未提供 PostgreSQL 测试 URL，142 项跳过；另 4 项需有创建数据库权限的 PostgreSQL 备份地址。预期失败为项目已有的平台 Key 请求身份 11 项、自动 MCP 发送 7 项、旧 JWT 与 SQLite ID 复用 1 项 |
| 短剧数据库专项 | 使用隔离 PostgreSQL 16 和 SQLite 验证迁移升降、原生备份、跨书删除保护、重映射和并发修改，共 11 项通过，无跳过 |
| 最终提示词 | 全量启动后更新了模型消息和提示词。对最终文件重新运行提示词、任务、授权和端到端工作流，29 项通过 |
| 前端 | `npm test` 298 个测试文件、2,013 项通过。生产构建通过。最终全仓 ESLint 零错误、零警告 |
| 浏览器及真实模型 | [Chrome 走查](browser-verification.md)覆盖真实页面、受控模型、协作权限、取消、手机宽度和两种主题。另用本地 Qwen2.5-7B 跑通四类任务并核对数据库与计量，[真实模型记录](live-model-verification.md)保留了对白归属、空镜人物和时间段重复等生成质量问题 |
