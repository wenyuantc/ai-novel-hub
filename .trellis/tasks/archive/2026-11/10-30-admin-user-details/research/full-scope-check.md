# 管理员用户详情：全范围质量检查

日期：2026-11-01（Asia/Shanghai）。检查角色 trellis-check；分支 codex/admin-user-details。

## 当前结论

- 已检查全部已修改及未跟踪 backend/frontend 产品文件，不限已暂存或最新实现片段。尚未发现需要机械修复的产品问题，本检查未修改产品代码。
- 独立前端门禁全部通过：84 文件、658 测试，12.61s；lint、app/node TypeScript、临时目录生产构建均 exit 0。
- 独立后端全目录 ruff 通过，git diff --check -- backend frontend 通过。
- 最终完整后端门禁通过：1350 passed, 1 warning in 565.37s，测试进程正常exit 0。命令为获批沙箱外标准完整pytest，没有修改源码、断言、超时、执行顺序、种子或环境阈值，没有排除/重试插件。本次是完整单轮结果，不是拼接分项通过；不再启动测试。
- 前两轮失败仍完整保留：实现代理1345 passed / 5 failed（四项沙箱bind、一项关系生成SSE超时），检查代理首次沙箱外1349 passed / 1 failed（任务stop/retry SSE超时）；相应单项复跑记录见下方。末轮通过并不等于这些非稳定超时的根因已修复，未扩大到任务引擎或测试超时改动。
- 主会话真实浏览器验收已通过，证据见research/browser-verification.md与browser-runtime-final.json；PostgreSQL及键盘穷举未实测。

## 审阅要点

- 登录：哈希/version/启用状态条件更新，SQL CASE 时间不倒退，updated_at 不变，commit后不refresh至新版本，失败回滚/固定安全500/不打印哈希SQL。注册、管理员创建、bootstrap、改密、日常访问入口区别正确。
- 详情：白名单账号列、owner限定、每作品章节预聚合、三种真实生命周期互斥覆盖、全部作品数、零资源、当前正文而非历史版本、单SELECT、原响应兼容。
- 迁移：0028父0027、原位ADD/DROP、存量null，真实用户/作品/章节/私有配置子行、外键及RESTRICT往返测试。
- 前端：RequireAdmin/lazy/Outlet、actor+target key、正十进制安全ID、固定owner分页与深链key重挂载，不开放他人私有/AI/阅读进度能力。
- 共享动作：声明式Modal、同步busy、异步表单校验同代次、提交前后身份/目标/gate检查、StrictMode重新激活、离页移除portal、精确用户名、409安全计数、末页回退。

## 规范同步复核

主会话已更新backend/auth-admin.md、database-guidelines.md及后端索引，frontend/component-guidelines.md、type-safety.md及前端索引；本检查逐份查看diff并对照实际代码，六份均一致。另指出并由主会话修正了create_user签名残留旧注释及TS usage内联对象/后端AdminUserUsage命名类的区别。七段式登录/详情契约、0028原位迁移、声明式Modal/controller和原权限边界均准确。检查代理遵守所有权未改规范，纯文档变更不重跑产品全套。

## 环境边界

标准npm test/build因既有只读缓存EACCES受限，独立门禁使用临时HOME/cache/buildinfo/outDir完成等价验证，没有放宽权限、规则或改依赖。独立生产入口index-BZnQc0hy.js；详情8.13KB、列表4.62KB、最大vendor-antd1147.01KB，低于原1500KB上限。外部outDir提示及宿主Node localstorage-file警告未屏蔽。

## 原始失败与复跑记录（保留，不覆盖）

1. 实现代理最终源码沙箱内全套：5 failed, 1345 passed, 1 warning in 629.86s。四个FakeMCP用例于tests/mcp_fakes.py:87 bind被PermissionError拒绝；关系生成test_generate_marks_duplicate_self_unselected_existing_and_invalid_fields于tests/helpers.py的10秒SSE等待超时。单项复跑分别1 passed in 1.60s、获批本地MCP四项4 passed in 4.08s。完整命令及源码快照见research/backend-checks.md，原工具日志未覆盖。
2. 检查代理第一次沙箱外全套：1 failed, 1349 passed, 1 warning in 578.78s。失败tests/test_tasks.py::test_stop_interrupts_running_and_keeps_queued[settings_overrides0]，tests/test_tasks.py:232重试后的run_task→tests/helpers.py:285/276→asyncio.wait_for(client.get(tasks stream), 10.0)触发TimeoutError；该用例未修改。相同环境单项命令pytest -q tests/test_tasks.py::test_stop_interrupts_running_and_keeps_queued，1 passed in 0.71s。原始工具输出从本检查自身Codex会话JSONL逐字提取，保留 /private/tmp/admin-user-details-check-first-unsandboxed-output.txt（18456字符），不是重写的堆栈。以上仅记录非稳定性，未做基线全套因果对照，不能断言超时根因。

## 独立通过的命令

工作目录frontend：npm run lint；npx tsc -p tsconfig.app.json --tsBuildInfoFile /private/tmp/admin-user-details-check-app.tsbuildinfo；npx tsc -p tsconfig.node.json --tsBuildInfoFile /private/tmp/admin-user-details-check-node.tsbuildinfo；npx vite build --configLoader runner --outDir /private/tmp/admin-user-details-check-frontend-dist；env HOME=/private/tmp/admin-user-details-check-vitest-home npm test -- --configLoader runner --no-cache --fsModuleCachePath /private/tmp/admin-user-details-check-vitest-cache。

