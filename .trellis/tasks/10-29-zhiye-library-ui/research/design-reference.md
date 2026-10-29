# 设计与实际实现对照

## 视觉原文
路径：/Users/wenyuantc/.vetta/workspace/AI小说平台/zhiye-novel-full.vetd。
**实现者必须阅读 DESIGN.md、theme.css、components/Shell.tsx、components/UI.tsx、frames/_layout.tsx、frames/library.tsx 及后者实际引用的 CoreScreens.tsx::Library，不能仅看本摘要。**

- 画板1440×900；侧栏216px、左右16/上24；顶栏56px、左右32；页内容26px 32px 28px。
- 标题30px Fraunces/宋体、500字重、行高1.35；正文14px Geist/苹方；辅助12px；按钮高36/圆角7；卡片圆角10。
- 全局分组：创作空间（我的作品/灵感立项/拆书导入），创作资源（模型/提示词/风格/工坊/技能/MCP）；底部任务/管理员/账号主题。
- 书库：标题/统计→浅色最近写作条→左状态右搜索/视图→四列24px间隔；设计封面高174，图片带90，下面书名/题材/作者；卡片正文上距24、状态/两行简介/章数字数时间/写作及更多。
- 用户真实封面为完整图片，应保留其内容；不拷贝 data.ts 示例作品/作者/封面/章节/任务数。

## 真实项目
- 颜色字面量只允许 theme/palette.ts，CSS 用变量/AntD 用 token；不要关闭或放宽 noHardcodedColors 测试。
- /projects 对应设计 /library；新建是原有 Modal；模型 /settings/ai；模板 /prompt-templates；MCP /mcp-plugins；任务是 TaskPanel Drawer；账号是原有改密/退出菜单，无 /account。
- ProjectListPage 已有 create/export/backup/import/delete/read，全保留；utils/shelf.ts 已有筛选排序统计、continueTarget/readTarget、封面配色和测试。
- Project 包含 genre/premise/synopsis/tags/status/total_words/chapter_count/last_edited_chapter_id/reading/cover_updated_at；最近章名需现有章节 API，不造作者或卷名。
- ProjectLayout 已有项目菜单/AgentDock，若整合只改布局，不改业务页。

## 公开首页补充
参考 frames/index.tsx（公开官网页）及 .snapshots/index-1793259781569.png，画板1440×2400。需实际读取 index.tsx；HomeWorks/HomeBookCover仅为视觉依据，不能迁移initialWorks假项目、固定作者或体验账号。公开页不能读取私有项目，入口接现有登录、注册开关、书库与新建表单。
