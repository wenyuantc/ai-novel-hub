# 前端实现记录

## 实施边界（写代码前）

- 行为缺口：现有用户列表没有详情页；只有删号动作有部分异步失效保护，其他账号动作复制到详情会保留旧响应/确认框风险。
- 行为归属：详情安全读取在 admin API/types 与新懒加载页面；账号操作交互在窄共享组件及其页级 controller，后端权限仍由原 API 裁决。
- 预计修改：api/types.ts、api/admin.ts及测试；router.tsx；AdminUsersPage.tsx；新增AdminUserDetailsPage与components/adminUsers下共享控件、目标弹窗、controller及行为/SSR测试。
- owner深链的两个现有管理页仅做按路由目标重建的最小正确性修复，如实际核实需要。
- 不修改后端、规范、任务规划、工具配置或依赖；不新建全局缓存/通用CRUD框架，不启动服务。
- 共享提取以原账号API方法、rowActions、严格用户名确认及列表末页回退的测试证明行为保留；角色/启停采用声明式目标Modal，保留确认语义并使卸载自然销毁portal，主会话已认可。

## 实施结果

- 新增懒加载 `/admin/users/:userId` 与用户名/详情入口；用户列表也改为同组lazy页面，未新增依赖或全局导航。
- 新增 AdminUserDetail / AdminResourceUsage 及 getUser；保持原列表/写响应字段不变。
- 详情使用 actor+target key 重建；明确错误/404/null/零资源，正常与回收站对比，删除中单独提示。所属正常作品始终固定 owner 分页，目录/回收站深链正确。
- 从用户列表窄提取 AdminUserActionButtons、声明式 AdminUserActionDialog 和 AdminUserActionsController / useAdminUserActions。所有写动作共用代次与身份校验，表单异步校验也先占用提交状态，离页/身份切换立即清理目标，旧结果不交付 toast/reload/导航。
- 保留原 rowActions、严格用户名确认、安全409计数收窄、列表末页删除回退；详情删号成功返回列表；新建账号流程仍在列表。
- 验证了现有作品目录/回收站仅初始化读取 owner 查询串的风险，最小修复为按当前 URL owner 重建内部页实例，不改全站筛选架构。
- 首轮 lint 发现 React 新 refs 规则把 useState 构造时传入回调中读 ref 视为渲染期读 ref；改为 effect 更新 controller 回调，不禁用规则，最终 lint 通过。
- 代码与单元/SSR检查完成；真实浏览器交互、375px截图和集成网络检查由主会话负责，本报告不冒称已手动验收。
