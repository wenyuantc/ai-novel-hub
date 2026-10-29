# 最终功能覆盖核对（2026-10-29）

## 子任务与依赖

已逐一读取 `task.json` 并校验：17 个子任务均唯一存在于 `archive/2026-10/`，状态为 `completed`，归档前均指回父任务 `10-23-mumu-parity`；各自 PRD 均有验收标准。部分 PRD 通过“已确认的可复用能力”说明前置能力，本表统一列出实际依赖，避免仅凭是否包含“依赖”二字判断遗漏。

| 已归档子任务 | 覆盖差距项 | 前置能力 / 依赖说明 |
|---|---|---|
| `10-23-chapter-versions` | A9、A12、A13、A15、A17 | 独立落地，基于原章节生成与编辑 |
| `10-23-task-engine` | A16 | 独立落地，统一原有 AI 任务 |
| `10-23-story-entities` | B1–B4、B9、B10 | task-engine；原有角色、世界观与项目归属校验 |
| `10-23-career-system` | B5、A1 职业步骤 | story-entities；task-engine |
| `10-23-analysis-memory` | A7 摘要、A10、A11 | chapter-versions、story-entities、career-system、task-engine |
| `10-26-foreshadows` | B6 | analysis-memory、story-entities |
| `10-23-outline-pro` | A1 增量、A2、A4–A6 | task-engine、chapter-versions、analysis-memory、story-entities、foreshadows |
| `10-26-batch-generate` | A8 | outline-pro 的细纲与章节、analysis-memory 回写、task-engine、chapter-versions |
| `10-23-prompt-styles` | C1、A14 | 独立模板化原有提示词，供后续子任务扩展 |
| `10-23-inspiration` | A3 | prompt-styles 的模板注册、原有新建项目与向导；无需额外后台任务 |
| `10-23-knowledge-base` | B7、B8 | analysis-memory 的摘要 / 情节点、task-engine、prompt-styles；自建受限上传解析 |
| `10-23-project-io-reader` | D3、D4 | 前述项目实体稳定后落地，基于检索、设定、细纲、风格等表注册规则 |
| `10-27-reader-ui` | A18、D5 | 原有项目与章节；复用 project-io-reader 导出 / 备份入口 |
| `10-23-book-import` | C3 | task-engine、knowledge-base 的 `kb_parser`、analysis-memory、设定与向导 normalize、prompt-styles |
| `10-23-project-agent` | C4 | 设定服务、chapter-versions / 大纲快照、task-engine、检索与 prompt-styles |
| `10-23-skills-mcp` | C5、C6 | project-agent、prompt-styles、既有 SSRF / Fernet / 管理员能力 |
| `10-26-prompt-workshop` | C2 | prompt-styles；管理员审核与用户归属校验 |

`book-import` 最终复用已先落地的 `knowledge-base.kb_parser`，而非尚未完成的封面文件存储。原文件只用于解析，不作为长期原件存储；这是已归档 PRD 的实际设计。

## 原差距清单逐项归属

原 PRD 的 ❌ / 🟡 是 2026-10-23 的立项基线，以下为最终状态。

| 差距项 | 最终归属 / 状态 |
|---|---|
| A1、A2、A4、A5、A6 | outline-pro；职业步骤由 career-system 补齐，后续 wizard-resume 独立修复已归档 |
| A3 | inspiration |
| A7 | 原单章生成 + analysis-memory 的分层摘要；knowledge-base 的旧情节召回 |
| A8 | batch-generate |
| A9、A12、A13、A15 | chapter-versions，具体取舍见下节 |
| A10、A11 | analysis-memory，章节版本复用 chapter-versions |
| A14 | prompt-styles |
| A16 | task-engine |
| A17 | 原路由死代码不照搬，去 AI 味并入 chapter-versions 的局部改写 |
| A18 | reader-ui |
| B1、B2、B3、B4、B9、B10 | story-entities |
| B5 | career-system |
| B6 | foreshadows |
| B7、B8 | knowledge-base；自动记忆来源为 analysis-memory |
| C1 | prompt-styles |
| C2 | prompt-workshop 的实例内分享 / 审核 / 导入；中央云端不做 |
| C3 | book-import |
| C4 | project-agent |
| C5、C6 | skills-mcp |
| C7 | 立项前已有的用户级 AI 配置与加密密钥，后续模型分类 / 参数任务已归档 |
| C8 | 独立任务 project-cover，2026-10-29 已归档；上传 / AI 预览确认 / 书库 / 备份 / EPUB 接入完成 |
| D1 | 立项前已有认证与隔离；admin-user-management 补齐管理员入口与会话失效 |
| D2、D6 | 2026-10-29 用户取消 oauth-announce；OAuth / 邮箱登录 / 实例公告不做 |
| D3、D4 | project-io-reader |
| D5 | reader-ui；后续 zhiye-library-ui 更新官网、认证与书库，已归档 |
| D7 | 赞助页不做 |
| D8 | PostgreSQL 部署 + SQLite 隔离测试；不维护两套迁移目录 |

## 已批准的具体取舍

以下是子任务确认后的范围，不把立项时“超越方向”全部视作最终交付承诺：

- A9：`chapter-versions` D3 明确按非空白字符统计，低于目标 85% 最多续写两轮；超出目标不截断，润色不自动续写。
- A13：支持全文快照、逐字对比、回滚；通过局部改写预览采纳选区。没有独立的版本 diff 分段合并入口。
- A15：生成记录保留完整提示词、模型、状态、产出字数与耗时；该子任务明确 token 用量字段先预留为空。智能体原生工具调用另按 Provider 可用信息记录用量。
- B7 / B8：云端向量归一化 + BM25 / numpy；没有向量模型时降级关键词；不引入本地大模型 / pgvector。
- C4：默认写提案确认，用户可开启受限的小修改自动执行；MCP external 默认仍需单独确认，不能撤销。
- C5 / C2 权限：普通用户可管理私有 Skill 和自己的工坊分享；全局 Skill、审核与官方条目管理限管理员。可见全局资源无写权限为 403，私有资源不可见为 404。

## 本地参考库文本复查

范围：两库的 `backend/app` 与 `frontend/src`，扩展名 `.py/.j2/.ts/.tsx/.css/.scss/.md`，排除 `.test.` 文件；当前库 520 文件，参考库 286 文件。

- ≥100 非空白字符的完整文件一致：0。
- 每行移除非汉字后的连续 18 字窗口：0 命中。
- 去空行、去首尾空白、总长度 ≥50 字符的连续三行：60 处命中，已逐项查看。均为 import、CORS 参数、SQLAlchemy 归属过滤 / commit / refresh、整数解析，以及 AntD 上传区、删除确认、布局等通用写法；未发现业务提示词或独特业务实现的重复片段。
- 原始脱敏报告：`/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/mumu-final-text-audit-vjtthe54/text-audit.json`。

这是文本相似度复查证据，不以短片段匹配或零命中单独推断代码来源；最终结论结合独立设计、子任务实现记录与逐项审阅。
