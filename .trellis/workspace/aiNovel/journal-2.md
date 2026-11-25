# Journal - aiNovel (Part 2)

> Continuation from `journal-1.md` (archived at ~2000 lines)
> Started: 2026-11-24

---



## Session 62: C4 跨进程任务流转发与断线回放
<!-- trellis-session: v=2 fp=d7d24be043dda04b -->

**Date**: 2026-11-24
**Task**: C4 跨进程任务流转发与断线回放
**Branch**: `codex/mp-peer-channel-stream`

### Summary

C4 十项验收通过，已在本地功能分支提交并归档。

### Main Changes

- 作者可跨进程订阅任务。协作者每帧写出前复核原授权，通道断开后按可验证的持久状态恢复。
- 跨进程读取进度、静默输出写回、任务与设置唤醒已接入；生产继续使用单个 Uvicorn worker。

### Git Commits

| Hash | Message |
|------|---------|
| `15778d0` | feat(tasks): 支持跨进程任务流转发与断线回放 |
| `3981070` | docs(trellis): 记录 C4 跨进程任务流设计与验收 |

### Testing

- [OK] SQLite 全套 7,416 项，7,367 项通过，30 项条件跳过，19 项预期失败。30 项条件跳过均在 PostgreSQL 16 实际通过。
- [OK] PostgreSQL 16 专项 121 项通过，前端 1,836 项通过。两进程 HTTP 与五组 Chrome 演练通过。
- [OK] 收尾核对了已验收代码哈希、归档链接和双进程演练脚本入口。

### Status

[OK] **Completed**


## Session 63: C5 引擎外状态跨进程协调
<!-- trellis-session: v=2 fp=31528fb9302d3421 -->

**Date**: 2026-11-24
**Task**: C5 引擎外状态跨进程协调
**Branch**: `codex/mp-peer-channel-stream`

### Summary

完成备份领取与失联恢复、灵感步骤互斥、项目导入加密暂存、智能体会话串行和周期维护，迁移头为 0069。SQLite 既有用例分组与复跑共 7367 项通过，C5 双库专项共 57 项通过。两进程 HTTP 演练验证了备份接管、灵感互斥和跨进程导入。C5 已归档。跨主机共享卷和多 worker 放量由 C7 验收。

### Git Commits

| Hash | Message |
|------|---------|
| `dccbb0b` | feat(backend): 支持引擎外业务状态跨进程协调 |
| `08fc00b` | docs(trellis): 记录 C5 跨进程设计与验收 |

### Status

[OK] **Completed**


## Session 64: C6 停止受理新任务的排空开关
<!-- trellis-session: v=2 fp=3c79b00759880cbd -->

**Date**: 2026-11-25
**Task**: C6 停止受理新任务的排空开关
**Branch**: `codex/mp-peer-channel-stream`

### Summary

C6 九项验收完成，已在本地功能分支提交并归档，父计划进度为 6/8。迁移头为 0070。

### Main Changes

- 设置保存后，所有进程立即停止受理新建、重试和继续。已受理任务及其派生继续执行。
- 系统设置页读共享排空状态，显示收尾、活动备份和存活进程。计划备份暂停受理，手动备份可用。

### Git Commits

| Hash | Message |
|------|---------|
| `018faa6a3792943b87c181a3f8fdd9e9f18f1b86` | feat(tasks): 支持全站后台任务排空 |
| `ff0352ab093fd5ec0f6bfa65554a64ca8ce88bce` | docs(trellis): 记录 C6 排空开关设计与验收 |

### Testing

- [OK] SQLite 全量与串行复验合计 7,430 项通过，54 项条件跳过，19 项预期失败。 PostgreSQL 16 专项 36 项、前端全量 1,849 项通过。双 Uvicorn 进程及 Chrome 验收通过。
- [OK] 收尾复验后端专项 26 项、前端相关 78 项通过。Ruff 和 Git 差异检查通过。

### Status

[OK] **Completed**


## Session 65: C7 多进程部署与迁移验收
<!-- trellis-session: v=2 fp=368b4508cf97ead1 -->

**Date**: 2026-11-25
**Task**: C7 多进程部署与迁移验收
**Branch**: `codex/mp-peer-channel-stream`

### Summary

完成并发迁移保护、多进程部署、真实进程验收与运维文档；C8 旧版回退和跨主机共享文件锁仍待验证。

### Main Changes

- 默认一个 worker；支持满足数据库、内网和共享目录前提的多进程与多实例部署。
- SQLite 与 PostgreSQL 迁移互斥、失败回滚和恢复流程已验证；OrbStack 默认 bridge 隔离不满足要求。

### Git Commits

| Hash | Message |
|------|---------|
| `e2eda468322a240e36fb6b36f7af17263c962541` | feat(deploy): 支持安全迁移与多进程启动 |
| `dbe7fc88490a4b31151f7a075e49806f2ee51d7d` | docs(trellis): 记录 C7 部署与验收 |

### Testing

