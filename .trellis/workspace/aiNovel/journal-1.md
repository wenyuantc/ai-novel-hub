# Journal - aiNovel (Part 1)

> AI development session journal
> Started: 2026-10-22

---



## Session 1: 模型配置增强：获取模型列表、思考等级、上下文与最大输出
<!-- trellis-session: v=2 fp=58346307b39ba8b8 -->

**Date**: 2026-10-23
**Task**: 模型配置增强：获取模型列表、思考等级、上下文与最大输出
**Branch**: `main`

### Summary

新增 POST /ai-configs/models 获取模型列表（支持未保存表单与已存 Key 回退，遵守 SSRF 防护）；ai_configs 增加 thinking_level/context_window_k/max_output_k（迁移 0002，K=1000 tokens）；三个 provider 映射思考等级并实现 list_models，关闭时不发送思考参数；生成/润色/向导透传 max_tokens 与思考等级，上下文大小参与章节前文预算；前端表单与列表更新；118 个后端测试通过，浏览器实测通过。本地开发库已升级至 0002。

### Git Commits

| Hash | Message |
|------|---------|
| `7994433` | feat: 模型配置支持获取模型列表、思考等级、上下文与最大输出 |

### Status

[OK] **Completed**


## Session 2: MuMu 对标规划与 P0-1 章节版本实现
<!-- trellis-session: v=2 fp=6d0ead72fe320a30 -->

**Date**: 2026-10-23
**Task**: MuMu 对标规划与 P0-1 章节版本实现
**Branch**: `main`

### Summary

调研 MuMuAINovel 四个功能域并盘点本项目，产出差距清单与 13 个子任务拆分；完成 chapter-versions：版本历史（固定 10 分钟合并、50 上限、标星）、对比回滚、局部改写预览采纳、自动续写、生成记录；浏览器冒烟验证通过

### Git Commits

| Hash | Message |
|------|---------|
| `2bf983f` | feat: 章节版本历史、局部改写与字数闭环 |

### Status

[OK] **Completed**


## Session 3: P0-2 统一持久化后台任务引擎
<!-- trellis-session: v=2 fp=543840001c50da40 -->

**Date**: 2026-10-23
**Task**: P0-2 统一持久化后台任务引擎
**Branch**: `main`

### Summary

数据库即队列的任务引擎：并发上限与资源互斥、生成/润色/向导改为后台任务、SSE 快照+增量订阅与断线重连、取消、重启中断与重试、任务浮窗；浏览器冒烟验证 AC1-AC6

### Git Commits

| Hash | Message |
|------|---------|
| `9f94b81` | feat: 统一持久化后台任务引擎与任务面板 |

### Status

[OK] **Completed**


## Session 4: P0-3 结构化设定体系
<!-- trellis-session: v=2 fp=def2956a9c8a3237 -->

**Date**: 2026-10-23
**Task**: P0-3 结构化设定体系
**Branch**: `main`

### Summary

角色结构化与别名、组织嵌套与成员、关系类型正反称呼与历史、懒加载关系图谱、AI 生成角色/组织与旧关系解析任务、卡片跨项目导入导出、章节精准选角上下文；拆出 career-system；浏览器冒烟验证 AC3-AC8

### Git Commits

| Hash | Message |
|------|---------|
| `18f9c77` | feat: 结构化设定体系（角色、组织、关系图谱、卡片导入导出、精准上下文） |

### Status

[OK] **Completed**


## Session 5: P0-3b 职业等级体系
<!-- trellis-session: v=2 fp=92af299b94e7b917 -->

**Date**: 2026-10-26
**Task**: P0-3b 职业等级体系
**Branch**: `main`

### Summary

职业与稳定 id 阶梯、角色 1 主 3 副挂接与 apply_assignment 历史、AI 增量生成职业、向导职业步骤与角色分配职业、上下文职业注入、卡片 v2；浏览器冒烟验证 AC1-AC7。遗留：卡片导入补阶段追加到末尾会打乱阶梯顺序

### Git Commits

| Hash | Message |
|------|---------|
| `d94a66c` | feat: 职业等级体系（职业阶梯、角色主副职业、向导职业步骤、卡片 v2） |

### Status

[OK] **Completed**


## Session 6: P0-4 章节分析与设定回写
<!-- trellis-session: v=2 fp=55fa826aa4a23b66 -->

**Date**: 2026-10-26
**Task**: P0-4 章节分析与设定回写
**Branch**: `main`

### Summary

章节分析任务与历史、过期判定、自动排队与同项目串行（serial_key）、回写分流与撤销、更晚章节优先、前情摘要、按建议重写与评分对比；拆出 foreshadows、B7 并入 knowledge-base；冒烟验证 AC1-AC7 并修复一键分析重复入队

### Git Commits

| Hash | Message |
|------|---------|
| `97f699d` | feat: 章节分析与设定回写（摘要、评分建议、回写清单、按建议重写） |

### Status

[OK] **Completed**


## Session 7: P0-4b 伏笔管理
<!-- trellis-session: v=2 fp=e4b240e7ca42dda2 -->

**Date**: 2026-10-26
**Task**: P0-4b 伏笔管理
**Branch**: `main`

### Summary

伏笔 CRUD 与 apply_foreshadow 历史、分析联动三类回写（自动回收/自动标已埋/新伏笔待确认含 overrides）、上下文五档提醒与裁剪、伏笔页与 SVG 时间线；无伏笔时提示词与上下文逐字不变；冒烟验证 AC1-AC5

### Git Commits

| Hash | Message |
|------|---------|
| `1c823a7` | feat: 伏笔管理（分析联动、上下文五档提醒、伏笔页与时间线） |

### Status

[OK] **Completed**


## Session 8: P0-5 大纲进阶
<!-- trellis-session: v=2 fp=52fda5d7d7894cd9 -->

**Date**: 2026-10-26
**Task**: P0-5 大纲进阶
**Branch**: `main`

### Summary

细纲与建章、全局章节号平移（登记表+反射测试）、续写/展开草稿预览、大纲导入导出、快照恢复（原 id、关联恢复、PG 序列只前移）、向导追加与续跑；拆出 batch-generate；冒烟验证 AC1-AC7

### Git Commits

| Hash | Message |
|------|---------|
| `f38fab3` | feat: 大纲进阶（续写、单条展开细纲、导入导出、快照恢复、向导追加与续跑） |

### Status

[OK] **Completed**


## Session 9: batch-generate 批量顺序生成
<!-- trellis-session: v=2 fp=a62b666502295b26 -->

**Date**: 2026-10-26
**Task**: batch-generate 批量顺序生成
**Branch**: `main`

