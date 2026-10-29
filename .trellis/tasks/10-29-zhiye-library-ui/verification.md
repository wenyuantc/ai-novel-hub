# 知页官网、认证页面、外壳与书库验收

官网、首页登录/注册欢迎弹窗、完整登录与注册页、外壳和书库均已实现并验收。用户提供的三张认证截图已逐一对照，并明确授权「提交归档」。本轮按UI代码、规范验收记录的顺序创建本地提交，再完成Trellis归档与日志，不推送远程。

2026-10-29，按 DESIGN.md、theme.css、Shell.tsx、UI.tsx、_layout.tsx、library.tsx 及实际 Library 组件实现，并补充 frames/index.tsx、frames/login.tsx、CoreScreens::Login。直接目视对照设计目录书库/官网截图及用户提供的欢迎弹窗、登录与注册三图。

## 结果

已完成公开 `/` 官网首页、认证入口与 `/login` 登录注册页、公共主题、外壳、基础组件和 `/projects` 书库；使用 React18 / Ant Design5 / React Router6 / Zustand 和既有 API，不修改业务后端。无 Tailwind，设计 token 转为 CSS 变量。Fraunces/Geist 为本地 Fontsource 包，颜色仍集中在 palette，未放宽颜色检查。

本轮认证补齐后，实现代理和独立 trellis-check 分别运行 `npm run lint`、`npm run build`、`npm test`，全部exit0；54个测试文件、417项测试通过。最新入口169.29kB，首页独立JS15.66kB/CSS21.79kB，装饰图12.10kB，最大vendor仍低于1500kB门槛；关系图保持独立chunk。此前外壳/书库/官网也已分别检查；主会话最终 `git diff --check` 通过。

提交前另由独立 trellis-check 完成全部任务范围的最终复核，覆盖主题/外壳/项目布局/书库/官网/认证，未发现产品阻塞问题；新鲜lint/build/54文件417项测试全部通过。复核发现的旧顶栏导航文档描述已同步为 `StudioNavigation` 共用侧栏/移动导航；本轮未新增产品改动。

## 真实接口与交互

默认开发服务为5173，仍代理原有8000后端；最终检查时8000已停止响应，未修改其配置或数据库。可用联调预览为 `http://localhost:5174/`，连接单独的8001真实后端与临时SQLite验收库。测试账号/作品通过真实认证、作品、章节及封面接口保存；不在应用中添加假数据，不修改用户现有数据库，不触发AI生成。

### 公开首页与认证

- 未登录直接访问 `/` 显示完整官网，无私有外壳、作品或任务列表；已登录进入 `/` 也保留官网，并将入口换成「进入空间」「新建作品」。书库侧栏品牌可返回官网。
- 已登录首页新建按钮进入 `/projects?create=1`，打开共用真实立项表单；取消后清掉参数，无额外作品写入。
- 开放注册时首页按钮先打开注册欢迎弹窗，其「创建账户」进入 `/login?tab=register`，注册标签及确认密码字段可见；登录按钮打开登录欢迎弹窗，其「去登录」进入 `/login`。仍调用原认证逻辑，未另造注册流程或体验账号。
- 未登录访问真实章节 `/projects/3/chapters/9` 先到登录页；切到注册再切回登录，以隔离账号通过真实接口登录后回到同一章节，验证 `state.from` 未丢失。
- 用隔离后端的 `REGISTRATION_ENABLED=false` 实测：首页按钮全部改为「登录开始创作」；直接输入 `/login?tab=register` 仅显示登录表单与「注册已关闭」；以既有隔离账号提交仍正常登录。随后已恢复原配置。
- 创作能力右方向键切到角色并转移焦点，End到正文、Home到灵感；面板文本同步变化。点击科幻书册切换文案与选中态，方向选项卡右方向键切到奇幻并转移焦点。
- FAQ第二项展开会收起第一项，再点能折叠；导航 `#features` 实测区段顶部为0，页脚回顶部链接有效。
- 深浅主题共用现有 store；暗色官网背景/文字实测为 `rgb(32,40,35)` / `rgb(229,234,223)`。发现品牌 hover 被 AntD 全局颜色覆盖后显式使用主题前景色，复验 hover=true 时品牌为 `rgb(229,234,223)`。
- 最终官网健康重载后控制台 error 列表为空。首页未请求模型生成或用户私有项目，题材装帧和稿纸文案均为明确标注的公开示意。

### 认证视觉与交互补充

