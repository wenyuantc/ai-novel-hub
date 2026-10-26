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