### Summary

实现 P0-5b 批量顺序生成：单任务逐章生成+任务内分析回写、断点续跑、失败重试与跳过、待审核暂停（引擎新增 paused 状态）、范围锁、发起预览；前端弹窗/横幅/任务面板。

### Main Changes

- 迁移 0012 tasks.checkpoint；TaskOut 增加 checkpoint/step_cursor
- 引擎 paused 状态与 resume/finish 接口

### Git Commits

| Hash | Message |
|------|---------|
| `47c7941` | feat: 批量顺序生成（跳过已写、任务内分析回写、失败续跑、待审核暂停） |
| `b5415e6` | chore(task): archive 10-26-batch-generate |

### Testing

- [OK] 后端 356 passed；前端 164 passed；浏览器冒烟覆盖审核点、失败续跑、重启中断补分析

### Status

[OK] **Completed**

### Next Steps

- 按父任务表开始规划 P1（prompt-styles 等）


## Session 10: prompt-styles 提示词模板与写作风格库
<!-- trellis-session: v=2 fp=c0bec1e381aff074 -->

**Date**: 2026-10-26
**Task**: prompt-styles 提示词模板与写作风格库
**Branch**: `main`

### Summary

实现 P1-6：沙箱 Jinja2 模板化 19 个提示词（parity 逐字一致）、用户覆盖与默认更新提醒、结构化写作风格库与逐章风格解析；拆出 prompt-workshop（P2-14）。

### Main Changes

- 迁移 0013 prompt_overrides/prompt_override_versions/writing_styles，projects.default_style_id、chapters.style_id
- 新增依赖 jinja2

### Git Commits

| Hash | Message |
|------|---------|
| `46d72ba` | feat: 提示词模板与写作风格库（沙箱 Jinja2 模板、用户覆盖、结构化风格、逐章风格解析） |
| `e83f50c` | chore(task): archive 10-23-prompt-styles |

### Testing

- [OK] 后端 472 passed；前端 196 passed；浏览器冒烟覆盖模板校验与覆盖、样文提取、试写、批量逐章风格、停用覆盖

### Status

[OK] **Completed**

### Next Steps

- 继续规划 P1：inspiration / book-import / knowledge-base / project-io-reader


## Session 11: inspiration 灵感模式与项目结构化字段
<!-- trellis-session: v=2 fp=9a6e4852e7c50681 -->

**Date**: 2026-10-26
**Task**: inspiration 灵感模式与项目结构化字段
**Branch**: `main`

### Summary

实现 P1-7：项目新增 6 个结构化字段并注入作品信息；服务端持久化的分步灵感会话（选项、编辑、带反馈换一批、补充想法、stale 标记、规模建议）、同步 SSE 生成与请求内重试、转为项目/应用到已有项目/从项目补全。

### Main Changes

- 迁移 0014：projects 新字段 + inspiration_sessions

### Git Commits

| Hash | Message |
|------|---------|
| `1b5a115` | feat: 灵感模式与项目结构化字段（服务端会话、分步选项、补全与应用到已有项目） |
| `4774bfb` | chore(task): archive 10-23-inspiration |

### Testing

- [OK] 后端 500 passed；前端 224 passed；浏览器冒烟覆盖重试、换一批、编辑后选、标签多选、stale、规模预填向导、补全与应用

### Status

[OK] **Completed**

### Next Steps

- 继续规划 P1：book-import / knowledge-base / project-io-reader


## Session 12: knowledge-base 知识库与向量剧情记忆
<!-- trellis-session: v=2 fp=a689624fcb49736d -->

**Date**: 2026-10-27
**Task**: knowledge-base 知识库与向量剧情记忆
**Branch**: `main`

### Summary

实现 P1-9：embedding 模型类型、统一检索表（库内向量 + numpy 余弦 + 二元组 BM25 混合）、知识库六种格式解析分块与 kb.index 任务、剧情记忆规则抽取与钉住/屏蔽、上下文两块注入（为空时逐字不变）；上传安全加固。

### Main Changes

- 迁移 0015：kb_settings/kb_documents/kb_chunks/story_memories/retrieval_chunks
- 新增依赖 numpy、pypdf、python-multipart

### Git Commits

| Hash | Message |
|------|---------|
| `8b00e9d` | feat: 知识库与向量剧情记忆（统一检索基建、混合检索、记忆钉住/屏蔽、上下文注入） |
| `2e80b01` | chore(task): archive 10-23-knowledge-base |

### Testing

- [OK] 后端 565 passed（PostgreSQL 临时容器迁移往返通过）；前端 257 passed；浏览器冒烟覆盖上传、仅关键词、重建后混合检索、钉住、注入与总开关

### Status

[OK] **Completed**

### Next Steps

- 继续规划 P1：project-io-reader（book-import 依赖封面任务的上传方案）


## Session 13: 向导失败后从中断处继续
<!-- trellis-session: v=2 fp=c3fddc93095af2b3 -->

**Date**: 2026-10-27
**Task**: 向导失败后从中断处继续
**Branch**: `main`

### Summary

向导（世界观/职业/角色/大纲）失败或中断后，基于旧任务部分输出续写：WizardRequest.continue_from_task_id、截断到完整对象+assistant/续写指令（新模板 wizard.continue）、续写开头清洗拼接、可链式继续；前端恢复最近失败任务并新增「从中断处继续」按钮。后端 574 测试、ruff、前端 tsc/eslint/vitest 通过；未做真实断流手测。

### Git Commits

| Hash | Message |
|------|---------|
| `4065aad` | feat: 向导生成失败后可从中断处继续 |

### Status

[OK] **Completed**


## Session 14: project-io-reader 项目 JSON 备份导入与 EPUB 导出
<!-- trellis-session: v=2 fp=0136b37a1bc9e7f3 -->

**Date**: 2026-10-27
**Task**: project-io-reader 项目 JSON 备份导入与 EPUB 导出
**Branch**: `main`

### Summary

实现 P1-10：ainovel.project v1 备份（按 id 重映射、表规则注册表+反射测试、JSON 内 id 重映射、延后回填）、导入预览与单事务导入、风格/关系类型/知识库/记忆处理；EPUB 3 导出；拆出 reader-ui（P1-10b）。

### Git Commits

| Hash | Message |
|------|---------|
| `f718918` | feat: 项目 JSON 备份导入与 EPUB 导出（按 id 重映射、反射测试防漏、导入预览） |
| `950735a` | chore(task): archive 10-23-project-io-reader |

### Testing

