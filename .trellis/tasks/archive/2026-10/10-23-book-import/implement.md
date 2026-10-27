# 执行计划：拆书导入

## 开工前

- `ls backend/alembic/versions`：确认迁移编号。当前最新为 `0016_reading_progress`，本任务计划用 `0017_book_imports`。
- `git status`：确认 `.trellis/tasks/10-23-project-cover/` 属于另一个会话，不要动。
- 阅读 spec：
  - `backend/batch-generate.md`：断点、暂停、续跑的契约
  - `backend/error-handling.md`：任务契约
  - `backend/retrieval.md`：`kb_parser` 与上传限制
  - `backend/prompts-styles.md`：模板注册与覆盖
  - `backend/story-entities.md`：实体写入与检索单元维护
  - `backend/outline.md`：大纲与章节号
  - `backend/project-transfer.md`：表规则注册
  - `backend/database-guidelines.md`
  - `backend/quality-guidelines.md`
  - `frontend/component-guidelines.md`
  - `frontend/type-safety.md`
  - `frontend/quality-guidelines.md`
- 先读这些代码，确认字段名：
  - `Character`、`Organization`、`OrganizationMember`、`Career`、`CareerStage`、`WorldSetting`、`Relationship` 模型；
  - `wizard_service` 的步骤名和 normalize 函数；
  - `relationship_service` 解析关系类型的方式；
  - `generation_log_service.save_log` 的参数；
  - `analysis_service.analyze_pending`。

## 有序清单

### 后端

1. [ ] **迁移 `0017_book_imports`**：新建 `book_imports` 表，`VersionSource` 增加 `import`（如果有数据库约束，一并修改），在项目备份表规则中登记 `book_imports`，补迁移测试和反射测试。
2. [ ] **`kb_parser`**：
   - 给 `parse` 增加 `max_chars`、`truncate` 参数；
   - 抽出 `epub_documents`，返回书名和各文档文字；
   - 现有知识库测试必须全部保持通过。
3. [ ] **`splitter.py`**（纯函数，测试先行）：
   - 强规则、卷规则、同一行既有卷又有章；
   - 序章（≥ 200 字）、目录区去除、兜底切块；
   - docx 按标题层级切、epub 按文档切；
   - 自定义正则：长度、嵌套量词、0 个匹配、子进程超时；
   - 7 种校正操作和边界情况；
   - 4 类提示。

   测试样本包括：对话体短行（不能误判为标题）、GBK 编码、"第一卷 第一章"写在同一行、中文和阿拉伯数字混用。
4. [ ] **`service.py` 与 API**：
   - 上传：上限 5 个，过期清理，`run_in_threadpool`；
   - 列表、详情、单章全文、重切、校正（revision 不一致返回 409）、预估、删除；
   - 开始：同一事务内完成建项目、分卷大纲、章节、导入版本，调用 `create_task`，删除解析记录；
   - 在 `main.py` 中加上传限制规则，在 lifespan 中执行过期清理。

   测试：
   - 访问别人的记录返回 404；
   - 范围校验，导入超过 3000 章被拒绝；
   - 开始失败时整体回滚，不留下项目（mock `create_task` 抛错）；
   - 已有拆书任务时返回 409；
   - 超大请求在 multipart 解析之前被拦截。
5. [ ] **提示词**：
   - 加入 `book_import` 分类，新增 `book_import.digest` 和 `book_import.synthesize` 的模板文件和注册项；
   - `formats.py` 新增两种输出格式；
   - 补默认渲染、锁定变量、用户覆盖生效的测试；
   - 更新 registry 文件头注释中的模板数量。
