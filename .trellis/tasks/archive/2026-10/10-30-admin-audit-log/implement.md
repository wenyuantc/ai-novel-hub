# 管理员操作日志：执行与验证计划

状态：用户已于 2026-10-31 在最终摘要后回复「开始」，最终实施评审通过，任务已激活并派发后端/前端实现；实现与最终门禁已通过，用户已批准按计划提交及收尾，不推送。全部实现严格限于 PRD 覆盖表。

## 1. 启动前检查

- [x] 最终摘要明确目标、首批覆盖/排除项、摘要与失败策略、日期清理护栏、风险和文档状态；获得后续明确批准。
- [x] 保持既有其他工作区改动，不覆盖 `.codex/config.toml`、`.gitignore` 或 source-command skill。
- [x] 重新确认 migration head 和 git 状态，按需建立 `codex/admin-audit-log` 分支。
- [x] 使用现有任务，不重复 create；运行 task.py start，再核对 current。若缺会话标识，按脚本提示配置本会话身份，不猜测其他聊天的 active pointer。
- [x] 加载 Phase 2.1 详情，按 auto 模式派发 trellis-implement；prompt 首行写 `Active task: .trellis/tasks/10-30-admin-audit-log`，原生注入优先，缺失时子代理加载三份文档和 implement.jsonl。
- [x] 上下文验证有大文件截断警告；派发时明确要求按 research/current-contracts.md 的读取地图补读相关规范，不将原生注入视为全文已经加载。
- [x] 明确实现代理拥有本任务业务/测试文件，不得回滚他人改动；主会话负责规划/规格/提交/收尾。

## 2. 实施顺序

### I1 数据模型与查询骨架（AC3/4/5）

- [x] 新增 models/audit.py、schemas/audit.py、迁移 0023_admin_audit_logs.py（重查编号）、models/__init__.py 导出。
- [x] 有界摘要/版本引用、action/result 类型、operation_id 唯一、无业务级联外键、SQLite 不复用 ID。
- [x] 新增 audit_service 查询/详情及 admin_audit 路由，在 v1 注册；全部 AdminUser。
- [x] 固定迁移版本的 upgrade/downgrade、历史快照、权限、筛选/转义/排序/分页测试。
- [x] 门槛：新迁移与查询测试通过，再接写入；不在业务已提交后补写成功审计。

### I2 显式写入与用户管理（AC1/2/5/7/10）

- [x] 实现审计上下文、同事务成功、rollback 后新事务失败、committed 状态与 operation_id 去重。
- [x] 拆出 auth_service 的不提交创建原语，保留公开 create_user/注册/bootstrap 原提交行为。
- [x] admin_service 创建/重置补 actor，其他写入接入；同步 admin.py 及调用测试。
- [x] 测试原有自身/最后管理员防护、token_version、重名冲突；审计不保存密码/哈希/请求。
- [x] 注入审计失败、业务异常、提交后投影失败，证明不能产生成功孤儿审计或把已提交业务误记失败。

### I3 跨用户作品和章节（AC1/2/4/6/7/10）

- [x] 接入 project_service、chapter_service、chapter_version_service、cast_service 的登记动作。
- [x] 保留 owned/manageable 权限，generation 冲突、字数、commit_versioned 和原版本合并/裁剪规则。
- [x] 删除前快照；批量建章一次事件、有界 ID/数量；正文只记摘要并关联已有版本。
- [x] 双管理员/同版本合并、空正文、删除目标/owner/actor、版本合并/裁剪/删除的回归。
- [x] 普通用户/管理员本人创作不误采集；AI/任务/其他人工子模块不因审计扩大权限或采集。

### I4 工坊与全局 Skill（AC2/4/5/7/10）

- [x] 接入 workshop_service 审核、发布、元数据、上下架、删除；锁顺序不变。
- [x] submit 自动通过/官方发布不双记；审核快照原业务保留但不复制进审计。
- [x] 接入 skill_service 全局创建/导入/修改/参考资料/删除/恢复；import→create 不双记。
- [x] 普通用户或不可见私有资源仍沿原 403/404；不把点赞/导入私有资源/私有 Skill 复制记为管理员维护。

### I5 受保护的日期清理（AC4/5/7/8/9）