- [OK] 后端 598 passed（PostgreSQL 临时容器 582/583，失败为既有 SQLite 专用测试）；前端 276 passed；浏览器冒烟覆盖备份往返与 EPUB 结构

### Status

[OK] **Completed**

### Next Steps

- 规划 P1-10b reader-ui（阅读器、书架、暗色主题、移动端）


## Session 15: reader-ui 阅读器、书架、暗色主题、移动端
<!-- trellis-session: v=2 fp=c5c1c197652e9c52 -->

**Date**: 2026-10-27
**Task**: reader-ui 阅读器、书架、暗色主题、移动端
**Branch**: `main`

### Summary

实现 P1-10b：服务端阅读进度与阅读器、书本式书架、全站暗色主题（写死颜色迁移+防回归测试）、移动端抽屉与重点页适配；冒烟修复书架搜索框 100vw 溢出。

### Main Changes

- 迁移 0016：projects.reading_progress（登记传输注册表）

### Git Commits

| Hash | Message |
|------|---------|
| `9a1aadb` | feat: 阅读器、书架、暗色主题与移动端适配 |
| `22cb9f7` | chore(task): archive 10-27-reader-ui |

### Testing

- [OK] 后端 606 passed；前端 307 passed；Playwright 375px 有滚动条时 14 页无横向溢出，暗色与跟随系统验证

### Status

[OK] **Completed**

### Next Steps

- 规划 P1-8 book-import（拆书），之后进入 P2


## Session 16: book-import 拆书导入
<!-- trellis-session: v=2 fp=734ab637140d8de2 -->

**Date**: 2026-10-27
**Task**: book-import 拆书导入
**Branch**: `main`

### Summary

实现 P1-8：txt/md/docx/epub 上传与强规则切章（分卷、目录区、序章、兜底、自定义正则子进程）、服务端校正与修订号 409、单事务建项目导入、book.import 分批通读反推（断点续跑、失败暂停可继续/跳过/结束）、风格提取与深度分析入队、前端四步拆书页；冒烟修复目录后引言切错、虚拟表格标题只剩省略号、汇总丢失别名。

### Main Changes

- 迁移 0017：book_imports 表、generation_logs.kind 放宽到 32、版本来源 import
- book_import 服务（splitter/merge/reverse/service）、2 个提示词模板、/book-imports API 与 skip 接口
- 前端 BookImportPage、任务面板跳过本批与已暂停文案、结果视图

### Git Commits

| Hash | Message |
|------|---------|
| `35e0d3a` | feat: 拆书导入（切章校正与 AI 反推设定） |
| `a09f39c` | chore(task): archive 10-23-book-import |

### Testing

- [OK] 后端 680 passed；前端 333 passed；冒烟覆盖 GBK 目录区与序章、拆分/改名/合并/新开分卷、正则重切、409 标记、失败暂停跳过、反推写入、别名补回、375px 无溢出

### Status

[OK] **Completed**

### Next Steps

- P2：project-agent、skills-mcp、oauth-announce、prompt-workshop；或先做父任务集成验收


## Session 17: P2-11 项目智能体：审计、修复、冒烟与归档
<!-- trellis-session: v=2 fp=7d6d0515551fe66f -->

**Date**: 2026-10-28
**Task**: P2-11 项目智能体：审计、修复、冒烟与归档
**Branch**: `codex/project-agent`

### Summary

接手外部完成的 project-agent 实现，完成 trellis-check 审计与变异验证、spec 补写、浏览器冒烟，修复跨 Provider 非法参数、撤销后 created_at 丢失（及随之引入的预览 409 时序问题）、任务悬浮按钮遮挡面板输入框，提交并归档。

### Main Changes

- Provider 三家 chat_tools_stream；history_arguments 容忍非法参数
- effects 快照保留 created_at，preview_diff 跳过 created_at
- TaskPanel 悬浮按钮通过 --agent-dock-width 避让智能体面板
- 新增 spec backend/agent.md，同步 7 份 spec

### Git Commits

| Hash | Message |
|------|---------|
| `9155d38` | feat: 项目智能体（原生工具调用、修改提案确认与撤销） |
| `3d150bf` | chore(task): archive 10-23-project-agent |

### Testing

- [OK] 后端 841 passed；前端 lint、360 passed、build 通过
- [OK] 浏览器冒烟：确认/撤销、同轮续跑拿到新 id、级联删除撤销、正文替换撤销、启动任务后取消、自动执行首批、刷新恢复、375 抽屉

### Status

[OK] **Completed**

### Next Steps

- 设定页卡片栅格适配面板宽度（已拆为独立会话）
- P2-12 skills-mcp 规划


## Session 18: P2-12 Skill 与 MCP 插件：规划、实现、检查、冒烟与归档
<!-- trellis-session: v=2 fp=4856e220b5119108 -->

**Date**: 2026-10-28
**Task**: P2-12 Skill 与 MCP 插件：规划、实现、检查、冒烟与归档
**Branch**: `main`

### Summary

完成 skills-mcp：Skill 两级权限与导入导出、智能体按需加载与强制启用、生成任务 Skill 快照；MCP 远程插件（加密请求头、SSRF、禁止重定向）接入智能体为 external 提案；check 修复中断回收、工具名路由、YAML 别名炸弹等；冒烟后修复同名选项、maxLength 警告、取消文案、重复警告与 409 原因提示。

### Main Changes

- 迁移 0019：skills、skill_references、mcp_plugins；会话 mcp_plugin_ids、消息 skill 快照
- external 提案：默认确认、不可撤销、不自动执行、recover_external 中断回收
- 新增 spec backend/skills-mcp.md，同步 11 份 spec；MCP_ENABLED 写入配置样例

### Git Commits

| Hash | Message |
|------|---------|
| `3c5cea8` | feat: Skill 与 MCP 插件 |

### Testing

- [OK] 后端 924 passed；前端 lint、394 passed、build 通过；46 个变异全部被抓住
- [OK] 浏览器冒烟：Skill 复制/导入导出/生成注入；插件表单与 JSON 导入、测试、试用；智能体强制 Skill、免确认工具、外部提案确认与 409；375 无溢出

### Status

[OK] **Completed**

### Next Steps

- P2 剩余：prompt-workshop、oauth-announce
- 全部完成后做父任务剩余验收（Skill/MCP/共享提示词安全约束、链路复跑、未复制检查）


## Session 19: 完成提示词工坊并合并 main
<!-- trellis-session: v=2 fp=082c909e18658a6b -->

**Date**: 2026-10-29
**Task**: 完成提示词工坊并合并 main
**Branch**: `main`

### Summary

