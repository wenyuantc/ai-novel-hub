# 协作批注实施计划

任务状态为规划中。以下检查项在开始实现后执行。

| 顺序 | 工作 | 完成证据 |
| --- | --- | --- |
| 1 | 基于实际迁移 head 建立讨论和留言表，登记章节删除规则，保留稳定回复顺序及可迁移的历史署名。 | 旧库升级后原书正常、批注为空，有讨论时降级保护生效。 |
| 2 | 实现独立的批注权限、章节列表、建讨论、回复和处理接口。对话写入只影响批注表。 | API 覆盖作者、查看者、编辑者、待接受、已撤权、管理权限和外部账号。交错执行撤权与回复、并发回复和网络重试，核对最终记录（A1、A4、A5）。 |
| 3 | 在正式正文写入路径接入选区校验、可靠平移和失效处理。逐项检查人工保存、AI、批量修订、历史恢复、场景拆并和移章。 | 用含重复句和表情的样章验证；覆盖选区前插入、覆盖原句、场景退场和仅修改另一景。正文操作回滚时批注位置一起回滚（A2、A3）。 |
| 4 | 在现有章节工作台加入选区批注、讨论列表、回复、作者处理和有效原句跳转；接入只读编辑器、窄屏和身份切换。 | 查看者经过章节“查看”入口完成讨论；已处理与位置失效均可看见。选区过期保留留言草稿，撤权后旧响应无法重新展示（A1–A5、A7）。 |
| 5 | 两表接入单书 JSON 核心组，更新格式版本、估算、预览、快照一致性和导入重映射。同时验证原生整库恢复。 | 关闭可选资料仍能往返所有讨论；跨账号、同名账号、场景移章、原位置失效、备份期间新增回复或处理、旧格式及异常引用均有覆盖。SQLite、PostgreSQL 整库分别核对批注与成员关系（A8、A9）。 |
| 6 | 用同一组批注样本检查阅读、各书稿格式、实际模型出站和两类备份。同步 README 和相关 Trellis 规范。 | 阅读、书稿与模型请求中没有测试短语，两种备份恢复后均可读到完整留言及回复（A6、A8、A9）。 |

## 验证安排

- 后端先执行新批注用例，并回归 `cd backend && uv run pytest tests/test_collaboration_content_access.py tests/test_rich_document_mapping.py tests/test_reader.py tests/test_document_export.py tests/test_rich_document_ai_context.py tests/test_project_transfer.py tests/test_project_transfer_privacy.py tests/test_backup_sqlite_restore.py`。在隔离的 PostgreSQL 测试库串行跑撤权、正文保存和回复的并发用例，并在隔离库完成一次真实整库导出与还原。
- 后端全量门禁为 `cd backend && uv run ruff check . && uv run pytest -n 4 --dist loadfile --durations=20`（默认 SQLite）。配置 PostgreSQL 时改用 `-n 0`。
- 前端执行 `cd frontend && npm run lint && npm test && npm run build`。用浏览器以作者和查看者两个会话走 A1、A2、A5、A7；在 375px 窄屏重复从选句到读回复的流程。
- 交付时记录实施时的迁移版本、实际执行命令、浏览器结果和仍未验证的项，再更新 TODO #28 的复选框。
