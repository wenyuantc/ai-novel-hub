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