完成实例内提示词工坊，实现分享审核、匿名署名、点赞和导入历史恢复；完整验收后快进合并本地 main 并归档，父任务进度 17/18。仓库未配置远端，推送待用户提供 URL。

### Main Changes

## 完成范围

- 实例内共享写作风格和单 key 提示词模板，包含作者分享 / 更新 / 撤回、管理员审核 / 官方发布 / 元数据管理、浏览筛选与分页、匿名署名、点赞和导入。
- 模板导入前展示当前生效内容差异，保存导入前版本并启用覆盖，重复导入保留工坊来源备注；风格重复导入产生独立副本，计数每人一次。
- 修复详情状态、重复导入预览快照、首次通过标签和审核 / 删除锁顺序；新增对应回归。
- 后端迁移 0020，前后端契约及父任务 C2 记录已同步；仅提交本任务范围，保留其他任务和 HTML 文件。

## 集成与验证

- 四个工作提交在 codex/prompt-workshop 完成，已快进合并到本地 main。
- 合并后的完整门禁已通过：后端 Ruff + 939 项测试（384.14 秒），前端 lint / TypeScript / build + 407 项测试。
- 浏览器验证完整分享审核导入恢复与更新上下架流程；375px 无横向溢出，控制台无错误 / 警告。
- PostgreSQL 真实行锁等待未现场执行；已核对锁顺序并以 SQLite 接口竞争测试验证终态及无孤立记录。

## 推送状态

仓库没有配置 Git 远端，已请求用户提供 URL；未执行推送。收到 URL 后配置远端、检查远端历史并推送 main。


### Git Commits

| Hash | Message |
|------|---------|
| `d1dfae1` | refactor(backend): 公开风格与模板保存辅助函数 |
| `4820409` | feat(backend): 新增实例内提示词工坊 |
| `b372a3e` | feat(frontend): 新增提示词工坊分享审核页面 |
| `1b0046b` | docs: 记录工坊契约与验收结果 |

### Testing

- [OK] main：backend Ruff + 939 tests；frontend lint/build + 407 tests；浏览器完整流程与375px通过

### Status

[OK] **Completed**

### Next Steps

- 用户提供远端仓库 URL 后检查远端历史并推送 main


## Session 20: 项目封面：上传、AI 生成与备份导出
<!-- trellis-session: v=2 fp=fd3050c0923f0579 -->

**Date**: 2026-10-29
**Task**: 项目封面：上传、AI 生成与备份导出
**Branch**: `main`

### Summary

完成项目封面上传、AI预览确认、书架展示、备份恢复与EPUB封面；修复提示词计数遮挡按钮。后端975项与前端414项、lint/build及独立复核通过，浏览器与实际HTTP验收通过。已创建3个工作提交、归档10-23-project-cover并快进合并到本地main；未推送。

### Git Commits

| Hash | Message |
|------|---------|
| `54bf4c4` | feat(backend): 支持项目封面上传、生成及备份导出 |
| `ed75db7` | feat(frontend): 接入项目封面管理与书架展示 |
| `0b7a95b` | docs: 记录项目封面契约与验收结果 |

### Status

[OK] **Completed**


## Session 21: 知页官网、认证与书库 UI 落地及归档
<!-- trellis-session: v=2 fp=27617a2933223ab8 -->

**Date**: 2026-10-29
**Task**: 知页官网、认证与书库 UI 落地及归档
**Branch**: `main`

### Summary

完成知页公开官网、首页认证弹窗、登录注册页、创作外壳与真实数据书库；质量与浏览器验收通过，按用户授权提交并归档，未推送远程。

### Main Changes

- 迁移颜色/字体/圆角 token，安装本地 Fraunces/Geist，统一 CSS 与 AntD 的深浅/系统主题。
- 实现公开官网与欢迎弹窗、双栏登录注册页，保留真实认证、注册开关、字段校验和受保护目标回跳。
- 统一侧栏/移动导航、账号与后台任务入口；书库接真实统计、封面、最近章节，保留筛选排序、视图、新建、阅读、导出与备份恢复。
- 同步前端契约和设计差异；任务已归档到 .trellis/tasks/archive/2026-10/10-29-zhiye-library-ui，pelican-bicycle.html 为原有未改动文件，未纳入提交。

### Git Commits

| Hash | Message |
|------|---------|
| `afd0eacdbb098d9b62b2648d6ffce8f4a9d9dccd` | feat(frontend): 实现知页官网认证、布局与真实数据书库 |
| `dd62b0f9bc9dec5ebd20c5e286f626143211a5ad` | docs(frontend): 同步知页界面契约与验收记录 |

### Testing

- [OK] 最终全范围独立审查通过；npm run lint、npm run build、npm test 全部通过，54 文件 417 项；git diff --check 通过。
- [OK] 使用隔离真实后端验收 320/375/768/1024/1440px、深浅主题、空/错/加载、真实作品操作、认证错误、关闭注册及 Modal 焦点/关闭；未修改用户原数据库。

### Status

[OK] **Completed**

### Next Steps

- 本任务已完成归档，无待办；本轮仅本地提交，未推送远程。


## Session 22: MuMu 对标父任务最终验收与归档
<!-- trellis-session: v=2 fp=a017c4fc86c8d88c -->

**Date**: 2026-10-29
**Task**: MuMu 对标父任务最终验收与归档
**Branch**: `main`

### Summary

完成最后一个未归档父任务 10-23-mumu-parity 的最终验收：17 个子任务、44 项功能覆盖核对完成；后端 975 项、前端 417 项及 lint/build 通过；同一项目创作主链路与 P2 安全通过，父任务已归档，活动任务归零。本轮无新产品代码，所列提交为本轮复核的既有 P2 功能提交；仅自动提交任务资料与会话日志，未推送。

### Main Changes

- 新增最终覆盖与验收记录，注明已取消范围、已批准取舍、文本相似度复查与验证限制；核对现有 spec 后确认无需新增契约规则。
- 父任务已归档至 .trellis/tasks/archive/2026-10/10-23-mumu-parity；保留原有未跟踪 pelican-bicycle.html。

### Git Commits

| Hash | Message |
|------|---------|
| `9155d38` | feat: 项目智能体（原生工具调用、修改提案确认与撤销） |
| `3c5cea8` | feat: Skill 与 MCP 插件 |
| `4820409` | feat(backend): 新增实例内提示词工坊 |
| `b372a3e` | feat(frontend): 新增提示词工坊分享审核页面 |

### Testing

