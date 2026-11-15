# 阶段 G：作品主界面、阅读器和作用域（部分）

## 已实现范围

- `ProjectLayout` 对纯管理员显示原有代管提示，对协作者显示真实只读或编辑能力；只有作者能力允许时才可打开作品成员面板。内容区按账号、会话及作品隔离，失去读取时卸载；成员抽屉及 AgentDock 按作品权限代次失效。仍具编辑权时的授权变化不直接卸载内容区，具体表单须阻止旧请求回写并保留未提交草稿。
- `AppLayout` 中普通协作者及管理员兼协作者返回个人书架；纯管理员继续返回作品管理。当前作品无 AI 能力时不挂载 TaskPanel，因此查看者不启动任务轮询、详情或订阅；个人任务面板按会话、作品与授权代次重新挂载。
- 作品概览分别按 `can_export`、`can_backup`、`can_recycle` 和 AI 能力显示操作；非作者不提交默认风格字段。自动分析设置开关保留给作者，协作者只显示状态。
- 项目资料、自动分析开关、向导进度、手工封面上传/删除、AI 封面预览确认均在请求发起前捕获项目 scope，响应后再校验；仅合并封面和向导对应字段，保留未保存表单。异步消息、导航等也受当前 scope 限制。
- 独立 Reader 以真实账号、授权快照和请求代次隔离整页与异步响应；只有 `can_write_reading_progress` 的作者读取/恢复/发送进度。viewer、editor 和管理员不挂载进度监听；章节或目录读到 403/404 后撤下阅读视图，阻断待上报及 keepalive。较早目录请求不能覆盖较新的进度或以迟到 403 作废新结果。
- 封面提示词响应接收并显示后端安全 `warning`：当当前账号无法继承作品私人风格时明确提示以无风格方式生成，保留原有 AI 门禁。

## 验证

在 `frontend/` 运行：

```sh
npm run test -- src/components/project/CoverGenerateModal.warning.test.ts src/pages/ReaderPage.collaboration.test.ts src/pages/project/collaborationWorkspace.test.ts src/components/project/adminProjectsUI.test.ts src/components/AppLayout.test.ts src/utils/reader.test.ts
# 6 个文件、36 项通过；已包含前端授权版本字段调整

npx eslint src/api/projects.ts src/components/AppLayout.tsx src/pages/project/ProjectLayout.tsx src/pages/project/OverviewPanel.tsx src/pages/project/WizardPage.tsx src/components/project/CoverPanel.tsx src/components/project/CoverGenerateModal.tsx src/components/project/CoverGenerateModal.warning.test.ts src/pages/ReaderPage.tsx src/pages/ReaderPage.collaboration.test.ts src/pages/project/collaborationWorkspace.test.ts
# 通过

npx tsc -b --pretty false
# 前次通过；最近一次被并行开发中的 seriesState.test.ts 缺少 canChangeSeriesBook 导出阻塞，需全量复跑
```

测试包含真实组件渲染和实际封面上传回调，覆盖 owner/viewer/editor/管理员与管理员兼 viewer；Reader 还模拟滚动进度、403、旧目录先后返回及同账号重登。暂未进行真实浏览器验证，也未运行整体前端门禁。

## 后续边界

- 当前 `canUseAI` 仍是阶段性仅作者门禁，editor 的 AI、任务、向量及相关 UI 不能标为完成；待阶段 E 全部鉴权、费用、撤权和投影接通后，按真实 `can_use_ai` 开放并复核所有效果。
- 后端 `update_wizard_progress` 当前仍严格作者路径；开放编辑者向导前须先完成相应后端动作授权。`inspirationApi.fromProject` 和一键分析同样须核对。
- 系列和各内容面板的完整权限、系统浏览器交互及响应式门禁由后续 G/F 工作完成；本记录不代表 Gate G 已通过。
