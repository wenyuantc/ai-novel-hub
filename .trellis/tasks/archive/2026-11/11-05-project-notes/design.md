# 笔记设计
权威跨层合同见 ../11-05-mention-tracking/design.md 的“笔记”和“HTTP合同”。
## 落点
后端新 models/notes.py:ProjectNote、schemas/notes.py、services/note_service.py、api/v1/notes.py，迁移0032_project_notes依赖0031。前端类型和API放 api/notes.ts，页面 NotesPanel.tsx，必要组件放 components/notes/。
模型字段 id/project_id/title/content/revision/timestamps，project_id CASCADE，revision默认1。读取采用manageable project，写入锁作品、expected_revision CAS；PATCH略去字段不改，content允许清空，title不可空且≤200，正文≤200000码点。
GET分页{items,total,page,page_size}，summary含excerpt；POST/GET/PATCH返回完整Note，DELETE带expected_revision。错误沿项目AppError/detail。
## UI
列表有“新建笔记”、标题摘要和更新时间，搜索/分页。详情编辑纯文本，保存状态明确，删除确认。读权限可看正文；修改按钮只对当前canEdit开放。切目标先处理dirty；409保留草稿并允许加载新版本，不自动覆盖。
NotesPanel 必须支持 ?id= 定位和当前权限/作品/请求代次隔离；兄弟任务会消费 mention_start/end/hash 或以共用定位窗口集成，可预留 props/helper，避免并发改同文件。前端路由和导航由兄弟M-F注册。
## 兼容
RULES加入project_notes；APP_SCHEMA暂0032，后由M-B推进0033。模型/API聚合注册先做完并通知兄弟。旧备份缺表合法，新备份重映射主键归属。无AI任务、审计动作或独立权限目录。
## 风险
冲突保护必须数据库CAS，前端禁用按钮不够。作者笔记不可丢在派生索引中。回滚迁移前导出，不能自动删除有数据的表。