- 桌面弹窗实测宽460px、top112px、遮罩hero色60%；打开时body overflow为hidden。Tab从末尾按钮继续仍在dialog内；Escape及点击遮罩均关闭，焦点回到首页「登录」，背景滚动恢复。
- 次入口「先看看创作方向」关闭弹窗后，焦点为 `#stories`，区段top为0，无私有外壳或项目加载。原型体验账号信息块改为真实产品说明。
- 1440×900登录与注册实测双栏737.555/702.445px（1.05:1），表单380px；左侧为原稿静态山海插画与242×340px倾斜书册，右侧有对应标题/说明、标签、表单和实例账号帮助。
- 注册空提交显示用户名、密码、确认密码三项必填；两次密码不同显示「两次输入的密码不一致」。以已存在的隔离账号提交注册，真实接口返回「用户名已存在」，Alert可见；本轮未新增注册账号。
- 登录错误密码经真实接口显示「用户名或密码错误」；修正密码登录成功。从真实受保护章节开始，切注册再切登录、经历错误后仍回到 `/projects/3/chapters/9`，未丢失返回目标。
- 独立审查确认提交中字段、主按钮和两个标签均禁用，onChange/submit有忙碌保护；没有改动认证store/API或使用假成功提示。
- `REGISTRATION_ENABLED=false` 时，首页所有创作按钮打开登录欢迎弹窗；直接 `/login?tab=register` 显示「回到你的故事」、仅登录标签及注册已关闭说明，既有隔离账号仍能登录。验证后已恢复注册开启及浅色偏好。
- 已登录首页「新建作品」仍直达真实共用表单，取消后不增加作品。匿名品牌仍可回公开官网。
- 深色认证页纸面背景 `rgb(40,48,42)`，表单输入背景 `rgb(41,50,44)`，文字及品牌 `rgb(229,234,223)`；欢迎弹窗、注册页也已目视检查，无固定白色块。窄屏隐藏装饰书册，保留品牌与说明。
- 登录和注册均实测320/375/768/1024/1440×900px：文档clientWidth与scrollWidth均等于视口宽度，两侧区段无溢出；320/375px表单宽280/327px。欢迎弹窗320/375px宽288/343px、top24px，其余宽460px/top112px，内容clientWidth=scrollWidth，无横向溢出。
- 最终已恢复注册开启与浅色偏好，认证页健康重载后的控制台error列表为空；预览保留在 `/login`，临时视口尺寸已还原。

### 书库与原有操作

- 真实4部作品、8729字，与接口一致；状态、章数、时间、章节标题和封面均实际读取。
- 题材“科幻”匹配《萤火档案》，标签“成长”匹配《雾港来信》；无匹配有清除筛选；连载中筛选2部，按字数4214/2838倒序；统计始终是全书库统计。
- 书架/列表切换及状态、排序在刷新后保持。普通账号首次为空时显示0作品/0字，无继续条，无管理员入口；加载有Skeleton与说明。
- 手机侧栏打开共用新建表单，必填校验有效；真实创建成功，简介/主题/20000目标字数经GET复核已保存，进入原有创作向导。
- 真实TXT导出包含4章、4332字符，内容读取验证后清理本次下载的验收文件。
- 备份弹窗读取到4条章节版本和真实体积；恢复入口打开原有JSON导入表单。未向真实库执行导入或删除。
- 侧栏后台任务打开现有Drawer，进行中计数与接口均为0；账号菜单的管理员入口按权限显示。
- 最近写作跳到真实 `/projects/3/chapters/9`；项目目录仅一个侧栏且章节高亮；进入阅读器后公共外壳不渲染，正文加载正确。
- 浅/暗主题切换、刷新持久化以及跟随系统入口有效；深色纸面/文字变量分别为#202823/#e5eadf。键盘Tab能到视图按钮，焦点为可见solid 3px轮廓。
- 在隔离库暂时停用作品表产生真实HTTP500，页面显示“服务器内部错误”及重试；恢复表后点击重试成功加载。没有把请求错误显示成空书库。

## 响应式与修复

首页1440×2400整页与原图的区段高度一致：页头88、首屏560、能力横带72、功能区480、流程288、方向示例360、FAQ400、页脚152px。375px整页已目视检查，首屏插画、功能/流程/FAQ纵排，导航换行。320/375/768/1024/1440px实测文档clientWidth与scrollWidth分别同为315/370/763/1019/1435px（视口各含5px滚动条），所有顶层区段均无横向溢出。书本插画与背景装饰只在自身裁切容器内溢出，主内容完整可见。

书库实测：

| 视口宽度 | 列数 | document.scrollWidth | 页面/工具栏横向溢出 |
|---|---|---|---|
| 320 | 1 | 320 | 无 |
| 375 | 1 | 375 | 无 |
| 768 | 2 | 768 | 无 |
| 1024 | 2 | 1024 | 无 |
| 1440 | 4 | 1440 | 无 |

1440×900下，侧栏216px、顶栏56px、书封272×174px。375px导航为左侧抽屉，选择链接后关闭，模型配置页也完成窄屏抽查。

