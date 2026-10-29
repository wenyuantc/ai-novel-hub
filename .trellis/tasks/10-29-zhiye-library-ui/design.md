# 技术设计

## 认证视觉补充（2026-10-29用户截图）
- 行为差距：首页登录仍直接跳转，LoginPage仍是旧居中Card。变更落在HomePage入口和LoginPage展示层，保留认证store/API与受保护路由。
- 首页入口由页面本地state管理login/register模式，欢迎弹窗用项目现有AntD Modal实现，460px宽、桌面距顶112px、32px内边距，窄屏缩进并限制高度。用真实产品说明代替原型体验账号面板，次级入口回公开创作方向，不绕过鉴权。
- LoginPage保留现有Credentials表单、共享规则、错误处理和state.from，改为原稿双栏：左1.05fr、右1fr，右表单最大380px。登录/注册标题与描述按tab变化；注册开关仍为服务端权威，提交中禁用冲突操作。
- 允许从只读设计assets复制mountain-story.jpg到项目静态资源，作为认证装帧装饰，配通用创作书名/示意标签；不能导入data.ts或复制假作者、作品id、账号。
- 文件边界：pages/HomePage.tsx/CSS、LoginPage.tsx/新增LoginPage.css；需要时新增components/auth/HomeEntryDialog及其局部样式、装饰资源，复用StudioBrand；若确需调整共享token只在palette集中定义。后端、认证store、路由守卫和其他业务页面不改。主会话只更新文档/验收，产品改动交实现子代理。

## 公开首页补充
- 新增 HomePage.tsx/CSS，直接使用现有主题与字体，参考 frames/index.tsx 和首页截图（1440×2400）。官网页不套216px工作区侧栏；保留现有书库和业务外壳。
- `/` 为公开首页，原 AppLayout 下业务仍由 RequireAuth 保护。首页不能因未知/匿名认证状态读取 projectApi，不将私人作品作为公共示例。
- 复用 `StudioBrand` 或等效唯一品牌组件，使官网页头/页脚及侧栏的首页链接一致指向 `/`。
- 首屏书本稿纸、创作能力示意、故事方向书册由CSS/语义DOM实现。示意文案是公开产品说明，不导入 HomeWorks/data.ts 的假项目对象，不带假作者、字数、章节、体验账号或伪造编辑器路由。
- 匿名登录/注册先打开对应欢迎弹窗，其主按钮进入 `/login` 或 `/login?tab=register`；关闭注册时不能选注册。已登录的开始按钮到 `/projects?create=1`，查看创作台/进入空间到 `/projects`，由用户选择真实作品。
- 认证中先避免错误的注册态闪现；注册开关只来自现有 `/auth/config`。首屏、功能切换、FAQ及示例切换适配键盘；窄屏重排、长文本换行、减少动画偏好均适配。

## 边界
实际变更归属为 AppLayout、ProjectListPage、theme 与 shelf。基础组件仅抽取本轮真实复用的 Page/Panel/Empty 等；表单/弹窗/下拉/焦点管理沿用 Ant Design。ProjectLayout 仅为共享导航做必要整合，保留项目加载/刷新、AgentDock 和业务 Outlet，不出现双侧栏。

## 主题
- palette.ts 是现有唯一允许颜色字面量的文件：在此定义设计明暗 token；字体/圆角可置 CSS。
- ThemeSync 将其映射为 --color-* 和已有 --app-bg/--app-text；ConfigProvider 同步主色、背景、文字、边框、语义色、字体、圆角与控件尺寸，避免 CSS/AntD 主题分裂。
- 保留 themeStore light/dark/system、系统监听、localStorage key；不降级颜色测试或扩大白名单。
- index.css 本地导入两个 Fontsource 字体；布局/书库样式限作用域，避免无关页面污染。

## 布局与导航
- 保留现有 /projects、/settings/ai、/prompt-templates、/styles、/workshop、/skills、/mcp-plugins、/inspiration、/book-import、/admin/users 等路径。
- 创作空间/创作资源定义一次，移动 Drawer 复用。真实用户名/头像来自 auth，任务入口复用 TaskPanel 和 tasks store，不伪造 /tasks 路由。
- 侧栏和书库新建打开同一原有表单，可使用 /projects?create=1 在现有路由触发，关闭时清理该参数。
- 如整合项目导航，复用 pid 路由和匹配当前项目的 store，不改变业务内容与智能体行为。

## 数据流
- projectApi.list → useLoader → shelfStats/filterAndSort → 书库。
- 继续写作选有 last_edited_chapter_id 的最近更新作品，按需通过 chapterApi.list 读取标题，失败降级为真实作品名，不阻断列表。
- 继续/阅读复用 continueTarget/readTarget；coverUrl 保留服务端微秒版本参数；真实封面保持完整展示，无图为文字装帧。
- 保留搜索/状态/排序设备偏好；视图偏好可增加容错解析。搜索补上 genre，同时保留 tags。
- 删除仍需原有确认与 remove；导出/备份/恢复沿用懒加载组件；创建保留原字段/验证/风格选择。

## 兼容与回滚
Ant Design 图标替代 Lucide；真实内容和无封面占位自然不同。为保留排序/阅读/备份可比原型多少量控件。全局主题需抽查其他业务路由。回滚主题/布局/书库文件组即可，无数据库迁移。
验收优先已有本地服务/登录态，不修改用户数据；必要时以独立临时数据库和服务验证真实 API。
