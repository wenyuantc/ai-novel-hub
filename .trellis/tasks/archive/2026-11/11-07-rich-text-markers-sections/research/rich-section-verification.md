# 区块编辑与回归证据（实施中快照）

本文件记录可复现的阶段性结果，不代替最终质量门禁。以下产品浏览器探针使用独立临时 SQLite（`/tmp/ainovel-section-product-kwWbCwRd/app.db`）、0040 迁移、127.0.0.1:8767 和实际 Chromium，非用户作品。页面为 375px；每个探针创建独立测试账号和作品。

- `rich_section_product_probe.js`：实际产品中创建同锚区块、名称和逐块 AI 开关、区块内标记、保存和刷新；折叠后从时间线定位并展开；键盘上下移动与首次原生拖动；区块内粗体转换成正文、原正文 CRLF 保留，转换确认明确提示撤销栈及版本恢复，并验证转换前版本完整含区块/格式/标记；删除后跨保存撤销/重做仍保留身份。最新 375px 复跑通过。先前首次拖动失败系测试在按下鼠标后才自动滚动目标，预先滚动后重跑通过；无需修改节点实现。
- `rich_section_create_crosssave_probe.js`：新建区块→保存→撤销→保存→重做→保存，保持原身份，默认 AI 关闭，旧混合换行不变。通过。
- `rich_section_ime_product_probe.js`：CDP 模拟跨 CRLF/emoji 区块标记的组合输入，组合期间禁止保存，完成后标记失效并保留原文；跨保存撤销/重做。通过。CDP 模拟不代表所有操作系统与真实输入法覆盖。
- `rich_section_conflict_product_probe.js`：两页同时编辑同一区块，先保存者成功，后保存者收到 409 且保留本地草稿，不改先前提交。通过。
- `rich_section_marker_relink_probe.js`：将已保存的失效正文标记显式重关联至区块内 Unicode 选区，核对目标 UUID 和码点。通过。
- `rich_section_cross_chapter_probe.js`：跨章跳至区块标记前保护并保存当前草稿，目标章按区块身份定位所选文字。通过。
- `rich_section_move_cursor_product_probe.js`：在 emoji 后放置正文光标，以键盘触发“移动到正文光标”，区块精确移动并保留正文 CRLF、区块及标记身份。通过。
- `rich_review_identity_probe.js`：独立 Chromium 与可控 HTTP，验证旧账号迟到预览、同账号会话代次变化后的迟到采纳、任务整数 ID 复用、采纳及后续刷新失败的冻结，以及身份复验成功路径。通过；并不冒充真实后端提案测试。
- `rich_section_editor_experimental_probe.js`：实际 Tiptap/Chromium 的实验性节点和事务门禁，覆盖正文与区块分别输入、混合换行粘贴、跨区块删除拒绝及模拟保存基线的历史撤销/重做。通过；产品链路以其他探针为准。
- 正文现有 `rich_marker_unsaved_probe.js`、`rich_marker_crosssave_probe.js`、`rich_block_identity_probe.js` 在相同独立新服务和当前前端构建上复跑通过。

`richSectionProjection.test.ts` 的固定种子样本覆盖 500 组混合 Unicode、换行、交叉样式、标题和区块；另有 1.2 万行、1000 区块、6000 样式的有界性测试。`richSectionMarkerMapping.test.ts` 包含 1000 个不同区块标记共用一次事务端点扫描的回归。它们不是对任意长度设备内存的保证。

主会话另用专用 SQLite、真实 FakeLLM 和真实浏览器验证 AI 来源过滤、整章候选、格式确认、阅读/导出/备份及权限竞争，其证据独立记录在同目录 `real_browser_*_probe.js`。源码之后变化时需重新构建并复验；PostgreSQL、全量测试及收尾由主会话统一负责。
