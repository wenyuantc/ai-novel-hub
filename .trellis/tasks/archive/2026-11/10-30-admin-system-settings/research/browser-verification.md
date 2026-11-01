# 系统设置真实浏览器验收

日期：2026-11-01（Asia/Shanghai）。仅本地合成数据，不读写开发者数据库。

## 隔离环境

127.0.0.1:8769、每次新建的tmp SQLite、合成settings_manager/settings_reader和FakeLLM；前端等价生产构建/private/tmp/admin-system-settings-frontend-dist。Playwright CLI两个独立无头Chrome会话admin-system-settings与admin-system-settings-public，不访问日常资料。绑定/浏览器启动默认沙箱拒绝后均经工具审批。原有8000服务未触碰。

脚本/private/tmp/admin_system_settings_runtime.py先切入空临时目录，默认应用导入也不加载开发者.env。runtime-info.json位于/private/tmp/admin-system-settings-browser。第一次预览PID74253，最终数值边界冻结后用同一临时库冷启动PID80181；并非只刷新页面假装重启。

## 已观察通过

1. 匿名直达设置页跳登录；真实合成管理员登录后StrictMode页面正常加载，无永久Skeleton。六项/来源/部署/代码默认及无需重启说明完整，桌面/移动/账户/面包屑入口已接通。
2. 真实UI暂存注册false、MCP false、全局并发3、自动分析true、API限额90；保存前管理GET仍revision0、公开开关true/true，未提前产生副作用。保存后revision1、五项admin来源与实际值一致，单用户未改仍deployment2。
3. 保存后MCP真实接口403，管理员本页MCP导航立即隐藏；另一个此前已打开的匿名登录页未经手动刷新，30秒轮询后注册Tab消失并显示注册已关闭。
4. 逐项恢复注册先弹确认，点击加入待恢复后后端仍false；再点保存才恢复deployment true（revision2）。刷新后值/来源正确。
5. 另一客户端模拟先保存单用户并发3后，旧页面保存返回409；未保存global草稿5仍在，显示暂停保存与刷新核实，不静默覆盖。实际有效global仍3。
6. 恢复确认弹窗打开时dialogs=1；通过浏览器返回执行客户端路由离页，projects页面dialogs=0且无遗留恢复文字。验证本页confirm句柄修复，不以整页刷新掩盖问题。
7. 对本浏览器管理GET注入受控500，显示中文失败并保留旧有效global3；移除拦截再点击刷新，错误撤下、值保持正确。此为前端受控网络故障，不冒称真实DB故障。
8. 最终后端真实冷启动后GET200、revision3，五项覆盖/来源保持，注册沿用部署true。没有依赖旧进程的Settings对象。
9. 最终Python进程对API限额2147483648+注册false批次返回422、revision和注册值均未变；2147483647保存200且真实开放/me200；逐项null恢复120后开放/me仍200。合成Key仅在函数内使用、未输出，随后撤销；独立SQL确认revoked_at存在且key_enc=null。
10. 最终前端限额aria-valuemax=2147483647，任务并发保持原JS安全整数上限；未将API数据库上限错误套到并发。
11. 实际打开操作日志及恢复限额详情，真实actor为settings_manager，安全摘要展示2147483647后台覆盖→120部署及取消覆盖含义，无部署凭据/原始配置对象。
12. 1440×1000与375×812、浅/暗主题均截图并目视核验；375的documentWidth=viewport=375。通过滚动到真实限流输入检查下部保存/放弃/冲突状态可达，无页面横向溢出。

## 截图

绝对目录：/Users/wenyuan/.codex/visualizations/2026/10/31/01a192df-d16b-7f21-ba75-657760f0a945/。

admin-system-settings-desktop-light.png、desktop-dark.png、mobile-light.png、mobile-dark.png、desktop-lower.png、restore.png（均有admin-system-settings-前缀）。部分截图为最终上限修复之前未改变的界面，最终上限另有DOM核验。full-page仍受工作区内部滚动容器限制，下部另行滚动截图，未伪称一张图覆盖全部控件。

13. 恢复确认打开时dialogs=1，通过真实logout接口使合成Cookie失效，后续个人tasks轮询真实401；应用自动跳/login，dialogs=0且无当前有效值残留。尝试定位刷新按钮时页面已被401清理，自动化定位超时不视为产品失败；401控制台证据为/api/v1/tasks?active=true&limit=200。
14. 合成普通用户真实登录后直达管理设置被转回首页；真实管理GET403，无有效值/遗留弹窗。
15. 已关闭本任务两个独立Chrome会话并停止8769预览进程，保留临时合成库与证据，不触碰其他服务。最终库/审计状态见/private/tmp/admin-system-settings-browser/runtime-final-result.json；停止进程的工具Ctrl-C退出码130属于人工结束隔离服务，不当作产品测试失败。

## 验收边界

后端调度/自动分析/MCP连接和队列屏障/同分钟计数、提交确认丢失与DB恢复由独立自动化Gate/Event测试验证，不声称均手动操作。PostgreSQL未实测；完整测试门禁另见verification.md，首轮既有writeback超时不能记作通过。
