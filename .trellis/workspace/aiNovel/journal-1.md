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
