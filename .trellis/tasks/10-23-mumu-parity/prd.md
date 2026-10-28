# 对标 MuMuAINovel 功能补齐

## Goal

参考 MuMuAINovel（下称 MuMu，源码 `/Users/wenyuantc/IdeaProjects/my/MuMuAINovel`，v1.5.7）补齐它的全部有效功能，并在可靠性、长篇一致性和安全性上做得更好。这个父任务只负责三件事：功能差距清单、子任务拆分、跨子任务的集成验收，不直接写代码。

## Background / 约束

- **许可证**：MuMu 采用 GPL v3，本项目 README 声明"自行实现，未复用其代码"。所有子任务只参考功能和交互，不复制源码、提示词原文或资源文件。
- **规模**：MuMu 后端约 6 万行、前端约 4.5 万行；本项目两端合计约 3600 行，7 张表，迁移版本到 `0004`。
- **本项目已有的基础**（详见 `research/current-inventory.md`）：FastAPI、异步 SQLAlchemy 和 Alembic，数据库用 PostgreSQL，测试用 SQLite；httpOnly JWT，带 `token_version`；SSE 分两阶段 `prepare_*_job` / `run_*_job`；`LLMProvider` 抽象层支持 OpenAI 兼容、Anthropic、Gemini；SSRF 防护；API Key 用 Fernet 加密；前端是 React 18、antd 5 和 Zustand。
- **并行任务**：`10-23-project-cover`（封面上传与 AI 生成，planning 中）会引入迁移 `0005`、Provider 的图片生成能力和文件存储方案。本任务的子任务不重复实现封面。如果要用到文件存储或上传（如拆书、知识库），复用封面任务确定的方案；迁移编号按实际落地顺序递增，冲突时 rebase 重编号。
- 调研材料：`research/mumu-creation-core.md`、`research/mumu-story-settings.md`、`research/mumu-prompts-agents.md`、`research/mumu-platform.md`、`research/current-inventory.md`。

## 功能差距清单

图例：✅ 本项目已具备　🟡 部分具备　❌ 缺失　⏬ 不打算照搬（理由见备注）

### A. 创作核心

| # | MuMu 功能 | 本项目 | 超越 MuMu 的方向（MuMu 缺陷） |
|---|---|---|---|
| A1 | 表单向导：世界观 → 职业 → 角色 → 大纲，SSE 流式 | 🟡 有世界观、角色、大纲，没有职业；每次整体覆盖 | 增量合并而不是覆盖；每步结果可编辑后再继续 |
| A2 | 向导断点续跑（`wizard_step`） | ❌ | 服务端持久化进度 |
| A3 | 灵感模式：对话式立项，分步给出选项 | ❌ | MuMu 的会话只存在前端，我们持久化会话 |
| A4 | 大纲续写、追加、全新生成 | ❌ 只有整体生成 | MuMu"全新生成"会直接删掉全部章节、分析和记忆，我们改为快照，可撤销 |
| A5 | 大纲一对一 / 一对多模式，大纲"展开"成多章规划 | 🟡 只能一对一建章 | MuMu 一对一有时靠序号匹配、有时用外键，重排后会错位；我们统一用外键，按大纲粒度选择模式，增量展开时保留已写章节 |
| A6 | 大纲 JSON 导入导出（append / merge） | ❌ | — |
| A7 | 单章流式生成，分层上下文（RTCO） | ✅ 有，但前情提要只是截取正文前 200 字 | 存章节摘要，按 token 预算分层注入 |
| A8 | 批量顺序生成（后台任务） | ❌ | MuMu 可能覆盖已写章节；我们提供"跳过已写"、失败后续跑、每 N 章人工审核点 |
| A9 | 章节字数控制 | 🟡 有 `target_words`，只靠提示词约束 | MuMu 也没有闭环；我们按中文字数口径统计，不足时续写，超出时收尾 |
| A10 | 章节分析：钩子、伏笔、情节点、角色状态、评分 | ❌ | 保留分析历史，重写前后评分可对比；分析失败可降级，不阻塞生成 |
| A11 | 按分析建议一键重写 | ❌ | MuMu 的版本功能是空壳（`auto_apply`、`save_as_version` 没实现）；我们做真正的版本体系 |
| A12 | 局部选段重写 | ❌ | 另加续写、扩写、缩写 |
| A13 | 章节版本、对比、回滚 | ❌ 生成结果直接覆盖正文 | MuMu 实际也没有；每次生成、重写、手动编辑都形成版本，可 diff，可按段落采纳 |
| A14 | 写作风格库、项目默认风格 | 🟡 项目上只有一个 `style` 字段 | 结构化风格维度，可从样文提取，可试写预览 |
| A15 | 生成历史 | ❌ | MuMu 的记录不完整，也没有页面；我们记录 prompt、模型、token、耗时，可查询 |
| A16 | 后台任务和任务浮窗 | ❌ | MuMu 有三套任务机制，队列只在内存里，重启后全部失败；我们做统一的持久化队列，支持取消和重启恢复 |
| A17 | AI 去味 | ⏬ MuMu 的路由没注册，属于死代码 | 并入 A12 的"局部改写"风格选项 |
| A18 | 章节阅读器：主题、字号、翻页 | ❌ 只有编辑器 | — |

