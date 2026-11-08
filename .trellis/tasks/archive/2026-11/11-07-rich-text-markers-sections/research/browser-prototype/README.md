# 浏览器富文本无损原型：阶段 0 记录

## 目的及边界

验证候选 Tiptap 3.33.0 / ProseMirror 编辑器能否在真实 Chromium 中保留旧正文的 CRLF、单个 CR/LF、连续空行、空格、tab、组合字符和 emoji，并将可见光标映射到 Python Unicode 码点。该目录是隔离原型，不是产品实现，不包含服务端、AI、持久化或最终安全规则。实测不能证明所有输入和所有浏览器的普遍正确性；对应永久回归仍需随功能实现。

## 2026-11-08 本机执行结果

- Tiptap `@tiptap/core`、`@tiptap/starter-kit`、`@tiptap/pm` 均使用 3.33.0；Vite 8.3.5；Playwright CLI 会话 `richproof` 使用真实 Chromium。
- `corpus_probe.py`：以固定随机种子生成 84 份正文，在真实编辑器里完成 Python 与 JS 码点比较、可见光标映射双向检查、357 次插字和原文核对；0 个失败。CRLF 内部的码点明确标为不可见光标位置，不猜测落点。
- `flow_probe.js`：26 项行为检查通过，包括 Backspace / Delete、Enter、撤销／重做、标题→列表转换、列表内 Enter、保留混合 CRLF 的文本粘贴、区块插入／拖动、禁止非显式删除及跨区块粘贴、标记删除后失效及撤销／重做同步、无效 schema 不覆盖、通过 Chromium CDP 注入中文组合事件及提交。CDP 模拟组合事件并非操作系统真实输入法的全面验证。
- 相同正式正文码点可对应区块前后两个不同编辑器光标（测试中分别为 PM 5／13）；必须随目标携带区块身份、同锚顺序或边界偏向，不能从单一整数反猜。
- 补充实测 `conversion_probe.py`：Chromium 中间插入 `甲 | 区块「备选」 | 乙`，实际生成两个空前缀段落；脚本只按该原型节点建立 `[1]` 零宽边界，不手工调整后调用后端转换，得到 `甲备选乙` 且无附加换行。对应正式 HTTP API 测试另覆盖普通区块、无存活结构的 CRLF 接缝，以及有依赖结构时的拒绝。**这是隔离原型到后端原语的桥接，不是产品编辑器端到端验收。**

## 实际发现与原型处置

1. 用 `separator_after` 存在前一个段落时，Backspace 合并 `甲\r\n乙\n末` 会错误得到 `甲乙\r\n末`。改为由后续段落持有 `separator_before` 后得到 `甲乙\n末`。
2. Tiptap 默认 Enter 会复制旧段分隔符；原型用自定义事务把新段前缀设为 LF，原始后续分隔符保持不变。标题转列表会重置属性，原型在仅格式变更且逐块文本未变时恢复前缀。
3. 在区块两侧保留零长度边界，插入区块时对分裂出的右段显式设置空前缀。默认连续 Backspace 可隐式删除区块；事务守卫禁止未明确授权的区块删除与跨区块替换。
4. 拖动句柄若沿用文本选区可能同时移动正文；原型在 `mousedown` 时先设置对应区块的 NodeSelection。
5. 默认 Markdown 输入规则会吞掉 `* ` 并自动变列表；原型关闭 input/paste rules。文本粘贴用受控 Slice 保留传入的混合换行；此原型仅处理纯文本粘贴，富格式剪贴板与区块跨边界操作仍须产品实现时设计并测试，不能声称已完成。
6. 标记元数据放入编辑器事务状态做原型，删除全文后追加失效状态与撤销／重做处于同一历史语义；仍须实现原文片段、跨段／跨区块约束、与服务端权威数据之间的完整同步。

## 可复现方式

将本目录 `index.html`、`main.js` 复制到独立临时目录，在该临时目录安装明确版本依赖并启动 Vite：

```bash
npm install --prefix "$TMP_DIR" --cache "$TMP_DIR/cache" --no-audit --no-fund @tiptap/core@3.33.0 @tiptap/starter-kit@3.33.0 @tiptap/pm@3.33.0 vite@8.3.5
"$TMP_DIR/node_modules/.bin/vite" --host 127.0.0.1 --port 4179 --strictPort
/Users/wenyuan/.codex/skills/playwright/scripts/playwright_cli.sh -s=richproof open http://127.0.0.1:4179/
python3 .trellis/tasks/11-07-rich-text-markers-sections/research/browser-prototype/corpus_probe.py
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv --directory backend run python ../.trellis/tasks/11-07-rich-text-markers-sections/research/browser-prototype/conversion_probe.py
```

将 `flow_probe.js` 的内容作为 `playwright_cli.sh -s=richproof --raw run-code` 参数即可重跑 26 项交互；手写 ClipboardEvent 和 CDP 输入是受控浏览器事件，不代表系统剪贴板／真实 IME 的所有平台实现。