工作目录backend：env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/ruff check . --no-cache；最终获批沙箱外env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/python -m pytest（stdout/stderr仅重定向到原始日志，不改变参数），1350 passed / 1 warning，565.37s，exit 0。

## 最终完整门禁与边界

- 原始完整日志：/private/tmp/admin-user-details-check-final-backend-pytest.log。日志最后真实摘要为1350 passed, 1 warning in 565.37s (0:09:25)，不是从分项结果拼出。
- 唯一warning是既有Gemini SDK generate_images弃用提示，未屏蔽，不属于本次改动。
- 末轮结束后逐一核对下方24个产品文件SHA-256与首轮快照一致，四个用户基线保护文件亦未变化；全仓git diff --check通过。
- 原标准npm test/build的只读缓存问题未修改；已通过等价测试、类型及生产构建。PostgreSQL与键盘穷举未实测，不声称通过。
- 已观察的两处既有测试用例SSE偶发超时未定位根因，不把末轮全绿说成任务引擎问题已根治。完整单轮门禁已满足，未再追绿或扩大修复范围。

## 全范围文件快照

| 文件 | SHA-256 |
|---|---|
| `backend/alembic/versions/0028_user_last_login.py` | `30858f69d58a3be7413deb65a18fcce6da47b2a3ef03bd65e33d1c85c85dd268` |
| `backend/app/api/v1/admin.py` | `f9518f67a2c62b3023af265d9554a0228ba474789f10f263d370b4981b0caa50` |
| `backend/app/models/entities.py` | `7340e366b0a880e334d004b93766fb2ecdd7d66f6bae56a85994a133807f82e0` |
| `backend/app/schemas/admin.py` | `9d834a99bf019cf5403a450f1229d575f33bee43defb98f4d7408e3a1c0779f8` |
| `backend/app/services/admin_service.py` | `690a32c5316675b509811fc64e3b22707bc9139a3a61eafc5956f8d23d6c7770` |
| `backend/app/services/auth_service.py` | `756a3a455dfe8309c07fc3e5d4a40e8c46a044436e1906f8257d280eeb3d6e6c` |
| `backend/tests/test_admin_user_details.py` | `52accb8c8f478a2dfffd515c0d0ad888043731687ec84a83e12b428e10cc4446` |
| `backend/tests/test_migrations.py` | `5cd1fc2229c749b23a73fcd7fda3ae590a9c2b0db1f4ddade91dc13e006f88a9` |
| `frontend/src/api/admin.test.ts` | `6d59b66d1352d58bfcf0a4d627c55c31b034b5153f3d52623e5f9f99a556ea1e` |
| `frontend/src/api/admin.ts` | `6ee0d6f59053a6cdb05d2968141a9144ef929b1de33f44bc6a5939ac156ff127` |
| `frontend/src/api/types.ts` | `bbc14f0505653ca54d346dc99b179575708ad30d64e57bb4d72b017b97d7d23b` |
| `frontend/src/components/adminUsers/AdminUserActionButtons.tsx` | `f97f3805781b65f84a28866da1d336c7ef7d230ad0337f2edabfabdb7bb1f4f4` |
| `frontend/src/components/adminUsers/AdminUserActionDialog.tsx` | `e6775c17ba794a2fd8735bfe7b1eed93e3f464a67e9789ce822b2cd947e49554` |
| `frontend/src/components/adminUsers/adminUserActionsController.test.ts` | `58ff3a63cf60e7ffcd2de2d93482bb11da23fe3ab6570c9212aa8584d97b26e5` |
| `frontend/src/components/adminUsers/adminUserActionsController.ts` | `a6eac7d530fe743ab19de226ce085c36b4ac16378eaf3a31cecc9540d6e4c093` |
| `frontend/src/components/adminUsers/adminUserDetailsUI.test.ts` | `694edbcbc95c2b2a2697ec163371519416c44a18c6395be28249601ceb31bb93` |
| `frontend/src/components/adminUsers/useAdminUserActions.ts` | `86fa3b58b90ca0ec2e1c5fe82d340f3c6aad188d996a9256bdccf2e38267a9ac` |
| `frontend/src/pages/AdminProjectsPage.tsx` | `e4d1c6dccab342852fbe4d8cf3d8dc23c0b6b768a39698e7e2ea6daa5cffbadd` |
| `frontend/src/pages/AdminRecycleBinPage.tsx` | `88dc7348407a5201085daf6466cd0595a5be696989a9263fef6cd5aab03daec1` |
| `frontend/src/pages/AdminUserDetailsPage.tsx` | `c5a10e709bb5ec9d4e76ace8ac0bd007d57bfc33c6b66df52af7a0625a89f18b` |
| `frontend/src/pages/AdminUsersPage.tsx` | `111e1d3f120117b674959b896560ae3f7ca283848bdcf7f2c545fbeec22c4dac` |
| `frontend/src/router.tsx` | `1b4a5a7ccc1d9c7604475b1a7a52d7c756d79f9923cfcd0e3574706697f4bf8f` |
| `frontend/src/utils/adminUserDetails.test.ts` | `21eba651f2047b536d39d77a252aef97666bf23f76ee3adda19e3b80df4df683` |
| `frontend/src/utils/adminUserDetails.ts` | `4fc323c522ee2fbd7004958eff38dcbffb6a36af92fbfb0dd5ab1b3ee1d2b6bc` |

基线保护文件 SHA-256 全部未变化。
