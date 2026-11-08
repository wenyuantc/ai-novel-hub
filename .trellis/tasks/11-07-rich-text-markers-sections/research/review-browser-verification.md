# 候选核对组件浏览器专项（实施中）

- 2026-11-08：启动 `npm --prefix frontend run dev -- --host 127.0.0.1 --port 5178 --strictPort`，使用 Playwright CLI 会话 `reviewproof` 和真实 Chromium，运行同目录 `review_browser_probe.js`。
- 该脚本加载仓库内真实 `RichReviewModal` 与码点映射代码，但模拟提案读取、采纳与任务刷新 HTTP 响应。结果：**PASS**。候选 `甲\r\n🙂乙` 在只读 textarea 的可见光标落在换行后时映射为原始文本码点 3；不勾选格式损失不能采纳，勾选后发送的一次请求携带任务身份、作者指定位置和确认。
- 原型执行时最初发现测试夹具从不同 Vite 模块实例取了 `App` 导致通知上下文不匹配，修正测试夹具版本后通过；不代表生产问题已复现。
- 该证据不覆盖真实后端、刷新恢复、权限竞争、375px、暗色主题或完整编辑器流程，不能代替 AC14 端到端验收。
