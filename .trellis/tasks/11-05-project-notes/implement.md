# 笔记执行清单
- [ ] 阅读父prd/design/implement与本任务jsonl；不自行再派implement/check。
- [ ] 尽早完成model、0032、API/metadata/备份注册；通知主会话与M-B共享文件交接。
- [ ] CRUD/权限/CAS/备份定向失败测试 → 实现 → 通过；主要tests/test_notes.py，补有意义的迁移/备份覆盖。
- [ ] api/notes.ts、NotesPanel与草稿/冲突/定位辅助测试；前端类型放api/notes.ts避免争用api/types.ts。
- [ ] 给M-F说明route导出、id深链、定位接入点；不编辑router/ProjectLayout/Sidebar。
- [ ] backend uv run ruff check .，uv run pytest tests/test_notes.py及相关备份/迁移目标；frontend npm run lint、npm run build、相关测试。
- [ ] 记录变更/结果/待集成点到 implementation-log.md，回报主会话，不提交、不归档。
兄弟提及实现可并行，但完整四源验收必须等本任务稳定。最终全范围检查由主会话派trellis-check。
