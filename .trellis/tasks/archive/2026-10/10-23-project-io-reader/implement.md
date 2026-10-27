# 执行计划：项目 JSON 备份与 EPUB 导出

## 开工前

- 本任务不需要迁移。先确认 `alembic heads` 为 `0015_knowledge_base`。
- 阅读以下 spec：
  - `backend/database-guidelines.md`
  - `backend/outline.md`（快照、`CHAPTER_NO_REFS` 的反射测试写法）
  - `backend/retrieval.md`（知识库分块、记忆写入点、上传中间件）
  - `backend/story-entities.md`
  - `backend/prompts-styles.md`（风格表）
  - `backend/error-handling.md`
  - `frontend/component-guidelines.md`
  - `frontend/type-safety.md`

## 有序清单

### 后端

1. [ ] **表规则注册表** `app/services/project_transfer/registry.py`，并附反射测试：
   - 与项目相关的表都已注册，或在 `NOT_EXPORTED` 中写明理由；
   - 每个外键列都已处理；
   - 每个 JSON 列都已声明重映射规则，或标明不含 id。

   **这是关卡：反射测试通过后才继续。**
2. [ ] **JSON 中的 id 重映射函数**：
   - `chapter_plans.character_ids`
   - `analysis_changes.target/before/after`：按 `writeback_service` 实际写入的键处理，每种变更类型各写一个用例
   - 三张历史表的 `changes`
   - `outline_snapshots.data`
   - 风格 `ref`
3. [ ] **导出**：
   - 流式 JSON 输出，分批查询，四个可选项；
   - `backup/estimate` 接口；
   - 校验项目归属，非本人返回 404。
4. [ ] **导入**：
   - preview：上传大小限制（扩展上传中间件的路径集合）、JSON 深度限制、格式和版本校验、文件内引用的完整性检查，并通过 token 缓存解析结果；
   - import：单个事务内插入数据并延后回填外键；风格按规则复用或新建；书名重名时加「（导入）」；知识库重新分块、为知识库和记忆写入关键词检索单元；有向量模型时排队重建索引；返回警告。
5. [ ] **EPUB**：
   - 实现 `epub_service`（opf、nav、ncx、扉页、简介页、卷标题页、章节页、css；XML 转义并去除控制字符）；
   - 在导出接口上增加 `format=epub`、`author`、`include_synopsis` 参数。
6. [ ] **测试**：新增 `test_project_transfer.py` 和 `test_epub.py`，必须包含以下内容：
   - **往返**：构造一个包含全部实体的项目（同名角色、同名章节、细纲、伏笔历史、分析变更、钉住和屏蔽的记忆、知识库文档、自定义关系类型、自定义风格与预设风格），导出后再导入，逐表比对数量和内容，并验证所有引用都指向新项目内的对应行。
   - **可选项**：勾选与不勾选时，结果分别包含、不包含对应数据。
   - **风格**：预设风格能对应上；自定义风格同名同内容时复用、同名不同内容时加后缀新建。
   - **全局关系类型**：缺失时降级为项目自定义类型，并给出警告。
   - **出错与校验**：导入出错时整体回滚；版本过新返回 400；文件内引用断裂返回 400；超过大小上限被拒绝；token 过期时提示重新选择文件。
   - **跨用户**：导出他人项目返回 404，导入的项目归属当前用户。
   - **导入后的检索**：记忆和知识库能用关键词检索到；配置了向量模型时排队了重建任务。
   - **EPUB**：mimetype 是第一个条目且未压缩；container 指向 opf；opf 的 manifest 和 spine 完整；nav 与 ncx 的层级正确；所有 xhtml 都能被 XML 解析器解析；有卷和无卷两种情况都覆盖；正文含控制字符时也能正常生成；只导出选中的章节。
7. [ ] **spec**：新建 `.trellis/spec/backend/project-transfer.md`，写清格式、表规则注册表、JSON 重映射规则、新增表时必须登记、版本升级方式，以及 EPUB 的结构约定；同时更新 `database-guidelines.md`，加入"新增与项目相关的表时，必须登记到传输注册表"。

### 前端

8. [ ] `types.ts`、`api/transfer.ts`；在导出弹窗中加入 EPUB 选项（作者名、是否包含简介页）。
9. [ ] 备份弹窗：可选项、大小估算、下载。
10. [ ] 导入弹窗（项目列表页）：上传、预览统计、警告与错误、修改书名、确认导入后跳转。
11. [ ] 纯函数测试（统计标签、大小格式化）；更新 spec；包体积不超过上限。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收：
- 准备一个包含设定、细纲、章节、分析、伏笔、记忆和知识库的项目。导出备份（勾选章节版本），再导入，逐页比对新项目：角色卡、关系图、伏笔时间线、记忆的钉住状态、知识库检索、章节版本列表。
- 导出 EPUB（有分卷，并去掉一章），用一款阅读器打开（例如 macOS 的「图书」），检查目录层级、扉页和首行缩进。

## 审查关卡与回滚点

- 第 1 步的反射测试和第 6 步的往返测试是两道关卡。
- 回滚：本任务不涉及数据库迁移，删除新增的服务和接口即可。
