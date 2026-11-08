# 正文标记产品与浏览器验证（实施中）

所有产品探针仅连接新建的 `/tmp/ainovel-marker-draft-proof-*` SQLite 库与本机 8766 服务；每次新注册随机测试用户／作品，不访问用户真实作品。浏览器为实际 Chromium，移动视口 375×812。以下证据不是全任务验收。

- `rich_marker_author_probe.js`：选取跨 CRLF / emoji 正文，新建蓝色标记，撤销／重做并保存；再对明确身份调整范围与颜色，保存、重载；核对 UUID、码点和逐字符正文。通过（早期同实现快照）。
- `rich_marker_newline_probe.js`：预置只覆盖 CRLF 的活动标记，通过“仅换行标记”找到、精确选中两码点、更改颜色并保存，原文无变化。通过。
- `rich_marker_unsaved_probe.js`：已保存标记内先输入 X、再跨 CRLF 删除；将服务端可校验的旧基线与客户端未保存草稿片段分别持久化，时间线真实展示并明确草稿未验证，可展开原有片段。撤销、保存、重做、再次保存验证基线更新与草稿片段保留。通过。
- `rich_marker_grouped_probe.js`：上述输入与删除合并为单个 ProseMirror 历史组，跨保存撤销／重做仍保存两份片段。通过。
- `rich_marker_long_probe.js`：标记 5000 码点时插入 emoji 增至 5001 仍活动；删除后基线 5000 码点完整，草稿片段在 CRLF 前截断为 4999 码点并标示截断。通过。
- `rich_marker_same_text_probe.js`：重复“甲”中选中第二字并替换为相同文字；原文仍相同，但标记失效并保留正确位置及摘录。通过。
- `rich_marker_crosssave_probe.js`：新建标记→保存→撤销→保存→重做→保存，原 UUID 依同景显式墓碑复活。通过（新增草稿字段后已重新运行）。
- `rich_marker_ime_probe.js`、`rich_marker_ime_cancel_probe.js`、`rich_plain_ime_probe.js`：分别检查有标记与无标记的 CDP 模拟组合输入、组合期间禁止保存、跨保存撤销／重做；选中标记后模拟取消时 Chromium 的选区确已删除，原片段仍作为失效记录保存。通过。
- `rich_editor_component_probe.js`：真实浏览器检查混合换行、选择替换、样式、粘贴及撤销重做；无纯文本的 HTML/图片粘贴不会删除选区。CDP 输入法是事件模拟，不代表各操作系统真实输入法覆盖。

服务端双片段由当前基线严格计算并校验 `excerpt`；`draft_excerpt` 只是作者客户端在未保存事务中的捕获，服务端不声称重放验证。旧数据的原有片段来源可能未知。备份和历史中均保留这两份信息。

未证明：区块相关的产品编辑器、区块内交互与一次性 AI 选区，所有浏览器/输入法、复杂并发及 PRD AC14 全流程。它们不能因为这些切片通过而宣称完成。
