# 提及统计前端实施记录

日期：2026-11-05。责任：M-S，仅统计页、热力图、统计展示工具及测试；未改其他工作者的 API、共享提及组件、路由或笔记文件。

## 已实现

- `frontend/src/pages/project/MentionsPanel.tsx`：条目类型、精确条目、名称/别名、起止章筛选；20 条目分页；频次、歧义数、提及章数、最近提及与连续未提及统计。50 章可视窗口内提供热力图、单条目时间线和逐章数值表。
- `frontend/src/components/mentions/MentionHeatmap.tsx`：语义表格、主题色密度、可读数值与歧义标记、未写章节状态；条目名与命中格可用键盘打开提及列表。表格只在自身容器内横向滚动。
- `frontend/src/utils/mentionStats.ts`：按稳定章节 ID 展开稀疏结果、50 章窗口、时间线筛选、可访问标签、章节区间校验和服务端查询构造。
- `frontend/src/utils/mentionStats.test.ts`：9 项有意义用例，覆盖 ID/类型区分、稀疏与不连续章号、零值/歧义/空章、分页边界、查询筛选与非法区间。

## 接口与状态

- 复用 M-F 的 `mentionApi`、DTO、`useMentionQuery`、`EntityMentions`，没有第二套请求/定位协议。
- 已与 M-F 和主会话确认 `EntityMentions.sourceType?: MentionSourceType`；统计页始终传 `chapter`，指定格子还传同一章节起止范围，避免混入聊天、笔记或资料。
- 统计页内层以作品与授权快照为 key；请求 hook 同时绑定参数、作品、身份并中止旧请求；订阅章节、角色和世界观变化刷新。
- 频次表明确对应完整筛选范围；图表、时间线与逐章表明确仅为当前 50 章窗口。不把未知/空章变成确定零，也不把歧义并入确定提及。

## 验证证据

1. 先添加统计工具测试，再运行定向 Vitest：按预期因缺失 `mentionStats` 模块失败；实现后 9 项全部通过。
2. `npx eslint src/pages/project/MentionsPanel.tsx src/components/mentions/MentionHeatmap.tsx src/utils/mentionStats.ts src/utils/mentionStats.test.ts`：最终退出 0，无错误/警告。已修复首次检查的 `entries` memo 依赖警告。
3. `npm test -- --run src/utils/mentionStats.test.ts src/theme/noHardcodedColors.test.ts`：2 文件、12 测试通过。
4. `npm run build`：退出 0；统计页独立懒加载产物约 13.8 kB，无超阈值 chunk。修复上述 memo 警告后再次 `npx tsc -b`，退出 0。
5. 新增文件空白检查无尾随空白或缺失末尾换行。

## 后续集成验证

- 真浏览器的格子→原文、身份切换、窄屏/暗色/键盘验证由主会话统一进行；本记录不将静态类型/单元测试冒充浏览器完成。
- 完整前端/后端门禁与跨来源验收仍由主会话协调。未提交、未归档。
