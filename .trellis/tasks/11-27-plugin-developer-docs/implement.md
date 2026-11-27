# 开发文档（插件开发指南）：实施计划

> 需求 `prd.md`（W1–W6），设计 `design.md`。依赖 `11-27-plugin-framework` 与 `11-27-short-drama-plugin` 已完成。
> 按顺序执行。中途只跑受影响测试加哨兵，全量门禁留到最后一步。

## 开始前复核

- [ ] 读当时的框架代码，列出**真实存在**的全部接入点与字段：`backend/app/plugins/manifest.py`、`registry.py`、`state.py`、`deps.py`，
      `frontend/src/plugins/types.ts`、`registry.tsx`、`RequirePlugin.tsx`。设计第 4 节的大纲按此增删，没有的接入点不写。
- [ ] 读短剧插件的实际目录，确定走读要引用的文件。
- [ ] 读框架与短剧的规范文档（`.trellis/spec/backend/feature-plugins.md`、`drama.md`），文档的说法与规范保持一致。
- [ ] 读现有测试 `frontend/src/utils/developerDocs.test.ts`，了解它断言了什么，改结构时不破坏。

## 步骤

### 1. 目录分组

- [ ] `content.ts` 增加 `DOC_GROUPS` 并保留 `DOC_SECTIONS` 导出；`DeveloperDocsPage.tsx` 的目录按组渲染（桌面与移动端）。
- [ ] 测试：现有八个章节的 id、顺序与标题不变；`DOC_SECTIONS` 的引用方不受影响；分组目录的结构。

验证：`cd frontend && npx vitest run src/utils/developerDocs.test.ts src/components/developerDocs && npm run lint`

### 2. 结构化内容

- [ ] `pluginContent.ts`：后端清单字段表（带固定标记注释）、前端两个类型的字段表（`satisfies Record<keyof …, string>`）、
      接入点表、检查清单、示例代码字符串、`REFERENCED_PATHS`。
- [ ] 确认 `tsc -b` 在字段表缺键时报错（临时删一个键验证后恢复）。

### 3. 一致性测试

- [ ] 后端测试 `backend/tests/test_plugin_docs_consistency.py`：`PluginManifest` 的字段集合与文档字段表一致；`REFERENCED_PATHS` 中的路径都存在。
- [ ] 临时改动一个字段名验证测试会失败，然后恢复。

验证：`cd backend && uv run pytest tests/test_plugin_docs_consistency.py -q`

### 4. 章节组件

按设计第 4 节（经复核修订后）逐章编写：

- [ ] 概念与边界、与 MCP 插件的区别
- [ ] 目录结构与清单字段
- [ ] 各接入点（接口、任务、页面与导航、数据与备份）
- [ ] 启停与数据
- [ ] 发布前检查清单
- [ ] 页面骨架接入第二部分与分隔导语

### 5. 最小示例的实操验证

- [ ] 在一个临时分支或临时目录里，严格按文档的「最小示例」步骤新增一个插件，不参考文档之外的任何信息。
      确认：管理后台「功能插件」页出现它；启用后页面可访问、接口可调用；停用后入口消失。
- [ ] 步骤走不通或有歧义之处即修改文档，直到能照着做通。验证用的示例插件不提交；过程记录到 `research/verification.md`。

### 6. 短剧插件走读

- [ ] 编写「完整示例：短剧插件」一章，引用的文件路径全部加入 `REFERENCED_PATHS`。

### 7. 走查与收口

- [ ] 浏览器走查：未登录访问；桌面与 375px 宽度；目录分组与锚点跳转；表格横向滚动；浅色与深色；键盘可达；
      现有开放 API 部分的显示与改动前一致（逐章对比）。
- [ ] 前端规范与 README（见 `design.md` 第 8 节）。
- [ ] 逐条核对 `prd.md` 的验收标准并勾选。
- [ ] 全量门禁：
  - `cd backend && uv run ruff check . && uv run pytest -n 4 --dist loadfile --durations=20`
  - `cd frontend && npm run lint && npm run test && npm run build`

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 文档写的是设计时的设想而不是实际代码 | 「开始前复核」先列真实接入点；第 5 步按文档实操一遍 |
| 改目录结构影响现有开放 API 指南 | 第 1 步单独完成并测试，保留原导出；走查时逐章对比 |
| 一致性测试靠正则读取前端文件，容易脆 | 用固定标记注释包住字段表；测试对「取不到」单独报清晰的错 |
| 框架后续再加接入点而文档没跟上 | 字段层面由类型约束与后端测试发现；规范里写明新增接入点必须同步此页 |

## 开始实现前的检查

- [ ] 用户已批准本子任务的规划；两项前序子任务已完成并归档。
- [ ] `implement.jsonl` 与 `check.jsonl` 各有至少一条真实的规范条目。
- [ ] 工作区干净，或已与用户确认现有改动的归属。
