# 执行计划：知识库与向量剧情记忆

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号。当前最新是 `0014_inspiration`，本任务计划用 `0015_knowledge_base`。
- 阅读这些 spec：
  - `backend/story-entities.md`：上下文组装
  - `backend/outline.md`：`CHAPTER_NO_REFS` 章节号平移
  - `backend/prompts-styles.md`：模板变量与 parity
  - `backend/error-handling.md`：任务契约
  - `backend/database-guidelines.md`
  - `frontend/component-guidelines.md`
  - `frontend/type-safety.md`
- 安装依赖：`uv add numpy pypdf python-multipart`。

## 有序清单

### 后端

1. [ ] **迁移 `0015_knowledge_base`**：新建 `kb_settings`、`kb_documents`、`kb_chunks`、`story_memories`、`retrieval_chunks` 五张表，并补迁移测试。
2. [ ] **向量模型**：
   - `ModelType` 加入 `embedding`；
   - 为 `openai_compat` 和 `gemini` 实现 `LLMProvider.embed`，`anthropic` 返回不支持；
   - AI 配置的校验允许新建向量模型，并支持按类型设置默认。

   用假服务测试 OpenAI 兼容和 Gemini 两种请求的格式。
3. [ ] **`retrieval_service`**：分词、BM25、向量编解码与归一化、混合打分、`min_score`、进程内缓存与失效、`upsert`/`delete`/`search`、"需要重建"的判定。单元测试要覆盖：
   - 分数有界，阈值以下的结果不返回；
   - 没有向量时只用关键词；
   - `embedding_key` 不一致的块不参与向量打分；
   - 缓存会在数据变化后失效。
4. [ ] **`kb_parser`**：实现 6 种格式的解析、魔数校验、扫描版 PDF 识别和分块（约 500 字，重叠 80 字，带 `heading_path`）。测试用的样例文件用代码现场生成：docx 和 epub 用 zipfile 拼出来，pdf 用 pypdf 写一页带文字的文件。
5. [ ] **`kb_service`**：
   - 设置的读写；
   - 上传：校验大小、数量、sha 去重，然后创建 `kb.index` 任务；
   - 文档的注入开关、删除、重建；
   - 分块分页；
   - 检索调试；
   - `context_block`。
6. [ ] **`kb.index` handler**：实现文档、记忆、重建三种模式；分批生成向量并逐批提交，中断后可以续跑；没有向量模型时以 `ready_keyword_only` 和 warning 收尾。
7. [ ] **`memory_service`**：
   - `sync_from_analysis`：在分析落库的同一个事务里执行，按 `content_key` 继承钉住和屏蔽标记，然后排队生成向量；
   - 删除章节时清理该章的自动记忆；
   - 手动记忆的增删改；
   - `context_block`：先放钉住的，再补检索结果，排除前情里已有的章节，最后按章号排序；
   - 把两个 `chapter_no` 字段登记进 `CHAPTER_NO_REFS`。
8. [ ] **接入**：
   - `context_builder` 新增两块内容，并扩展裁剪顺序；
   - 大纲、向导、实体生成、灵感模式的模板新增可选变量 `knowledge` / `memories`，默认模板在变量为空时不输出这两块，各服务组装查询并传入；
   - 文件头注释同步更新。

   完成后跑 parity 测试和全部提示词测试，确认块为空时提示词与之前完全一致，这是关卡。
9. [ ] **路由**：`/projects/{pid}/kb/*` 和 `/projects/{pid}/memories/*`。可选：`GET /chapters/{id}/context-preview`。
10. [ ] **测试**：新增 `test_retrieval.py`、`test_kb.py`、`test_memories.py`，并扩展 `context_builder`、`analysis`、跨用户和迁移相关的测试，逐条对应 PRD 的验收标准。必须包含：
    - 第 N 章只召回第 N 章之前的记忆；
    - 前情里已有的章节不重复注入；
    - 钉住的记忆优先，屏蔽的不召回；
    - 重新分析后保留标记；
    - 关闭文档的"注入"开关或项目总开关后不注入；
    - 更换向量模型后显示需要重建，重建完成前不使用旧向量；
    - 检索出错时生成照常进行。
11. [ ] **spec**：新建 `.trellis/spec/backend/retrieval.md`，写清打分公式、存储方式、建索引任务、记忆抽取规则、注入的位置和裁剪顺序，以及接入新的注入点的方法；同时更新 `database-guidelines`、`story-entities`（上下文组装）、`prompts-styles`（新增的模板变量）和 `outline.md`（`CHAPTER_NO_REFS`）。

### 前端

12. [ ] `types.ts`，新增 `api/kb.ts`、`api/memories.ts`；AI 配置页支持"向量模型"类型。
13. [ ] 知识库页：上传、文档表格、设置、检索调试、重建横幅。
14. [ ] 剧情记忆页：筛选、钉住和屏蔽、手动记忆、召回参数、检索调试、重建横幅；侧边栏增加入口。
15. [ ] 可选：章节编辑器中的注入预览。
16. [ ] 纯函数测试（状态文案、上传前检查、分数格式化），并更新 spec。新页面懒加载，包体积不超过上限。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收（假 AI 服务增加 `/v1/embeddings`，按字符哈希返回确定性的向量）：
- 不配置向量模型，上传一个 md 和一个 docx，用调试框检索，可以命中，状态显示"仅关键词"。
- 配置向量模型后，页面提示需要重建；点重建后，命中结果里出现向量分。
- 分析几章后打开记忆页，可以看到记忆；钉住一条，再生成后面的章节，在假服务日志里能看到【相关旧情节】中包含这条钉住的记忆，并且没有前情里已有的章节。
- 关闭项目的知识库总开关后，提示词里不再出现【参考资料】。

## 审查关卡与回滚点

- 第 3 步（打分）和第 8 步（parity）是关卡。
- 回滚方式：执行 0015 的 downgrade；去掉 `context_builder` 中两块内容的接入即可恢复原有行为。