### B. 设定与记忆

| # | MuMu 功能 | 本项目 | 超越方向 |
|---|---|---|---|
| B1 | 角色结构化字段：外貌、性格、状态、存活、心理 | 🟡 只有 `profile` 文本 | 字段结构化，状态随章节演进，并保留时间线 |
| B2 | 单个角色 AI 生成；大纲生成后自动补齐角色 | ❌ | 补齐前给预览，确认后再写入 |
| B3 | 组织（门派、势力）及成员 | ❌ | 组织和角色分开建表（MuMu 共用一张表） |
| B4 | 关系：21 种预置类型、亲密度、关系图谱 | ❌ 只有 `relations_text` | MuMu 自动更新时会覆盖关系名；我们保留关系变化历史 |
| B5 | 职业和等级体系（修仙境界等），分析后自动升降阶段 | ❌ | — |
| B6 | 伏笔：五种状态、计划回收章节、超期提醒 | ❌ | 回收时同步更新记忆；时间线视图 |
| B7 | 剧情记忆：分析结果拆分后向量化，生成时检索注入 | ❌ | MuMu 未归一化、度量配置错，经常检索不到；本地 470MB 模型太重。选型在对应子任务里决定 |
| B8 | 知识库：上传 txt/md/json，分块检索 | ❌ | 与 B7 共用向量方案 |
| B9 | 角色 / 组织卡片导入导出 | ❌ | MuMu 导出的是内部 ID，跨项目会丢关联；我们按名称或稳定键关联 |
| B10 | 世界观设定 | ✅ 扁平条目（比 MuMu 的四段自由文本更灵活） | 加分类：地点、势力、物品、规则 |

### C. 提示词与 AI 扩展

| # | MuMu 功能 | 本项目 | 超越方向 |
|---|---|---|---|
| C1 | 提示词模板：约 40 个，用户可覆盖、恢复默认、导入导出 | ❌ 提示词硬编码在 `backend/app/ai/prompts/` | 带变量校验，可预览渲染结果，可做版本对比 |
| C2 | 提示词工坊（社区分享） | ❌ | ⏬ MuMu 依赖作者的中央云端，身份可伪造；我们改为实例内共享、管理员审核，加文件导入导出 |
| C3 | 拆书：TXT 导入、识别编码、切章、AI 反推设定和大纲 | ❌ | MuMu 的任务只在内存里；我们走持久化任务，切章结果可人工校正 |
| C4 | 项目智能体：预览后确认写操作，保存快照 | ❌ | 用模型原生的工具调用协议，可撤销 |
| C5 | Skill（文件形式，按触发词匹配） | ❌ | MuMu 允许任何用户增删全局 Skill；我们区分用户级和管理员级 |
| C6 | MCP 插件（远程 HTTP/SSE） | ❌ | 沿用 SSRF 防护；默认不自动注入工具 |
| C7 | 用户级 API 设置 | ✅ 已做，且是加密存储（MuMu 是明文，接口还会返回完整密钥） | — |
| C8 | 项目封面 | 🟡 另一个任务 `10-23-project-cover` 在做 | — |

### D. 平台与周边

| # | MuMu 功能 | 本项目 | 备注 |
|---|---|---|---|
| D1 | 本地账号、多用户隔离、管理员 | ✅ 更完善：会话可强制失效、外键级联删除 | — |
| D2 | LinuxDO OAuth、邮箱验证码 | ❌ | 可选；state 和验证码不能只存进程内存 |
| D3 | 项目 JSON 导入导出（备份 / 迁移） | ❌ 只能导出 TXT/MD | 带版本号的格式，导入前预览 |
| D4 | EPUB 等格式导出 | ❌（MuMu 也没有） | 超越项 |
| D5 | 书架式项目列表、暗色主题、移动端适配 | ❌ | — |
| D6 | 公告、更新日志 | ❌ | ⏬ MuMu 的这两项都指向上游作者的服务器；如果要做，只做实例内的管理员公告，放最低优先级 |
| D7 | 赞助页 | ⏬ | 不做 |
| D8 | SQLite / PostgreSQL 双库 | 🟡 测试用 SQLite | 不维护双迁移目录 |

## 子任务拆分（建议，待确认）