- [OK] 后端 Ruff + 975 passed（322.20 秒，1 条 Gemini SDK 弃用警告）；前端 lint/build + 54 文件 417 passed；git diff --check 通过。
- [OK] 隔离 HTTP 主链路 57 请求/123 断言/8 任务全部成功；27 表（20 非空）JSON 往返内容与关联一致，两章正文 SHA-256 一致，EPUB 结构通过。
- [OK] P2 安全相关 144 项及逐请求 SSRF 补充 6 项通过：智能体确认/撤销、跨用户隔离、Skill 快照、MCP 脱敏和默认确认、工坊审核导入恢复。

### Status

[OK] **Completed**

### Next Steps

- 当前无活动任务；真实模型质量、PostgreSQL 真实行锁及已有 SDK 弃用警告等限制已记录在最终验收文档。


## Session 23: AI 生成人物关系：实现、验收与归档
<!-- trellis-session: v=2 fp=bfbf196228a3c189 -->

**Date**: 2026-10-30
**Task**: AI 生成人物关系：实现、验收与归档
**Branch**: `feature/ai-relationship-generation`

### Summary

实现选定已有角色间缺失关系的 AI 草稿生成、编辑勾选与确认保存；完成三批工作提交，并按用户“提交归档”指令归档任务。

### Main Changes

- 新增 relations.generate 后台任务与只读草稿，支持 2–20 个已有角色、上下文预算、身份 ID 校验、取消和结果恢复。
- 接入关系页入口、lazy 结果预览与批量确认，保存来源 ai_generate；结果重开屏蔽已存关系，保存后自行广播刷新。
- 修复角色对及首次自定义类型的并发唯一冲突，精确返回中文 409，保留整批回滚和外层事务所有权；同步跨层规范与证据。

### Git Commits

| Hash | Message |
|------|---------|
| `6a98227eb60f3618b1a3c689d79bf498bbd023af` | feat(relationships): 增加 AI 批量关系草稿生成 |
| `c5c61e33a75d8194e8f5b5d0d6e6edbeae8c3eea` | feat(frontend): 接入 AI 关系生成与预览保存 |
| `036af7a5e4a76b32b3041ac870998cd76339ad1a` | docs(relationships): 记录生成契约与验收结果 |

### Testing

- [OK] 上轮最终后端完整测试 1019 项、前端 428 项通过；Ruff、ESLint、TypeScript 和 Vite 构建通过，证据随任务归档。
- [OK] 隔离 SQLite/FakeLLM 的真实 TCP/SSE 验证草稿无写入、断开重接、选择 2/3、方向、来源、两类并发 201/409、重试及批次回滚。
- [OK] 本轮仅提交与归档，核对三批精确暂存范围及 diff 空白检查；清理研究资料行尾空白，未改产品代码、未重跑产品测试。

### Status

[OK] **Completed**

### Next Steps

- Work 设置禁用本地浏览器，真实交互、浅暗主题和 375px 尚未验证；真实 PostgreSQL 并发及真实模型创作质量仍未验证。保留这些限制，不将归档等同于验收通过。


## Session 24: 平台 API Key、开放读取接口与客户开发文档
<!-- trellis-session: v=2 fp=1dc9ae11e9699af3 -->

**Date**: 2026-10-30
**Task**: 平台 API Key、开放读取接口与客户开发文档
**Branch**: `codex/openapi-api-keys`

### Summary

实现平台密钥可再次显隐复制、按小说授权与独立限流，开放20个只读接口及OpenAPI，新增首页客户开发文档；完成验证、四组工作提交与任务归档。

### Main Changes

- 新增0022迁移、Fernet密文与摘要认证、指定/全部小说授权、有效期和永久撤销；平台Key独立于网页登录会话。
- 开放小说、章节、封面、导出、人物、大纲、世界观、组织与关系读取，提供独立Swagger及OpenAPI JSON。
- 新增密钥管理页与首页开发文档入口，公开/developers沿用官网主题，包含鉴权、分页、真实响应、错误限流及可复制接入示例。
- 修复迟到请求倒退分钟限流窗口的竞争：窗口只能前进、调用时间非递减，增加两组先失败后通过的回归。
- 按用户“提交 归档”授权完成四组工作提交；归档当前任务，原有pelican-bicycle.html保持原样，不推送或部署。

### Git Commits

| Hash | Message |
|------|---------|
| `c916c96512d32cd6148b47f58e7d72bd9a4cd07d` | feat(api-keys): 支持小说范围授权与可再次查看的访问密钥 |
| `6c423a26be81d8e606bb5eeb4e4f494c30fbef50` | feat(open-api): 开放小说与创作设定只读接口及文档 |
| `7c29099b142a4513e5ffe537479cf0c65e306b18` | feat(frontend): 增加密钥管理与公开开发文档 |
| `6815f644a6c6f477b8f5e6121bee106c6abcfa40` | docs(open-api): 记录接入方式、接口契约和验收结果 |

### Testing

- [OK] backend完整pytest：1061 passed；Ruff exit0；Key/OpenAPI定向41 passed，追加公开文档后OpenAPI契约21 passed。
- [OK] frontend lint/build exit0；58文件447项测试通过；新增页面懒加载，无新增依赖或包体积警告。
- [OK] 隔离QA实测密钥显隐复制、刷新后再次查看、Swagger读取、授权隔离与撤销；客户文档匿名访问、目录、代码复制、登录返回和键盘焦点正常。
- [OK] 首页及客户文档在320/375/768/1024/1440px明暗主题下无页面横向溢出；实际JSON HTTP200且20条GET与X-API-Key定义正确。
- [OK] IAB未观察到下载完成事件，且无法强制重现剪贴板失败弹窗；实际文件HTTP、复制结果及失败分支自动化均有证据，限制已保留在归档验收记录。
- [OK] 完整暂存检查通过；仅清理五份规范摘录末尾多余空行，49个产品文件与已验证快照一致，未放宽测试或阈值。

### Status

[OK] **Completed**

### Next Steps

- 启用新增API前应用数据库迁移0022并重启后端；本会话未推送远程、未迁移或重启原8000实例。


## Session 25: 管理员作品管理实现、命名与验收收尾
<!-- trellis-session: v=2 fp=71d2dd5bc2c15397 -->

**Date**: 2026-10-30
**Task**: 管理员作品管理实现、命名与验收收尾
**Branch**: `codex/admin-project-management`

### Summary

完成管理员跨用户作品查询、筛选分页、人工内容维护及单作品删除，菜单统一为作品管理；AI、私有资源、任务和阅读进度保持归属隔离。后端 Ruff 与 1069 测试通过，前端 ESLint、TypeScript/Vite 构建和 459 测试通过；三身份、桌面与 375px、明暗主题浏览器验收通过，命名调整另有 18 项针对性测试与构建通过。已知已授权同项目重新加载后的旧保存响应版本竞争保留，详见归档任务 final-check.md。用户明确授权提交归档，三组工作提交与任务归档已完成；另组书架四个并行文件保持未提交，未推送或合并主分支。