- [x] 日期 AwareDatetime/UTC 半开区间；预览区分可删/受保护，签名 purpose/actor/range/high-water/count/nonce。
- [x] 执行强制预览令牌、confirm=true、当前管理员；nonce 防重放/并发重复。
- [x] 单事务 DELETE + 成功清理留痕；action=audit.cleanup 在所有清理路径排除，失败清理同样保护。
- [x] 日期端点、相邻日、时区、非法/过期/篡改/他人令牌、直接调用、取消、重复预览/确认均有测试。
- [x] 注入 DELETE/留痕失败，断言均回滚；并发新增较大 ID 不被旧预览清掉；实际数可与预计不同。

### I6 前端管理页（AC3/4/6/8/9）

- [x] api/types.ts + api/auditLogs.ts；镜像后端 schema，不散落重复传输类型或直接 fetch。
- [x] AdminAuditLogsPage、只读详情、日期清理弹窗、对应 utils 及测试。
- [x] RequireAdmin 懒路由、AppLayout 桌面/移动入口、页面名/面包屑。
- [x] 筛选回首页、加载/失败/空态、详情不存在、版本不可用、删除后页码回退。
- [x] 日期/账号/关闭使预览失效；旧异步响应不复活令牌，提交中不可重复点击。
- [x] 不改全局 useLoader/认证状态架构；页面使用既有模式、主题、中文文案和响应式控件。

## 3. 目标文件与非目标

新增：backend 的 audit model/schema/service/router/migration、tests/test_admin_audit_logs.py；frontend 的 audit API/page/detail/cleanup/utility 与相应 *.test.ts。

局部修改：models/__init__.py、api/v1/__init__.py、上述 R1 服务/必要路由、tests/test_migrations.py 和相关原有测试；frontend/api/types.ts、router.tsx、components/AppLayout.tsx。

若服务实际过长，可在 audit 领域内分文件；不重构所有业务事务，不改其他内容模块、项目备份格式、认证协议或权限系统。发现范围明显大于该清单时停在规划边界，先更新设计并重新评审。

## 4. 自动验证命令

均在对应 workdir 单独执行，不靠 shell 分隔符串接；实际门禁及环境替代以 research/check-report.md 为准，不把计划命令误作执行证据。

backend：
- `uv run pytest tests/test_admin_audit_logs.py tests/test_migrations.py`
- `uv run pytest tests/test_admin.py tests/test_auth.py tests/test_admin_projects.py tests/test_chapters.py tests/test_chapter_versions.py tests/test_cast_context.py tests/test_skills.py tests/test_workshop.py`
- `uv run ruff check .`
- `uv run pytest`

frontend：
- `npm test -- src/api/auditLogs.test.ts src/utils/auditLogs.test.ts src/components/auditLogs/auditLogsUI.test.ts`
- `npm run lint`
- `npm run build`
- `npm test`

迁移仅在测试临时库验证固定 revision 往返及 head 全链路，不对用户现有数据库运行 downgrade 或删除。后端故障测试使用 tmp_path/FakeLLM/可控异常，不依赖真实网络。

## 5. 运行时验证与独立检查

- [x] 使用临时测试库/受控管理员与两个用户，禁止操作用户真实数据做清理测试。
- [x] 浏览器完成筛选/详情/版本关联、预览/取消及确认护栏；隔离HTTP完成真正清理与再次清理保护，未点击浏览器最终永久删除，详见runtime-verification.md。
- [x] 非管理员入口不可见、直接 API 拒绝；网络失败可重试；快速切范围/关闭/切账号不复活旧确认。
- [x] 桌面1280及375px明暗主题检查表格/卡片、日期输入、弹窗与AX焦点可达；文档无横向溢出。
- [x] 派发 trellis-check（首行 Active task 同上）加载 check.jsonl，检查最后迭代全部 diff，不只看新测试。
- [x] 独立检查重点：事务时机、actor 身份、敏感字段白名单、覆盖矩阵、嵌套调用双记、保护过滤、token 重放、日期与并发、原权限零退化。
- [x] 将实际命令、数量、结果、风险和浏览器证据写入 verification.md，未执行项明确列出，不用规划文件充当完成证据。

## 6. 规格、提交与回滚

- [x] 通过质量门禁后按 trellis-update-spec 固化新 admin-audit 契约，并在 backend/frontend index 挂接；只记录实际已实现行为。
- [x] 走 Phase 3 提交评审，使用中文 Conventional Commits，仅暂存本任务文件；不自动把启动前的配置或 skill 改动混进提交。
- [x] 明确应用回滚可保留新审计表；显式迁移降级会删除审计，必须单独备份和确认。
- [x] 最终实施和独立提交批准均已获得，按两笔工作提交→归档→会话日志的顺序执行，不推送。
