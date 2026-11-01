# 最终构建隔离浏览器验收

日期：2026-11-02（Asia/Shanghai）。本文件只记录主会话实际操作，不把Node/SSR或旧首构建当作当前浏览器结果。

## 实例与产物

- 实际前端：`/private/tmp/admin-rbac-check-frontend-dist`，入口 `/assets/index-C4bIyr3O.js`。
- index.html SHA-256：`ebf6c1235c98c12ab685fbc1be9d481e26074bbd099827834e3b7aee41aa4924`。
- 后端为checker冻结后的当前产品树，随后仅补四入口并发测试断言，没有产品代码改变；最终173产品/测试文件hash再次核对一致。
- 临时服务只绑定127.0.0.1:8781，PID22749，工具会话56953。脚本导入app前切全新临时目录并注入合成环境，不读开发.env，SQLite create_all + 显式初始化 + FakeLLM。**这里不是Alembic/PG迁移证明**，迁移另见后端报告。
- 10个账号均本次合成；Playwright专用headed Chrome会话admin-rbac-quotas，不使用用户登录凭据。桌面1366×900、移动375×812。
- 原始CLI快照/console在`/private/tmp/admin-rbac-quotas-browser/.playwright-cli/`；最终安全DB/审计摘要已存`browser-final-runtime-result.json`，没有Cookie/密码/密钥。

## 实际交互与断言

| 场景 | 真实动作与结果 |
| --- | --- |
| 匿名直达角色路由 | 无Cookie访问/admin/roles，跳login并保留from；super真实表单登录返回roles |
| 内置及在用保护 | 内置四角色edit/delete disabled；content13项、reviewer5项；已分配自定义角色delete disabled |
| 复制super | 真实点第一行“复制”，弹窗明确仅复制可委派权限；填写“浏览器验收-全部可委派”并保存成功，DB role10 system_key=null、权限33项 |
| 多角色分配 | 用户3详情真实多选新role10及审核员并保存，普通基线仍保留；DB user_roles含3/10。以用户3真实登录roles，显示“没有访问此管理功能的权限”，is_admin仍0，不因全部可委派权限成为超管 |
| 管理目标保护 | 同一非超管（全部可委派权限）访问content管理员详情，安全信息可读，重置/禁用/删除disabled，无配置配额/分配角色入口 |
| 配额真实占用/来源 | owner详情显示全部作品3：正常1/回收站1/删除中1；并发占用1、用户指定1、全站顶4，说明达到上限排队/超管不豁免 |
| limited0保存 | 移动配额弹窗将作品3改0并真实保存，表格显示占用仍3、用户指定0，已有作品不删。此轮未逐项操作inherit/unlimited选择，三态API/组件/后端边界由自动化门禁覆盖 |
| 配额失败保草稿 | owner真实登录新建，输入书名/一句话设定并提交，实际POST409：quota_exceeded/metric=project_count/used=3/limit=0；仍停留原modal且文本保留，无第四个作品、未进入向导 |
| 人工只读与快捷键 | projects.read账号直达他人章节，显示只读提示；标题/选角/保存/完成disabled。点击正文后真实键入“不应写入”、Meta-S，正文原文完全不变；重新进章节再Meta-S，requests只含合法GET，无PATCH/生成/输出/阅读进度请求。职业页新增disabled |
| 授权人工维护 | content_admin真实登录同章节，填写合成校对文本，Meta-S实际PATCH200；合成owner独立HTTP读取断言正文一致，字数26。requests无他人模型/私有风格/AI任务/生成日志/阅读进度写；DB reading_progress均保持空对象 |
| 任务真实排队 | 任务处置账号看到2任务，1 running/1 queued，符合owner task limit1；没有查询owner输出/私人配置 |
| 撤任务写权保读 | 打开任务1取消原因弹窗；独立super真实PATCH角色只留tasks.read，再切标签回前台。授权刷新后取消原因/所有取消按钮均消失，2任务列表与1运行1排队保留，仍登录 |
| 两阶段取消 | 恢复任务取消权并reload，真实点运行任务1，填写安全原因→下一步核对→勾选→明确提交；实际POST202，先显示已受理/尚未确认取消及审计33，再显示任务已取消。DB task1 cancelled、marker为空，排队task2随后succeeded |
| 撤任务读权 | 独立super把角色权限置空，切标签回前台；路由显示无权，rbac_owner/旧列表无匹配，真实actor菜单仍显示rbac_task_operator，没有跳login |
| 工坊隐私红→绿 | 旧首构建已复现红例（见browser-preflight）。本次reviewer先看到两匿名条目的真实rbac_owner及隐藏条目；仅本浏览器route工坊items为500，独立super撤reviewer角色后切标签回前台。合成500错误实际出现，但rbac_owner/隐藏条目均No matches，审核入口撤下，仍保持登录。unroute+reload后只有公开条目且“作者：匿名” |
| 移动与主题 | 真实移动菜单切暗色；375px角色页为可读纵向卡片，搜索/刷新/新建可用、身份/权限数/成员/更新时间/动作完整，浅暗截图均人工view_image核验。角色页与用户详情测document.scrollWidth=innerWidth=375，无页面横滚；人工编辑区暗色截图核验无溢出 |

## 截图

位于本任务可写可视化目录（未向仓库引入截图二进制）：

- `/Users/wenyuan/.codex/visualizations/2026/11/01/01a19725-3c1f-7f71-82f0-39348bf95fdd/rbac-roles-mobile-light.png`
- `/Users/wenyuan/.codex/visualizations/2026/11/01/01a19725-3c1f-7f71-82f0-39348bf95fdd/rbac-roles-mobile-dark.png`
- `/Users/wenyuan/.codex/visualizations/2026/11/01/01a19725-3c1f-7f71-82f0-39348bf95fdd/rbac-editor-mobile-dark.png`

## 错误分类与边界

- 控制台错误仅预期：匿名/auth/me401、注入工坊500、撤读期间旧轮询403、作品额度409；实际编辑成功页仅初始匿名401，没有React未捕获异常/前端warning。未声称全站每页无错误。
- 发生的CLI操作失败不算产品缺陷：作用域snapshot后旧ref不在当前快照、modal遮挡错误目标（未force）、zsh未引用带?的URL、旧版本CLI命令名network应为requests；均按新快照/实际命令重试，不用DOM脚本绕过控件。配额最后安全DB核对初误以reading_progress为NULL，实际默认为JSON空对象，修正断言后通过。
- UI仅实际覆盖表中路径；所有33项直接请求、关联Form外/组织/职业/移动菜单的实际回调零写、CAS/迟到logout/StrictMode、四入口原子竞争等由独立自动化测试证明，不把它们写成本次均逐页手工点击。
- FakeLLM最终2次调用恰为种子运行任务和取消后接续的排队任务；管理浏览器操作没有新增模型调用，工具/embedding均0。35条安全审计含真实rbac_task_operator取消受理和rbac_editor人工修改，均非超管。
- 全部故障route移除，额外标签关闭；专用Chrome仅本会话close，未close-all/kill-all。临时服务SIGINT正常停止exit130，finally安全结果写入完成；此退出码不是应用测试失败。
- PostgreSQL按用户要求暂缓；本轮未启动Docker、未连接/迁移开发数据库。部署前仍需隔离PG补真实行锁竞争。