### Git Commits

| Hash | Message |
|------|---------|
| `77065635d4437aa2a3e19c2e901bb317f37978cb` | feat(admin): 支持管理员跨用户维护项目内容 |
| `e4e46edccae7cafb34051e893b86f43a4a645a6d` | feat(frontend): 增加管理员作品管理与人工编辑入口 |
| `22050b8087ffd62a211232d67df2a40bbc58986a` | docs(admin): 记录作品管理权限边界与验收结果 |

### Status

[OK] **Completed**


## Session 26: 管理员操作审计与受保护日期清理实现、验收及归档
<!-- trellis-session: v=2 fp=240751defa8a39a2 -->

**Date**: 2026-10-31
**Task**: 管理员操作审计与受保护日期清理实现、验收及归档
**Branch**: `codex/admin-audit-log`

### Summary

完成管理员操作日志，并按批准计划提交、归档与记录会话；保留原有无关改动，不推送。

### Main Changes

- 新增实例级审计、真实操作人和目标快照、变更摘要与已有版本关联、组合筛选和详情。
- 接入28种管理动作；成功与业务同事务，失败回滚后安全记录；修复版本预flush的409与原始SQL异常日志泄露。
- 实现日期预览/二次确认、purpose/actor/nonce/高水位保护，清理记录不可再次清除；新增管理员审计页及移动卡片。
- 同步7份规格，完成独立检查及隔离运行时验收；仅归档本任务，不处理其他七个规划任务。

### Git Commits

| Hash | Message |
|------|---------|
| `a19d9e366fb4176f8979b25b55965ada7c66364d` | feat(admin): 新增操作审计与受保护日期清理 |
| `fb272aa279817ff3d74938ed1173a7a054cdd7a1` | docs(admin): 固化审计契约与验收记录 |

### Testing

- [OK] 后端完整1102 passed、1条既有Gemini警告，实际exit0；前端64文件491通过；lint、类型检查与等价生产构建通过。
- [OK] 保留受限MCP本地bind EPERM及获批后完整重跑通过证据；构建使用临时缓存，未改既有目录权限或配置。
- [OK] 临时SQLite/FakeLLM零模型调用，31条加1条实际清理、防重放及删actor历史保留通过；桌面1280/375明暗、筛选/详情/清理预览取消和故障恢复通过。浏览器未点击最终永久删除，执行由隔离HTTP验证。

### Status

[OK] **Completed**

### Next Steps

- 未推送、未合并；部署前升级目标库至0023，并按部署需要补PostgreSQL并发实测。


## Session 27: 作品回收站实施、验收与归档
<!-- trellis-session: v=2 fp=465e65746f9738bc -->

**Date**: 2026-10-31
**Task**: 作品回收站实施、验收与归档
**Branch**: `codex/admin-recycle-bin`

### Summary

完成管理员专用作品回收站、30天保留、安全任务排空及有作品禁删号；本地门禁通过，工作变更已提交、当前任务已归档，用户追加授权合并main并推送。

### Main Changes

- 软删除与30天清理、管理员恢复/精确名称彻删、真实系统审计；0024迁移保留父子数据并保护破坏性降级。
- 持久删除意图与父项目准入屏障，等待真实任务执行及回调退出；last_deleted_at单调高水位与SQLite不复用项目ID共同防旧请求误删。
- 所有普通入口隔离回收站，RESTRICT及友好冲突阻止有作品删号；Key可见投影不丢隐藏授权，统一管理页与移动端提示。
- 新增回收站规范并同步跨层契约；仅归档当前任务，其他6项规划不动；4项原有无关改动保持基线且均未提交。

### Git Commits

| Hash | Message |
|------|---------|
| `10b23cae2c6280dd8b04e8eada65be153d8cc608` | feat(admin): 新增作品回收站与安全恢复 |
| `24807922aff358bd8fdc62e133f7e01e6b3fc49c` | docs(admin): 固化回收站契约与验收记录 |

### Testing

- [OK] 后端最终完整套件1143 passed（含21项loopback MCP测试），前端67个文件520 tests passed，最终测试后代码未修改。
- [OK] Ruff、ESLint、等价TypeScript/Vite生产构建及git diff --check通过；隔离HTTP和真实浏览器验收通过，零真实模型调用，临时服务均停止。
- [OK] 归档JSONL上下文引用验证通过；工作提交与归档提交暂存范围已精确核验。真实PostgreSQL未实测，标准npm run build仍受原有root-owned生成目录权限阻止，不宣称这两项通过。

### Status

[OK] **Completed**

### Next Steps

- 按用户明确授权快进合并到main并正常推送origin/main；本日志为归档后、合并推送前快照，实际结果以最终Git状态核实，不强制推送。
- 发布前在独立真实PostgreSQL补验带数据迁移、RESTRICT、父行屏障和恢复/清理并发；数据库先备份，新旧版本不混跑。
- 由环境所有者修复frontend生成目录写权限，再运行原标准构建命令；代码入库不等同于生产发布。


## Session 28: 管理员首页与全站统计交付
<!-- trellis-session: v=2 fp=605796fea4ba597f -->

**Date**: 2026-10-31
**Task**: 管理员首页与全站统计交付
**Branch**: `main`

### Summary

完整交付管理员全站统计、待办、趋势和历史分析；完成独立持久任务统计、UUID执行身份隔离和取消原子保存修复。两笔工作提交已快进合并main，任务已归档；按用户追加授权将工作和收尾提交普通推送，原有4项配置与技能改动保持不动。

### Main Changes

- 总量覆盖所有账号以及回收站作品、章节和字数；7/30/90天北京时间历史、无记录缺口和可关闭的60秒刷新。
- 独立任务累计不随裁剪或业务级联删除丢失；UUID贯穿数据库、调度、Hub和流，防复用整数ID的旧请求影响新任务。
- 定位并修复SQLite取消INSERT RETURNING残留写锁：有界结果保存保证提交前回滚、极晚提交获胜；补真实游标和提交屏障回归。
- 完成七段式规范和复盘、README、移动图表可读性修复；归档到 .trellis/tasks/archive/2026-10/10-30-admin-dashboard。

### Git Commits

| Hash | Message |
|------|---------|
| `415fc45e280fce00dc45ad2e792fffbab58ed49b` | feat(admin): 新增全站概览与持久化任务统计 |
| `f3de499b775332e9e0ef7354eba42cd0ef2ac9c7` | docs(admin): 固化首页统计契约与验收记录 |