6. [ ] **`merge.py`**（纯函数）：角色并查集按别名合并、字段取最长、组织/职业/世界观/关系去重、`findings` 体积上限。全部是单元测试。
7. [ ] **`reverse.py` 与处理器**：
   - 装批与长章分段；
   - 通读：写入摘要（只写空摘要，分段章节拼接后再写）、累积已知角色名单、按批提交断点；
   - 汇总，以及跳过时的代码兜底；
   - apply：只写空字段，同名跳过，关系校验，维护检索单元；
   - 风格提取：失败只记警告；
   - 深度分析入队：把 `analyze_pending` 抽成可复用函数；
   - 重试和退避、paused 载荷、skip 接口、finish_result、validate_retry；
   - 生成记录。

   用 FakeLLM 测试：
   - 正常完整流程，校验各项统计；
   - 某批失败时重试 2 次后暂停，再分别验证"继续""跳过""结束"三种处理；
   - 汇总失败后跳过，使用代码合并的结果；
   - 中断后重试不重复调用已完成的批次（计数验证）；
   - 用户事先手动建了同名角色时，不会被覆盖；
   - 已有大纲摘要时，摘要不会被覆盖；
   - 勾选深度分析后，`chapter.analyze` 在角色写入后入队；
   - 风格成功时设为项目默认，项目已有默认风格时不改动；
   - 执行时某章已被删除，跳过这段并记警告。
8. [ ] **全量后端验证**：
   ```bash
   cd backend && uv run ruff check . && uv run pytest -q
   ```
   ruff format 只检查本任务新增或修改的文件。已知的历史格式问题和 SQLite 专属测试不在本任务处理范围内。

### 前端

9. [ ] **类型、API 与入口**：
   - `types.ts` 加入镜像类型，`api/bookImport.ts`；
   - 版本来源标签补上"导入"；
   - 注册 `/book-import` 懒加载路由；
   - 书架"新建"卡片菜单加入"拆书导入"。
10. [ ] **`BookImportPage`**：
    - 四步流程；
    - 未完成解析列表；
    - 虚拟化章节列表、行内改名、操作菜单；
    - 全文抽屉，支持点击某一行拆分；
    - 按提示类型筛选、notices；
    - 正则重切（确认后会清空校正）；
    - 范围滑块、预估（防抖）、开始后跳转；
    - revision 冲突（409）时重新拉取；
    - 移动端卡片列表。
11. [ ] **任务面板**：
    - 支持 `book.import` 的 `batch_end` 进度；
    - 暂停原因为 `failed` 时显示"跳过本批"；
    - 结果展示写入统计、跳过的章节和警告。
12. [ ] **前端测试**：
    - 步骤流转；
    - 409 后重新拉取；
    - 深度分析默认值按 100 章判断；
    - "跳过本批"只在暂停原因为 failed 时出现；
    - 移动端渲染；
    - 禁止写死颜色的测试要通过。
13. [ ] **前端验证**：
    ```bash
    cd frontend && npm run lint && npx vitest run && npm run build
    ```
    入口 chunk 不能明显增大，`BookImportPage` 必须懒加载。

### 收尾

14. [ ] **spec**：新增 `.trellis/spec/backend/book-import.md`，记录切章规则、校正操作、断点结构、暂停与跳过契约、只写空字段的规则，并更新 index。同时更新 frontend 的 component-guidelines，记录拆书页和虚拟列表的约定。
15. [ ] **trellis-check**：做一次变异验证，至少覆盖以下几处：
    - 序章阈值；
    - 目录区判定；
    - 只写空字段；
    - 别名合并；
    - 跳过后 `step_cursor` 前进；
    - revision 校验。
16. [ ] **浏览器冒烟**（产物放在 `/tmp/bi-smoke` 和 `.playwright-mcp`，结束后经用户确认再删除）：
    - 上传一个 GBK txt，包含卷、目录区和序章，检查切章与提示；
    - 依次执行合并、拆分、新开一卷，然后用正则重切；
    - 检查预估，然后开始导入；
    - 用 FakeLLM 或测试配置跑完任务；
    - 到概览、大纲、角色、风格各页检查写入结果；
    - 制造失败，验证暂停后跳过；
    - 在 375 宽度下检查没有横向溢出。

## 回滚点

- 每一步完成都要通过测试，可以单独回退。
- 迁移的 downgrade 会删除 `book_imports` 表。