- [OK] 后端完整回归 7519 通过、76 跳过、19 xfailed；PostgreSQL 迁移专项 22 项通过。
- [OK] 前端 lint、构建和 1849 项测试通过；真实部署与恢复演练见 C7 验收记录。

### Status

[OK] **Completed**

### Next Steps

- Daybreak 高深度扫描待 C7 分支推送后启动；C8 旧版升级与回滚独立推进。


## Session 66: C8 协作迁移及回滚演练
<!-- trellis-session: v=2 fp=95bd9f10bc08eac7 -->

**Date**: 2026-11-25
**Task**: C8 协作迁移及回滚演练
**Branch**: `codex/mp-peer-channel-stream`

### Summary

隔离演练 0043 到 0070 的迁移、排空、备份与快照回滚，并由独立执行者按 README 完成页面复做。

### Main Changes

- 更新升级操作步骤、后端规范和待办；记录旧任务及未决提案治理问题。

### Git Commits

| Hash | Message |
|------|---------|
| `a1ed1ad` | docs(trellis): 完成协作迁移与回滚演练 |

### Testing

- [OK] SQLite 后端全量 7519 通过、76 跳过、19 xfailed；SQLite 与 PostgreSQL 的 10 项场景复跑均通过。

### Status

[OK] **Completed**

### Next Steps

- 研究 TODO 第 34 项旧任务和未决提案的安全核对；目标部署需独立验证环境。


## Session 67: 多进程父任务集成核对与归档
<!-- trellis-session: v=2 fp=25424610f410df4e -->

**Date**: 2026-11-25
**Task**: 多进程父任务集成核对与归档
**Branch**: `codex/mp-peer-channel-stream`

### Summary

核对 C1–C8 的归档和父任务验收证据，整理既有规划资料并完成父任务归档。

### Main Changes

- 补齐父任务验收状态和归档引用，保留既有研究与边界。

### Git Commits

| Hash | Message |
|------|---------|
| `76b2889aa2a07247184658620bd064c5a6bc5299` | docs(trellis): 核对多进程父任务验收及归档引用 |

### Testing

- [OK] 归档后核对 33 条 Markdown 链接、10 条清单引用及 13 项验收状态；本轮未重跑产品测试。

### Status

[OK] **Completed**

### Next Steps

- 未排空旧任务及未决提案治理见 TODO 第 34 项；跨主机共享文件锁须在目标环境验证。


## Session 68: 个人写作统计与码字目标
<!-- trellis-session: v=2 fp=57ef8669cb40e6f2 -->

**Date**: 2026-11-25
**Task**: 个人写作统计与码字目标
**Branch**: `codex/writing-stats-goals`

### Summary

完成 TODO 第26项，按本人和作品区分手写与 AI 落稿，接入连载工作台。SQLite 全量、PostgreSQL 定向、前端测试构建及浏览器验收已记录。

### Git Commits

| Hash | Message |
|------|---------|
| `03bcb30` | feat: 新增个人写作统计与码字目标 |

### Status

[OK] **Completed**


## Session 69: 站内通知实施与验收
<!-- trellis-session: v=2 fp=8ca4849263873486 -->

**Date**: 2026-11-25
**Task**: 站内通知实施与验收
**Branch**: `codex/in-app-notifications`

### Summary

交付协作邀请、任务、预算与检查结果四类站内提醒；验证桌面与移动端、撤权、恢复后旧链接、并发入账和旧库升级。SQLite 全量 7575 项通过，前端 1865 项通过，PostgreSQL 专项 5 项通过。

### Git Commits

| Hash | Message |
|------|---------|
| `a24b807` | feat: 新增站内通知 |

### Status

[OK] **Completed**


## Session 70: 完成协作批注与备份恢复验收
<!-- trellis-session: v=2 fp=edb87e54704b7f52 -->

**Date**: 2026-11-25
**Task**: 完成协作批注与备份恢复验收
**Branch**: `codex/collaboration-annotations`

### Summary

TODO #28 已实现选区批注、回复、作者处理和失效定位。单书 v17 以及 SQLite 和 PostgreSQL 整库恢复通过。后端全量 7594 项通过，前端 1874 项通过，桌面与 375px 窄屏实测通过。

### Git Commits

| Hash | Message |
|------|---------|
| `70b93a3` | feat: 新增协作批注 |

### Status

[OK] **Completed**


## Session 71: 阅读页朗读与 AI 角色配音
<!-- trellis-session: v=2 fp=f665661b17b41555 -->

**Date**: 2026-11-25
**Task**: 阅读页朗读与 AI 角色配音
**Branch**: `main`

### Summary

完成本机中文朗读、角色音色、情绪和逐句试听，接入所选文本模型分析章节并保留手动纠正。前端 1929 项及后端相关 26 项测试通过，浏览器已验证真实发声；真实模型识别准确度及人工听感待评估。

### Git Commits

| Hash | Message |
|------|---------|
| `3720926` | feat: 新增阅读页朗读与 AI 角色配音 |

### Status

[OK] **Completed**