### Testing

- [OK] 后端全量1211通过、exit0；唯一警告为既有Gemini SDK弃用提示。
- [OK] 前端71文件542通过；lint、两套TS类型检查及临时目录等价生产构建通过。标准npm run build受既有root所有目录权限限制。
- [OK] 全后端与0025显式Ruff通过，迁移3项通过；真实浏览器桌面/375移动/浅暗、权限、失败重试和刷新验收通过。
- [OK] PostgreSQL实例未实跑；没有将SQLite通过或官方镜像源码核对冒充生产容器验证。

### Status

[OK] **Completed**

### Next Steps

- 部署前备份并应用0025迁移；历史从首次初始化开始积累，不回填上线前数据。
- 补充PostgreSQL真实迁移与并发验证。
- 处理既有frontend/dist和node_modules/.tmp目录权限后验证标准npm run build。


## Session 29: 管理员全站任务监控交付与收尾
<!-- trellis-session: v=2 fp=ba84ced88b0f4096 -->

**Date**: 2026-11-01
**Task**: 管理员全站任务监控交付与收尾
**Branch**: `codex/admin-task-monitoring`

### Summary

完成管理员现存任务安全监控、持久取消受理与审计；后端1292/前端594测试通过，已归档，按用户授权合并main并推送。

### Main Changes

- 安全列表/详情、组合筛选、轮次时长与5秒刷新
- 持久取消受理、真实actor审计、资源/CAS/生命周期保护与0026迁移

### Git Commits

| Hash | Message |
|------|---------|
| `5247751af0927dea529aedb1488cc89eb795d088` | feat(admin): 新增全站任务监控与审计取消 |
| `a530e7b5b9716811e259cb1da8cbf51d07cd6187` | docs(admin): 固化任务监控与安全取消契约 |

### Testing

- [OK] 后端1292 passed、前端594 passed，lint/类型/等价生产构建/真实浏览器通过

### Status

[OK] **Completed**

### Next Steps

- 在可用PostgreSQL环境补真实行锁/并发及迁移实测；保留当前已说明的环境边界


## Session 30: 管理员系统设置交付与分支收口
<!-- trellis-session: v=2 fp=17be5abaf052e17e -->

**Date**: 2026-11-01
**Task**: 管理员系统设置交付与分支收口
**Branch**: `codex/admin-system-settings`

### Summary

完成系统设置六项持久热覆盖与安全审计，验证通过并归档，按用户授权合并main推送。

### Main Changes

- 六项设置、原子审计发布、公开开关同步与逐项恢复

### Git Commits

| Hash | Message |
|------|---------|
| `5a8a1bc6c37f077d6d0db2169dd91d8f8c0f45bc` | feat(admin): 新增系统设置与运行时配置覆盖 |
| `78ed9ba8458a346a546198cf062181b1e514f03a` | docs(admin): 固化系统设置契约与验收记录 |

### Testing

- [OK] 后端1332及最终定向40通过，前端634及浏览器通过；PG未实测

### Status

[OK] **Completed**

### Next Steps

- 快进合并main并推送origin/main，保留用户原有无关修改


## Session 31: 管理员用户详情交付
<!-- trellis-session: v=2 fp=0288555d2d551c57 -->

**Date**: 2026-11-01
**Task**: 管理员用户详情交付
**Branch**: `codex/admin-user-details`

### Summary

完成管理员用户详情、最近成功登录与分生命周期基础统计，复用账号动作和所属作品入口；通过最终质量门禁及真实浏览器验收，已按用户确认提交并归档。

### Main Changes

- 新增只读用户详情及正常/回收站/删除中作品、章节、正文总字数统计，保留原账号安全边界。
- 新增0028可空登录时间迁移；成功密码登录与注册自动登录原子记录，防并发撤销绕过及敏感SQL日志。
- 列表/详情共享声明式账号操作，目标/身份/请求代次失效；完善固定owner分页与深链，规范及验收证据已固化。

### Git Commits

| Hash | Message |
|------|---------|
| `84688d7` | feat(admin): 新增用户详情与最近登录统计 |
| `ee64804` | docs(admin): 固化用户详情契约与验收记录 |

### Testing

- [OK] 最终完整后端1350 passed、1 warning；前端84文件658测试、lint、两份TypeScript及等价生产构建通过。
- [OK] 独立Chrome/新临时SQLite/FakeLLM真实验收权限、统计、分页、归属、改密会话失效、删号、失败重试和375px明暗；7条真实actor审计、模型调用0。
- [OK] 保留先前沙箱本地bind限制及两次非稳定SSE超时原始结果；未改测试超时/顺序/种子，最终单轮全绿；PostgreSQL及键盘穷举未实测。

### Status

[OK] **Completed**

### Next Steps

- 部署前执行0028迁移；PostgreSQL验证和既有SSE偶发超时根因如需推进应另行明确范围。


## Session 32: 管理员角色权限与配额交付收尾
<!-- trellis-session: v=2 fp=dab24a235abac99e -->

**Date**: 2026-11-02
**Task**: 管理员角色权限与配额交付收尾
**Branch**: `codex/admin-rbac-quotas`

### Summary

完成内置与自定义多角色权限、管理目标保护和用户作品/后台并发配额；独立验收与规范同步通过，按用户授权提交并归档。

### Main Changes

- 新增0029迁移及33项可委派动作，保留唯一超管事实和owner-only隐私边界。
- 四作品创建入口原子准入，后台真实执行占用与设置发布协调；前端逐动作授权和撤权清理。
- 独立检查修复领取退出取消窗口、工坊隐私残留、只读子表单/移动动作及旧logout导航；暂存后补清2处纯EOF空白。
- 原有四项用户修改始终排除，未amend、未强制推送，任务已归档到2026-11。

### Git Commits

| Hash | Message |
|------|---------|
| `c9e4045` | feat(admin): 实现角色权限分级与用户资源配额 |
| `39133fa` | style(frontend): 清理权限工具文件尾部空行 |
| `2f2da1b` | docs(admin): 固化角色配额契约与验收记录 |

### Testing

- [OK] 后端全量1399通过；随后补充四入口竞争，concurrency17通过；ruff通过。
- [OK] 前端90文件693通过，lint/双tsc及隔离缓存和输出的等价原配置构建通过；标准缓存EACCES未改权限。
- [OK] 最终构建完成隔离浏览器关键流程及375px明暗验收；原173文件快照除两处LF清理外保持不变。
- [OK] 真实PostgreSQL按用户明确要求暂缓，未启动Docker或连接开发数据库；未将SQLite视为PG验证。

