# 笔名实施与验证计划

## 启动门禁

- [x] 用户在收到完整规划摘要后明确批准；总任务与本子任务文档、context manifests 均有效。
- [x] 重新核对分支、工作区，在隔离分支实施；实施者复核当前 Alembic head并保留其他工作。
- [x] 主会话运行本子任务 `task.py start`，然后以 `Active task: .../11-16-pen-name-groups` 派发 `trellis-implement`；完成后使用 `trellis-check`。

- [x] 实施／检查代理已按父任务 `research/implementation-context.md` 加载本领域研究和适用规范；长文分段读取。

## 有序实施

1. [x] 新增两表及迁移、schema、无提交的本人笔名／作品关联原语；加入真实数据库同作者和删除约束测试。
2. [x] 完成私库 CRUD、CAS 和 owner-only 项目写入口；接入普通创建及最终 ProjectOut 批量投影。
3. [x] 补齐备份 v10 的署名字段、旧格式兼容、同事务恢复、registry 与 APP_SCHEMA；接通站内和开放 EPUB 默认署名。
4. [x] 接入书库管理／筛选、作者选择器、书架／概览署名和身份代次保护；保留未保存表单。
5. [x] 完成定向 HTTP、数据库迁移、PostgreSQL 竞争和真实浏览器检查；修复本任务问题，不改变既有权限语义。
6. [x] 运行本子任务质量门禁，交 `trellis-check` 复查 PN-AC1–PN-AC7；结果写 `verification.md`。
7. [x] 同步真实功能 README 与相应 spec，最终组合验证后勾选 TODOS 对应子项并交付下一子任务。
8. [ ] 按项目收口流程，在确认提交后归档和记录。

## 验证命令

定向测试及真实执行结果见 `verification.md`；最终集成门禁覆盖后三项共同改动。

```sh
cd backend
uv run pytest tests/test_pen_names.py tests/test_pen_name_migration.py tests/test_pen_names_postgresql.py
uv run pytest tests/test_project_transfer.py tests/test_epub.py tests/test_open_api.py tests/test_project_lifecycle.py tests/test_projects_and_story.py tests/test_inspiration.py tests/test_book_import.py tests/test_admin.py tests/test_migrations.py
uv run ruff check .
uv run pytest
```

```sh
cd frontend
npm test -- src/api/penNames.test.ts src/utils/shelf.test.ts src/components/shelf/LibraryBookCard.test.ts src/pages/ProjectListPage.test.ts
npm run lint
npm run build
npm test
```

PG 使用项目守卫允许的本机隔离测试库和 `create_isolated_schema`。定向验证改名冲突、同名创建、删除与绑定／恢复双向竞争、回收与关联、账号停用及撤权与读取；缺环境只记未执行，不能用 SQLite 或 skip 充当通过。若 locale 导致中文配置读取失败，按规范在该命令设 UTF-8 locale，不改配置绕过。

真实浏览器：作者和协作者分别完成读写边界、新建／改名／删除、筛选清除、概览未保存草稿、账号切换；桌面与 375px。用隔离测试账号和数据。

## 风险文件、检查点与回滚

- `project_service.py`、ProjectOut/types、书架、importer/exporter、开放 EPUB 是跨入口风险点；只完成一个入口不算交付。
- 新增表后先验证迁移；权限接口后验证跨用户；备份／导出后验证旧格式及两个 EPUB；UI 后做真实浏览器，再全量门禁一次。
- 有笔名数据不得直接降级删表。业务事务失败必须回滚，无需靠补偿删除作品。
- 不重复跑已通过的全量测试，除非后续改动或发现问题需要；下一子任务共享文件改动后由最终集成门禁再验证。
- 提交前新增文件和已有文件均检查空白；按授权范围暂存后运行 `git diff --cached --check` 并确认退出码。
