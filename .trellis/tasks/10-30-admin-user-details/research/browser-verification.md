# 用户详情真实浏览器验收

日期：2026-11-01（Asia/Shanghai）。仅独立本地合成数据，不读写开发者数据库或真实用户凭据。

## 隔离环境与产物

- 127.0.0.1:8773，独立临时 SQLite、details_manager / details_alice / details_bob / details_empty 合成账号、FakeLLM。
- 脚本 `/private/tmp/admin_user_details_runtime.py`，导入默认应用前先切入空临时目录；本次进程 PID 27579。使用 `Base.metadata.create_all` 准备验收库，真实 Alembic 往返另由后端迁移测试验证，不混称。
- 前端为 `/private/tmp/admin-user-details-frontend-dist`，入口 `index-BZnQc0hy.js`，与实现代理和独立检查代理的构建一致。
- 缓存 Playwright CLI + 独立临时 Chrome 会话 admin-user-details。浏览器/本地监听经工具审批，不连接日常浏览器资料；CLI控制及本地API需审批权限。沙箱内直接 httpx 网络探测失败属于环境隔离，后续通过浏览器真实请求验证，不当作产品失败。
- 运行信息保留 `/private/tmp/admin-user-details-browser/runtime-info.json`；最终安全账号/审计快照见本目录 `browser-runtime-final.json`。

## 实际观察通过

1. 匿名直达 `/admin/users/2` 跳 `/login`；真实 UI 输入合成管理员用户名/密码后返回原详情页，标题正确。
2. 详情 GET 精确字段集合为 `id,username,is_admin,is_active,created_at,project_count,last_login_at,usage`，没有密码哈希、会话版本或私人配置。
3. Alice 的全部作品数24，正常22部/2章/10字、回收站1部/2章/10字、删除中1部/1章/3字，符合独立种子数据。删除中单列提示，不被算成回收站；其他 owner 的作品不混入。
4. 正常作品列表20条一页，点击第2页后仅剩空作品01与星港远航，列表总数仍22、用量总计仍22/2/10，不变成页内小计。
5. 从第2页点击“星港远航”进入 `/projects/1/overview`，真实提示“管理员正在管理「details_alice」的项目”。该次导航网络仅项目GET及favicon，没有他人模型配置、AI/任务输出或阅读进度请求；本次全部浏览器流程 FakeLLM 调用最终为0。
6. 列表→Empty详情展示全部零统计、明确“暂无记录”、正常作品空态。该账号由管理员创建，未伪造首次登录；禁用后正确密码登录403，读取其 last_login_at 仍为null。
7. 列表用户名和返回入口正常；同一会话在Alice/Empty间切换不显示旧目标。Empty重置弹窗打开后真实浏览器返回列表，dialogs从1变0且旧标题消失；前进回详情不复活旧弹窗。
8. Empty真实UI授予管理员、撤销管理员、禁用和启用成功，状态及操作按钮随响应刷新。非预期脚本选择器曾未识别AntD两个汉字的“禁 用”空格，读取实际快照后继续；此自动化定位超时未修改产品、不是产品失败。
9. Empty独立客户端用旧密码登录成功后，管理员真实UI输入两次新密码并重置：旧Cookie `/auth/me` 401、旧密码401、新密码200；管理员当前会话仍有效。刷新后最近登录变为真实时间。
10. Empty删号确认中，尾部空格和大写用户名均不能确认；精确用户名才可提交。删除成功自动回 `/admin/users`、dialogs=0、服务端目标GET404。最终审计中创建/四次状态角色变更/重置/删除均为真实 actor details_manager，未冒用目标身份。
11. Alice详情回收站“查看全部”进入 `/admin/recycle-bin?owner_user_id=2`；真实请求为 `/admin/recycle-bin/projects?owner_user_id=2&page=1&page_size=20`，展示种子的回收站作品，未出现其正常编辑链接。
12. 对详情GET注入受控500“合成详情读取故障”，界面撤下旧账号和危险动作，仅显示错误及“重试详情”；移除拦截再点重试，正确详情恢复。此为前端网络故障注入，不声称制造了真实数据库故障。
13. `/admin/users/999999` 显示“用户不存在”和返回核实说明；本人详情的重置密码/禁用/撤销管理员/删除均disabled。
14. 独立匿名请求详情401、合成普通账号请求403。Alice重置弹窗打开期间真实logout使Cookie失效，后续应用请求401自动跳登录，dialogs从1变0；无需刷新或全局destroyAll。
15. 在登录页真实输入普通Bob账号后，原管理员详情目标被守卫重定向公开首页 `/`，没有Alice详情标题/遗留弹窗；真实详情API仍403。
16. 1440×1000和375×812、浅色/暗色均截图并目视核验，documentWidth与viewport一致。移动端账号字段纵向、操作换行、作品为列表卡片；下部回收站/删除提示/作品列表可滚动到达。下部最终截图明确等待 `.ant-spin-spinning` 为0，不以加载遮罩下的灰暗样式冒充完成态。
17. 控制台仅出现预期匿名/过期401、404和注入500的资源错误，以及Chrome关于密码表单建议增加用户名的VERBOSE提示；未观察未捕获JS异常或新React告警。

## 截图

绝对目录：`/Users/wenyuan/.codex/visualizations/2026/11/01/01a19526-984c-7433-bdad-1bfc275b5c81/`。

- admin-user-details-desktop-light.png
- admin-user-details-desktop-dark.png
- admin-user-details-mobile-light.png
- admin-user-details-mobile-dark.png
- admin-user-details-mobile-lower.png

截图受工作区内部滚动容器影响，不声称单张覆盖整个长列表；下部另行滚动捕获。字段、统计、归属与实际变更通过浏览器可访问树/真实请求检查，不仅比较图片。

## 清理与验证边界

- 已关闭本任务Chrome会话，核对进程命令后仅向本任务27579发送SIGINT并停止8773服务；退出130/KeyboardInterrupt为人工结束隔离服务，不当作产品失败。未停止已有8000等其他服务。
- 后端并发认证/存储失败/迟到时间和迁移由自动化行为测试验证；前端双击/异步表单/StrictMode由controller行为测试验证，不冒称均手动操作。
- 没有PostgreSQL实例实测；没有用浏览器键盘穷举所有AntD快捷键。原有组件语义及SSR/标签检查保留。
- 后端最终完整测试门禁由检查代理另行记录，本报告不将分项重试、浏览器通过或准备脚本当作全套通过。
