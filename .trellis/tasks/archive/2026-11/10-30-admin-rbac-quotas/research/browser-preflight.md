# 浏览器预验（非最终验收）

日期：2026-11-02。仅新临时SQLite、合成账号与FakeLLM。独立checker还在修改后端局部取消窗口，前端最终QuotaPanel小修也晚于首构建，所以本记录不当作最终树完整验收。

## 运行与实际观察

- 服务脚本`/private/tmp/admin_rbac_quotas_runtime.py`，127.0.0.1:8781，PID7192，统一执行会话69010；首次导入顺序引发测试脚本自身的providers循环导入，已先导入tests.helpers修正，未修改产品导入代码。
- 首构建`/private/tmp/admin-rbac-quotas-frontend-dist`，index SHA-256为3c38b23a5b99fe3c008502b383623dc50c791134a4a5b677eca71b379e08ff7f。不是最终frontend-final-dist(9975...)，也不是checker后续构建。
- Chrome独立会话admin-rbac-quotas，真实访问匿名/admin/roles跳/login；通过UI输入合成rbac_super账号后回到角色页。
- 角色目录四类内置有明确“内置只读”，编辑/删除disabled；内容管理员13项、审核员5项；自定义已有成员时删除disabled。
- 真实UI新建“浏览器验收-内容维护”，勾选“人工内容维护”后“查看全站作品人工内容”同时可见勾选，危险与依赖文字明确；提交成功后SQLite roles确有id10，role.create成功审计存在。
- 从合成rbac_empty详情打开分配角色，实际选择刚建自定义角色及内置审核员；提交后详情显示“审核员 浏览器验收-内容维护”，弹窗撤下。没有以仅点击保存或截图推断持久化。
- AntD Select未展开时输入只读、展开后才可搜索，第一次直接fill因此超时；随后真实click展开再fill成功。已选择后的下拉遮挡保存时，先Escape收起再保存成功。这两次为自动化操作顺序，不是产品缺陷，不使用force点击绕过。
- 初始GET auth/me的匿名401为预期；此预验未观察新的未捕获JS异常，完整控制台与最终窗口仍须复验。

## 工坊旧构建隐私红例

- 在同一临时实例经真实HTTP创建普通owner的两条匿名风格，root审核并下架其中一条；reviewer正常浏览时可见隐藏条目及两处作者`rbac_owner`。
- 对此隔离浏览器的`**/api/v1/workshop/items**`可撤销注入500；独立root会话将reviewer所有角色撤回，浏览器真实切标签/回前台同步。
- 权限确已更新：“审核”入口消失；刷新故障确已发生：页面显示“合成工坊刷新故障”。但旧首构建仍保留隐藏条目及两处`rbac_owner`，真实复现checker指出的useLoader保留旧data泄露，而不只是静态推测。
- checker已按actor ID+workshop.read布尔重挂Browse，最终新构建必须重复同场景做绿例验证。未把旧首构建错误掩盖成通过。
- 预验结束已撤销故障route、关闭额外空白标签并导航about:blank；核实PID命令后仅SIGINT停止本任务7192，等待最终代码/构建再启动，不动其他进程。

## 需重跑与边界

- checker修复领取session.__aexit__取消窗口、重建前端最终树之后，重启到对应最终代码/构建并重新做关键流程，不能拿当前旧进程/首构建替代。
- 不能拿runtime-info中的初始running/queued状态当当前占用，也不从Settings的300秒默认值推断FakeLLM已超时；Fake provider与真实provider的超时实现不同。预验最终快照实际仅1次假模型调用、0工具/向量调用。脚本下一轮显式使用7200秒测试参数，仍以真实任务状态/占用验收，不改变部署设置。
- 尚未完成只读工作区、管理目标保护、真实配额编辑/0/不限额、任务取消、撤权旧弹窗、375px/明暗截图等最终验收；不预填通过。
