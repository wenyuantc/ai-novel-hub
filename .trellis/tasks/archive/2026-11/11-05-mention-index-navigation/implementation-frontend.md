# 前端实施记录

日期：2026-11-05。责任：提及公共组件、四来源集成、世界观别名与导航；统计页由独立统计工作者实现，见 implementation-stats.md。

## 交付

- `frontend/src/api/mentions.ts`：与父 design 一致的 catalog/preview/list/summary/source-window 合同，带取消信号；共享实体、来源、码点范围与统计 DTO。
- `utils/mentions.ts`：校验原文定位参数、四来源稳定链接、码点文字切段（一次展开码点，保留 CRLF/空白及 emoji，避免逐段反复扫描全文）。
- `hooks/useMentionQuery.ts`：结果绑定作品、授权快照、查询和刷新代次；中止/迟到响应失效。`useEntityDeepLink.ts` 仅消费条目 id，沿原页面编辑入口打开。
- `components/mentions/`：MentionText 使用 React 文本和键盘按钮；歧义称呼显示候选；EntityPreview 原页只读设定；MentionPreview 基于当前草稿并校验定位 hash；SourceMentionFocus 展示服务端原文窗口；EntityMentions 按四来源筛选和分页，支持统计页固定正文来源。
- `ChapterEditorPage.tsx` 保留原 TextArea/草稿/选区逻辑，增加可切换标记视图；AI 流期间仍显示流式文本，标记不写正文。
- `ReaderPage.tsx` 标记模式直接保留原文及原文偏移，普通阅读模式保持原分段逻辑；有效提及链接优先于进度恢复。
- `CharactersPanel.tsx` / `WorldPanel.tsx` 支持条目深链和提及 Tab；提及内容不放在禁用的编辑 Form 内。WorldSetting 别名加入表单、基础元数据比较及 expected_revision PATCH，既有基础/剧情值不混写。
- `AgentPanel` 选择链接会话；`AgentTimeline` 定位消息卡并暂停自动粘底；`AgentConversationView` 通过原文窗口高亮 Markdown 消息的源文本片段，避免把源偏移套到渲染 DOM。
- `KnowledgePanel` 根据稳定文档 ID 打开原文定位 Drawer，不依赖可重建的 chunk ID。
- 笔记工作者交接后修改 `components/notes/NoteEditor.tsx`：链接打开对应笔记的标记预览，当前草稿与已保存 hash 不符则提示失效，切回编辑保留草稿。
- 注册 notes/mentions 懒加载路由、共享项目菜单和 notes/chat 刷新资源类型。

## 验证

1. 定位测试先红：缺少 utils/mentions 时 Vitest 明确失败；实现后通过。
2. 新测试覆盖：emoji 与窗口绝对偏移、原文不丢字、非法/过期参数、链接不含原文、草稿仅预览与取消信号、筛选/指纹 HTTP 合同、React 安全文本、候选不擅自归属、别名去重/清空/版本冲突。
3. 全套初次暴露 `worldReadOnly.test.ts` 缺少 Router 上下文。新增 StaticRouter 包装与 canReadNow 测试桩，保留原先全部只读/待校准断言；未跳过或删除测试。
4. `npm run lint`：通过，exit 0。
5. `npm run build`：通过，TypeScript 与 Vite 完整构建；入口约 300 kB，新增页面保持按需加载。
6. `npm test`：115 个文件、837 个测试全部通过。Node 打印既有 localStorage 实验性环境提示，没有测试失败。
7. `git diff --check`：通过。

## 集成边界与后续

- 主会话已在隔离环境准备真实四来源数据并负责浏览器验收；本记录不把静态渲染/单元测试当作真实滚动和草稿交互验收。
- 未修改统计工作者文件、笔记后端或提及后端；未提交、未归档。
- 首版按父 design 预览接口上限 500000 码点；超限请求展示服务端错误，不静默截断正文或将部分计数标为全量。