独立审查发现最近作品名为200位无空格英文时，继续条宽338px而scrollWidth为1687px。修复p单行省略并以title保留全文；复验继续条clientWidth/scrollWidth均338px，title仍为200字符。

最终健康重载后的书库无控制台error。此前已记录既有React Router升级提示和ProjectInfoFields的InputNumber.addonAfter弃用告警；不在本轮改造无关业务控件。开发期间Vite曾返回旧转译模块，重启自启的预览服务后恢复，最终构建与浏览器均已检查。

## 与设计稿的差异

| 设计稿 | 本次实现及原因 |
|---|---|
| 官网示例作品书名、作者及预览作品 | 用明确标注的题材方向书册和示意文案替代，不冒充真实用户作品或模型生成结果；点击切换方向，不进入虚构作品id。 |
| 官网固定「开始创作」及体验入口 | 按真实认证状态和注册开关显示对应欢迎弹窗或新建入口，创作台仍需要登录；欢迎说明介绍产品，不显示体验账号或匿名私有作品入口。 |
| 官网首屏白色手稿、图标和装饰 | 浅色保留纸面，深色跟随现有主题；使用CSS插画与项目AntD图标，品牌及部分图标轮廓、手稿文案与原稿不同。 |
| 认证页书册显示示例书名/作者、密码限制较简略 | 保留原图装饰插画与双栏视觉，书册改为「故事初章/装帧示意」；密码说明按至少6字符、至多72字节准确显示。手机收起装饰书册。 |
| 固定示例作品、作者、章节、统计与任务数 | 全部改为现有接口与账号数据；无已写章节不构造继续条，无卷名接口不填写示例卷名。书封署名沿用现有导出规则取当前账户。 |
| 顶部90px裁切插画与文字组合封面 | 已上传封面完整contain显示，宽高比不同会留空；无封面为本地几何纹样/文字装帧，不使用原型插画冒充用户封面。 |
| 仅筛选/搜索/视图控件 | 额外保留原排序和目标字数进度；工具栏搜索比原图更靠左。状态沿用“连载中”，原图为“创作中”。 |
| Lucide图标，包括品牌打开的书本 | 使用项目已有Ant Design图标，部分轮廓不同。 |
| 单击明暗图标、独立账号/任务/新建/导出备份页面 | 主题支持浅/暗/跟随系统菜单；其他入口沿用既有菜单、Modal/Drawer和真实路由，未增加原型页面。 |
| 项目智能体位于公共顶栏 | 保留现有AgentDock行为，入口仍在项目内容区顶部；这里只适配56px外壳尺寸。 |
| 1440×900桌面画板 | 补充移动菜单和自适应列数，窄手机为单列；长书名省略，提示保留完整内容。 |

本轮根据用户补充已完整重做登录/注册展示层；编辑器、阅读器和其他业务页内容保持原范围。原有书卡的独立标签行按设计改为简介，标签搜索保留。

## 截图

截图使用隔离验收数据，存于本机可访问目录：

- [首页登录欢迎弹窗](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-entry-desktop.jpg)
- [新版桌面登录](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-login-desktop.jpg)
- [新版桌面注册](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-register-desktop.jpg)
- [新版暗色登录](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-login-dark.jpg)
- [手机暗色注册](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-register-mobile-dark.jpg)
- [手机暗色欢迎弹窗](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-entry-mobile-dark.jpg)
- [新版关闭注册](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-registration-disabled.jpg)
- [认证页响应式实测](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-responsive-checks.json)
- [弹窗响应式实测](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/auth-dialog-responsive-checks.json)
- [官网桌面整页](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/home-desktop-light.jpg)
- [官网桌面首屏](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/home-desktop-hero.jpg)
- [官网移动整页](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/home-mobile-light.jpg)
- [官网深色](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/home-desktop-dark.jpg)
- [关闭注册](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/registration-disabled.jpg)
- [官网响应式实测](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/home-responsive-checks.json)
- [桌面浅色](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/library-desktop-light.jpg)
- [桌面深色](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/library-desktop-dark.jpg)
- [手机空书库](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/library-empty-mobile.jpg)
- [手机接口错误](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/library-error-mobile.jpg)
- [响应式实测](/Users/wenyuantc/.codex/visualizations/2026/10/29/01a186e8-316c-7910-976b-600dbc36a93b/responsive-checks.json)

## 提交状态

UI代码已提交于 `afd0eac`（main）；规范与本验收记录作为随后独立工作提交，完整工作提交哈希由Trellis会话日志记录。

用户在所有UI补齐并验收后明确授权「提交归档」。成果按 `commit-plan.md` 分为UI代码、规范与验收记录两个本地工作提交，随后Trellis脚本分别归档和记录会话。任务完成状态及提交哈希以task.json、开发日志和Git历史为准；未识别且未改动的 `pelican-bicycle.html` 不进入本轮提交，不推送远程。