按依赖和价值排序。每个子任务都单独规划、实现、验收、归档。依赖关系写进子任务自己的 PRD。

| 优先级 | 子任务（slug） | 覆盖 | 依赖 |
|---|---|---|---|
| P0-1 | `chapter-versions` 章节版本与局部改写 | A13、A12、A17、A9、A15 | 无 |
| P0-2 | `task-engine` 统一持久化后台任务与任务面板 | A16 | 无 |
| P0-3 | `story-entities` 结构化设定：角色、组织、关系图谱、卡片导入导出、世界观分类、上下文注入 | B1–B4、B9、B10 | task-engine |
| P0-3b | `career-system` 职业等级体系（用户决定从 story-entities 拆出，紧接着做） | B5 | story-entities |
| P0-4 | `analysis-memory` 章节分析、摘要、回写设定、按建议重写 | A10、A11、A7（摘要） | chapter-versions、story-entities、career-system、task-engine |
| P0-4b | `foreshadows` 伏笔管理（用户决定从 analysis-memory 拆出，紧接着做） | B6 | analysis-memory |
| P0-5 | `outline-pro` 大纲续写、一对多展开、导入导出、快照撤销、向导增量与续跑 | A1、A2、A4、A5、A6 | task-engine、chapter-versions |
| P0-5b | `batch-generate` 批量顺序生成（用户决定从 outline-pro 拆出，紧接着做） | A8 | outline-pro、analysis-memory |
| P1-6 | `prompt-styles` 提示词模板、写作风格库（含文件导入导出） | C1、A14 | 无 |
| P1-7 | `inspiration` 灵感模式与项目结构化字段 | A3 | 无 |
| P1-8 | `book-import` 拆书 | C3 | task-engine、story-entities、outline-pro；文件上传复用封面任务的方案 |
| P1-9 | `knowledge-base` 检索基建：知识库 + 向量剧情记忆（B7 由用户决定从 analysis-memory 并入） | B7、B8 | analysis-memory（摘要/情节点作为记忆来源） |
| P1-10 | `project-io-reader` 项目 JSON 备份与导入、EPUB 导出 | D3、D4 | 实体稳定后再做 JSON 格式，所以放在 P0 之后 |
| P1-10b | `reader-ui` 阅读器、书架、暗色主题、移动端（用户决定从 project-io-reader 拆出，紧接着做） | D5、A18 | 无（书架可接入 project-io-reader 的导出入口） |
| P2-11 | `project-agent` 项目智能体 | C4 | story-entities、chapter-versions |
| P2-12 | `skills-mcp` Skill 和 MCP 插件 | C5、C6 | prompt-styles |
| P2-13 | `oauth-announce` OAuth、邮箱登录、实例公告 | D2、D6 | 无 |
| P2-14 | `prompt-workshop` 实例内工坊：共享写作风格与提示词模板、管理员审核（用户决定从 prompt-styles 拆出） | C2 | prompt-styles |

## 进度

已归档 16/18（2026-10-28）：chapter-versions、task-engine、story-entities、career-system、analysis-memory、foreshadows、outline-pro、batch-generate、prompt-styles、inspiration、knowledge-base、project-io-reader、reader-ui、book-import、project-agent、skills-mcp。P0、P1 全部完成；P2 已完成 project-agent、skills-mcp，待做 prompt-workshop、oauth-announce。

不做：D7 赞助页；C2 里依赖中央云端的部分；D6 里指向上游作者服务器的部分；D8 双迁移目录。

## Acceptance Criteria（父任务）

- [ ] 上表每个子任务都已创建，并挂在本父任务下，各自的 PRD 写清依赖和验收标准。
- [ ] 差距清单里每一项标 ❌ / 🟡 的功能，都能对应到某个子任务；或者在"不做"里写明原因。
- [x] （P0/P1 已验收，2026-10-27，见 `research/integration-acceptance.md`；P2 完成后再补一次）所有子任务归档后做一次集成验收，完整走通这条链路：新建项目 → 灵感或向导生成设定 → 大纲展开 → 批量生成章节 → 自动分析回写 → 按建议重写，并能对比版本、回滚 → 导出 EPUB 和 JSON 备份 → 把备份导入新项目，数据完整。
- [ ] （P0/P1 范围已通过；Skill、MCP、共享提示词待 P2 完成后验收）集成验收时检查以下安全约束：多用户之间互相访问一律返回 404；API Key 不在任何接口里明文返回；AI 和 MCP 的外部地址都经过 SSRF 防护；全局资源（Skill、共享提示词）只有管理员能改。
- [x] 仓库里没有从 MuMu 复制的源码或提示词原文。（2026-10-27 文本比对，P2 完成后复查）

## Open Questions

- 子任务拆分和优先级是否认可，从哪个子任务开始规划（当前提问）。
