# 管理员操作日志：独立完整检查报告

日期：2026-10-31。角色：主会话派发的 trellis-check。已补读保存的完整 hook 输出、PRD/design/implement、check.jsonl 及 research/current-contracts.md 指定的大文件规范段落。本报告不以实施代理的旧门禁替代最终独立验证。

## 检查范围与边界

- 审查本任务全部新增文件与完整差异：后端 audit model/schema/writer/service/router/migration、模型/路由注册、admin/auth/project/chapter/version/cast/skill/workshop 服务、必要路由与迁移/审计测试；前端 types/API/utils、页面、详情/清理组件、AppLayout、路由和配套测试。
- 先只读后端并向实施者反馈；正式移交后复核最后核心修复并独立运行最终全量。检查代理自身只修改前端局部标签和前后端对应测试，没有再次修改已移交的后端产品代码。
- 保留原有 .codex/config.toml、.gitignore、两个 source-command skill 目录；未 stage/commit/switch，未操作真实数据或真实模型，未打开第二套浏览器验收。
- 平台配置、模板、init/update/detection 机制无本任务变更；模型导出、API router、管理员懒路由及桌面/移动入口已检查，无遗漏触点。

## Findings (fixed)

### 1. 业务版本唯一竞争在审计预 flush 时丢失原 409

- File：backend/app/services/audit_writer.py、version_service.py、chapter_service.py、chapter_version_service.py。
- Issue：AuditOperation.commit 先 flush，再调用 commit_versioned；原版本竞争可能提前抛出未翻译的 IntegrityError，变为500。
- Fix：检查反馈后由后端实施者补充精确识别 chapter_id/seq 唯一键的 flush_versioned；版本写原语及章节修改/恢复均接正确业务 flush 边界；审计 flush 自身不被误译为业务409。移交后已逐条复核。
- Regression：test_version_unique_competition_remains_safe_409 的 business_flush/version_primitive 两个案例；原版本与生成、改写回归纳入最终全量。

### 2. 原始数据库异常可能继续进入全局 traceback 日志

- File：backend/app/services/audit_writer.py。
- Issue：失败审计行虽已脱敏，原异常仍可能交给全局 logger.exception，SQLAlchemy 参数可能带正文或密码哈希。
- Fix：检查反馈后由后端实施者在已启用审计边界通过 AuditOperationError 返回固定中文500，只记录 operation_id/committed；失败审计不可写时仅安全运维信息，不递归、不重新提交业务。提交后的投影故障保留成功事实，不补失败。
- Regression：test_audit_system_database_error_does_not_reach_raw_exception_logger 同时验证 caplog、HTTP 和审计行；不确定提交、业务回滚、提交后投影已有独立断言。

### 3. 共享摘要标签与实际业务语义不符

- File：frontend/src/api/auditLogs.ts、utils/auditLogs.test.ts、components/auditLogs/auditLogsUI.test.ts。
- Issue：hidden_by 实际为 admin/author，却标为“下架操作人 ID”；content 同时用于工坊模板，却标为“章节正文”。
- Fix：检查代理分别改为“下架方”和中性的“正文内容”；新增作者→管理员、工坊正文两条只读展示回归，更新原纯函数期望。未修改传输或交互契约。

### 4. 补充两个关键范围/拒绝采集的明确审计断言

- File：backend/tests/test_admin_audit_logs.py。
- Issue：全局 Skill 创建/工坊发布拒绝及不可见私有 Skill 的安全失败范围，缺少直接针对审计结果的完整断言。
- Fix：检查代理新增 test_skill_create_and_workshop_publish_business_rejections_are_audited、test_private_skill_denial_audits_only_safe_id_and_private_writes_are_excluded，验证409/400失败事件、404仅留安全ID、不复制私有名称/所有者/正文，以及管理员本人私有写入不误采集。仅补测试，不改产品。

### 5. 规格与实现末检

- 主会话新增 backend/admin-audit.md，并同步两个 index、auth-admin、database、error-handling、logging。
- 检查发现并由主会话修正：标签唯一来源应为 api/auditLogs.ts；工坊 approve 必须保留原“锁释放前 flush/refresh/生成 output，再 commit”例外；补入新增两条测试。设计与数据库规范同步该既有例外，未移动业务投影代码。
- 最终签名、枚举、摘要/错误字段、500安全日志例外和测试名均与源码一致；规范没有新增产品承诺。

## Findings (not fixed)

无待修复的本任务产品代码或规格问题。

以下是已说明的环境/验证边界，不通过越权改权限、改配置、跳过测试或扩大任务范围解决：
- 原标准 npm run build 会写 root 所有的 node_modules/.tmp 与配置缓存，实施阶段已证明 EPERM。检查阶段使用等价的两个 tsc 配置检查及 Vite runner 临时产物路径全部通过；未 chmod/chown、未改 tsconfig/Vite 配置，未声称标准脚本本身已恢复可写。
- 未连接真实 PostgreSQL 做数据库并发集成；SQLite 的真实唯一约束、同 nonce 并发200/409、不同预览的实际计数、高水位及原事务规则均有测试。生产 PostgreSQL 锁顺序没有改动。
- 数据库整体不可用/进程终止时失败审计不能承诺落库；这是已批准的同库方案边界，不是额外可靠投递保证。

## 跨层验收核对