### Status

[OK] **Completed**

### Next Steps

- 按用户授权将本分支正常合并到main并推送origin/main；本记录写入时该远端步骤尚未执行。
- 生产发布前使用可清空隔离PostgreSQL补真实行锁竞争验证。


## Session 33: 交付管理员整库备份管理
<!-- trellis-session: v=2 fp=e21ec16b43961e3e -->

**Date**: 2026-11-02
**Task**: 交付管理员整库备份管理
**Branch**: `codex/admin-backup-management`

### Summary

完成可配置北京时间计划、本地/S3独立保留、可选GPG加密、SMTP失败提醒及分项授权，真实隔离恢复与浏览器验收通过；用户批准三批提交、收尾、合并main和推送。

### Main Changes

- 新增0030备份表及37项当前权限目录升级，保持0029历史33项、六项设置和作品JSON格式v1。
- 完成安全配置CAS/原子审计、持久幂等与调度、可恢复GPG私有目录、S3版本化保留和原生鉴权下载。
- 完成后台页面、同actor撤权清理、HEAD403权限刷新、375px/明暗主题，以及部署工具/恢复指南；保留.lithe/run/local.json原暂存。

### Git Commits

| Hash | Message |
|------|---------|
| `f3028c406b9d4a962ec5029ea6e9b9784e97bcbf` | feat(admin): 实现整库备份与独立存储管理 |
| `71087fb3b7d3b3d1a3e23e45e1c2cfa43a8365a5` | feat(frontend): 增加整库备份页面与分项授权 |
| `4f395f96868affd28ffaac6ceb4727e5723e90b5` | docs(admin): 固化整库备份契约与验收记录 |

### Testing

- [OK] 后端完整基线1526 passed；最终GET/HEAD元数据变更受影响API/OpenAPI60项及runtime36项通过；Ruff通过。
- [OK] 前端lint/build通过，95文件724项测试通过；真实浏览器4种动作角色/CAS/撤权/下载hash/375px及主题通过。
- [OK] PG16/SQLite真实all32项通过，覆盖6次恢复并启动目标、S3大文件multipart/独立保留、TLS SMTP/未知投递、崩溃及计划恢复；临时资源已回收。
- [OK] 最终镜像OpenAPI唯一编号及加密HEAD/GET烟测通过；后端73运行包审计0漏洞；前端保留14项既有moderate并记录可达性。

### Status

[OK] **Completed**


## Session 34: 交付设定按章节演变并完成审核修复
<!-- trellis-session: v=2 fp=bcfb68e4cf990cdd -->

**Date**: 2026-11-05
**Task**: 交付设定按章节演变并完成审核修复
**Branch**: `codex/setting-progressions`

### Summary

完成设定按章节演变（179 文件）并按审核结论修复三项问题；全量门禁通过后归档任务并记录会话。

### Main Changes

- 实现五类主体的章节级设定时间线、有效值回溯、分析回写与人工维护，含迁移 0031 与备份 v2。
- PATCH 设定变化禁止改分析来源事件的字段引用，并在写入边界捕获 IntegrityError 转 409；前端仅将 code=stale_revision 的 409 视为版本冲突。
- DELETE /relationships/{id} 增加可选 expected_revision 对时间线做 CAS，兼容智能体通用 CRUD 与旧调用。

### Git Commits

| Hash | Message |
|------|---------|
| `0ed55f7e0393c4af9e041c59762f1d21b40e0620` | feat(story): 支持设定按章节演变 |
| `576e632ec6e5a1dc0ef684211680ff1e5354236e` | chore(trellis): 添加 CodeBuddy 平台集成配置 |
| `5bac4e87c10f968264e5cdac7ffe500380e68fe5` | fix(story): 设定变化编辑的 409 兜底与冲突判定修正 |
| `1e98e01dac35db29bcdc8df49861ecd085a1b804` | fix(story): 关系删除补可选版本守卫 |

### Testing

- [OK] 后端全量 1793 passed / 1 skipped；关系相关 51 项、智能体工具 54 项通过；ruff check app tests 通过。
- [OK] 前端全量 109 文件 / 815 用例通过；eslint、tsc -b && vite build 通过。

### Status

[OK] **Completed**

### Next Steps

- 按用户授权将本分支合并到 main 并推送 origin/main；本记录写入时该远端步骤尚未执行。
- 生产环境用隔离 PostgreSQL 复核行锁竞争与浏览器交互（审核结论遗留项）。


## Session 35: 全文提及追踪、作品笔记与出场热力图
<!-- trellis-session: v=2 fp=ff5697e9ff95caaf -->

**Date**: 2026-11-05
**Task**: 全文提及追踪、作品笔记与出场热力图
**Branch**: `codex/mention-tracking`

### Summary

完成 TODOS 第 2 项及第 9 项基础笔记：四来源提及追踪、精确定位、正文标记和章节出场统计；父任务与两个子任务已归档，用户已授权合并 main 并推送。

### Main Changes

- 实现正文、本人项目聊天、独立笔记和知识库资料的名称与别名追踪；歧义单列、码点定位、旧原文 hash 失效提示。
- 新增作品笔记 CRUD 与版本冲突保护、世界观别名、0032/0033 迁移及备份兼容；接通草稿和 Reader 标记、详情提及、热力图与时间线。
- 同步正式规范和 TODOS，归档 mention-tracking、mention-index-navigation、project-notes；保留无关 pelican-cycling.html。

### Git Commits

| Hash | Message |
|------|---------|
| `366c0ed` | feat(story): 添加作品笔记与四源提及接口 |
| `560363c` | feat(studio): 接入提及跳转与出场热力图 |
| `2c0bb10` | docs(story): 记录提及追踪规范与验收结果 |

### Testing

- [OK] 前端 lint、TypeScript/Vite 构建及 115 文件 838 测试通过；后端 Ruff 通过。
- [OK] 后端原始全量 1819 passed、6 failed、1 skipped；4 项本机监听限制及 2 项旧 schema 断言经完整文件复验 21/11/7 passed，合计 1825 个不同用例验证通过；唯一 skip 为既有 PostgreSQL 环境条件。
- [OK] 真实浏览器验证四源定位、草稿保护、旧链接、热力图、375px/桌面、主题和键盘；550 万码点 SQLite 合成语料 HTTP 冷 0.233 秒、热 0.110 秒。
- [OK] 收尾未改产品代码；已核对 108 个工作提交文件，1020 个产品文件与收尾前快照一致，git diff --check 通过。

### Status

[OK] **Completed**