| 要求 | 源码与测试核对结果 |
| --- | --- |
| AC1/2 真实actor与范围 | 真实认证actor直达服务，不冒充owner；双管理员/合并版本各留一条；覆盖28动作，导入→创建与submit→approve不双记；普通/本人/私有及排除项保持原边界。 |
| AC3/4 查询与历史 | 明确AdminUser；组合过滤、用户名字面量、同时间ID倒序、分页/空态；无业务外键，删对象/owner/actor保留快照；不可见私有目标不额外探查。 |
| AC5 脱敏 | 摘要Pydantic白名单/有界列表；重置不记密码或长度；正文/备注/参考资料不复制；原异常和SQL参数不进审计或运维日志。 |
| AC6 版本关联 | 仅ID/seq/hash，核对project/chapter/version关系后available/changed/missing；原合并/裁剪/标星不变；正文由主动独立versionApi.get获取，关闭后本地丢弃。 |
| AC7 原子与结果 | 成功同事务；创建用户无提前commit；失败整体rollback后新事务；不读取expired ORM；审计失败阻止业务；未知提交结果查operation_id，已成功不追加失败。 |
| AC8/9 清理 | aware UTC半开日期、本地结束日次日边界；purpose/actor/exp/nonce/高水位/confirm均服务端校验；所有DELETE排除audit.cleanup；失败清理亦保护；删除+成功留痕原子，重放/并发直接API有断言。 |
| AC3/8 前端状态 | 筛选应用回首页、失效页回退、错误重试；真实账号key重建本地状态，日期/关闭/账号及请求门闩隔离旧响应；busy与本地立即消费令牌防重复提交。 |
| AC10 原行为 | 全量原用户/会话/项目权限、AI/任务、版本、Skill、工坊等回归通过；没有放宽owned helper、改变owner身份或引入新依赖。 |

后端Literal与TS联合类型经Python AST/文本提取对照，6组共94值完全一致：28 action、8 target、2 result、8 error、33 field、15 state；nullable与请求/响应字段也已人工逐项核对。

## Verification

### 最终门禁

| 项目 | 实际结果 |
| --- | --- |
| 后端Lint | env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONUTF8=1 .venv/bin/python -m ruff check --no-cache .：All checks passed，exit0。 |
| 前端Lint | npm run lint：通过，exit0，无错误/警告。 |
| TypeCheck | 两个tsconfig均通过，exit0；后端仓库没有独立配置的静态类型检查命令，不另冒称已运行mypy等工具。 |
| 前端构建 | Vite runner构建3445模块成功，exit0；审计页chunk22.18kB（gzip8.17kB），vendor-antd1147.01kB低于1500kB，RelationGraph仍独立。 |
| 前端全量测试 | 64文件、491测试通过，exit0；含本轮新增2条标签回归。 |
| 审计后端定向 | 32 passed in22.87s，exit0；含本轮新增2条范围/失败回归。 |
| 最终后端完整全量 | 1102 passed、1条既有Gemini SDK弃用warning，450.59s，取得实际exit0后才报告通过。含22条迁移以及所有包的原回归，没有跳过MCP或仅挑任务测试。 |
| 差异检查 | git diff --check：完整工作区通过；检查代理没有stage/commit/switch。 |

前端具体等价命令（workdir=frontend）：

```bash
npm run lint
npm exec -- tsc -p tsconfig.app.json --incremental --tsBuildInfoFile /private/tmp/admin-audit-check-app.tsbuildinfo
npm exec -- tsc -p tsconfig.node.json --incremental --tsBuildInfoFile /private/tmp/admin-audit-check-node.tsbuildinfo
npm exec -- vite build --configLoader runner --outDir /private/tmp/admin-audit-frontend-dist
env HOME=/private/tmp/admin-audit-frontend-home npm test -- --configLoader runner --no-cache --no-fsModuleCache
```

最新临时构建已在标签修正后重建并通知主会话。Vitest有两条既有Node --localstorage-file无有效路径提示，不影响测试结果。

后端最终完整命令（workdir=backend，Python3.12.8）：

```bash
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider
```

### 环境失败与受控重跑证据（完整保留）

1. 首次Ruff默认缓存因 backend/.ruff_cache 写入EPERM失败；未改权限，改无缓存执行完整Ruff后通过。
2. 最终核心、追加2条测试之前的受限全量收集1100项，exit1：1096 passed、4 failed、1既有warning，536.79s。4条均在 tests/mcp_fakes.py:87 的 sock.bind((127.0.0.1,0)) 抛 PermissionError [Errno1] Operation not permitted，未到产品MCP协议行为。分别是 connect_time_ssrf_check_sends_no_request、redirect_not_followed、fastmcp_integration_list_and_call 的 streamable_http/sse 参数。
3. 受限小范围-x复现同一bind错误：1 failed/17 deselected，1.09s；据此核实为本地监听限制，没有修改或跳过MCP代码。
4. 经窄范围require_escalated审批，允许仅临时本地监听，原4条完整通过：4 passed/17 deselected，4.23s，exit0。
5. 再经相同本地测试权限审批，完整运行包含新增2条测试的最终1102项，所有包与MCP均通过，1102 passed/1既有warning/450.59s/exit0。未访问真实外网、生产库或LLM。

### 运行时证据归属

真实浏览器/临时HTTP验收由主会话完成，详见 research/runtime-verification.md；本代理不将SSR当浏览器证据，也不冒称自己重复执行。已核对该报告覆盖375/1280明暗、筛选分页/空态、只读关联版本、日期失效/取消、网络失败恢复、普通用户guard，以及临时API实际清理/重放/高水位/删除actor后留痕。浏览器未点击最终永久清理，实际破坏性执行在独立临时HTTP环境完成，这一区别在原报告中明确。
