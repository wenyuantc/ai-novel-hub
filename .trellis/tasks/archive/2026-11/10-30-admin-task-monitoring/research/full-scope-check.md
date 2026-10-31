# 全范围独立检查与自修复

检查角色：原生 `trellis-check`；日期：2026-10-31 / 跨日复验。基线 `e6ba17c`，分支 `codex/admin-task-monitoring`。未提交、未切分支、未启动其他子代理。

## 范围与所有权

- 已读取保存的完整原生注入、check.jsonl 全部上下文、PRD/design/implement、取消研究末尾真实CAS复核，以及前后端规范索引/质量/跨层规范。
- 按 `git diff --name-only e6ba17c -- backend frontend` 与 `git ls-files --others --exclude-standard -- backend frontend` 的并集逐文件审查；共 **49 文件：backend 24、frontend 25**，包含全部未跟踪产品和测试，不只末批diff。最终全量暴露后增加的2个既有测试文件同样纳入复核。
- 第一阶段只读，具体问题交原实现所有者修复；主会话明确移交后，检查者才写产品。检查者直接修改 `task_engine/engine.py`、`task_engine/cancellation.py`，新增 `tests/test_admin_task_check_regressions.py`，适配 `tests/test_project_lifecycle.py`、`tests/test_tasks.py` 的真实锁/生命周期测试；没有覆盖其他代理修改。
- 排除用户原有 `.codex/config.toml`、`.gitignore` 和两个 source-command skill，未触碰这些文件；最终逐文件SHA256与implementation-baseline.json四项均一致。规范由主会话维护，检查者只读复核并反馈。

## Findings（已修复）

### 1. 共用终态提交确认丢失仍会遗漏 drain 完成事实

- 文件：`backend/app/services/task_engine/engine.py::complete_state`、`backend/tests/test_admin_task_check_regressions.py`。
- 问题：调用者 `_finalize` 的局部回读不能覆盖 drain 直接调用 `complete_state`。真实数据库已提交cancelled、marker清空后若确认抛错，drain只记安全日志；completed未登记、重新连接的owner Hub未关闭。遗留项目级 `_abort_failures` 会使后续项目排空重放旧abort，危及后来同章节资源。
- 证据：新增 `test_drain_commit_confirmation_loss_cannot_replay_old_abort_on_new_resource`，真实commit之后抛错，红测确认completed缺失；并以真实Chapter/旧失败标记/新同资源任务验证旧abort不会先还原新资源。
- 修复：在共用状态提交边界捕获确认异常，rollback请求会话，再以独立session核实原 `id + UUID + queued_at轮次 + 目标状态 + marker已清`；事实匹配才回放已提交状态、维护原轮completed并正常完成Hub/统计后的流程。无法确认仍抛出，由原安全失败/持久意图路径处理，不重放模型/统计/abort。
- 执行者：检查者直接修复；不是只记录风险。

### 2. 缓存核验IN参数可无限增长并阻断核心取消扫描

- 文件：`backend/app/services/task_engine/cancellation.py::scan`、同一检查回归文件。
- 问题：现存用户及全站保留任务量无固定上限，直接把全部completed/winners/recovering key放入一个IN会触达数据库绑定参数上限。每用户200条不能限制全站规模。
- 证据：新增601条真实缓存元数据，真实SQL监听红测观察单条查询601个参数；不是模拟扫描实现。
- 修复：pending核心查询独立执行，缓存元数据每批最多500个key核验；已在pending结果中的key不再重复查询。缓存仍随现存Task/轮次清理，没有新增历史归档或调度框架。
- 执行者：后端实现者提出边界，检查者直接补红测、修改并复验。

### 3. completed只绑定UUID，会越过同Task的新轮次

- 文件：`task_engine/cancellation.py`、`task_service.py`、`tests/test_admin_task_cancellation.py`。
- 问题：late paused获胜把UUID放入completed后，owner resume的新轮普通取消若abort失败，项目重试仍可能因旧UUID completed跳过新轮必要清理。
- 修复：completed绑定UUID与queued_at，项目重试只跳过同轮；resume以 `max(now, previous + 1微秒)` 换轮，冻结/回拨也不复用。winners/completed/recovering随现存Task生命周期回收。
- 执行者：只读阶段发现并交原后端所有者修复，检查者复核最终代码与回归。

### 4. 未知handler的pending在recover/finalize错误放行

- 文件：`task_engine/engine.py`、`tests/test_admin_task_cancellation.py`。
- 问题：drain本身未知kind会fail-closed，但recover/finalize无handler时原abort_ok=True仍可清marker并释放活动资源，行为不一致。
- 修复：未知handler非成功收尾不能声称abort成功；pending保留原状态/意图/资源。无pending仍保留原有恢复语义。分别新增启动恢复和finalize回归。
- 执行者：检查者发现两处并交后端所有者修复，最终已核实。

### 5. 合法大写UUID深链被误判身份变化

- 文件：`frontend/src/components/adminTasks/adminTasksController.ts` 及其测试。
- 问题：UUID校验允许大写，后端规范小写，getExact逐字比较误拒合法大写深链。
- 修复：openDetail入口规范小写；不同UUID仍严格拒绝，重复打开规范化同一目标不重复GET。
- 执行者：只读检查发现，前端原所有者修复并冻结，检查者复核后独立执行前端全量。

### 6. 必需失败窗口测试缺口

- 文件：`backend/tests/test_admin_task_check_regressions.py`（检查者独占新增）。
- 已补：真实受理commit成功后确认丢失→HTTP安全500、marker/唯一成功审计保留、日志不含异常秘密、独立扫描single-flight完成且不记相反失败；真实HTTP请求在受理并登记drain后取消→drain无cancel、仍在project_work、独立session继续完成。
- 同文件另含上述共享终态确认丢失与601-key参数上限回归，共4例。

### 7. 原删号测试假设无真实User写屏障，测试自身形成锁互等

- 文件：`backend/tests/test_project_lifecycle.py`。
- 问题：原测试在删除count之后的flush gate中等待并发项目创建提交，而本任务新增的真实父User写锁正持有数据库写屏障。测试把release放在创建await之后，形成自身互等并报database locked；不能移除产品锁或加大超时。
- 修复：原RESTRICT末层防御测试显式仅为该删除请求故障注入遗漏父写屏障，保留count后真实插入、409及User/Project均保留的全部断言；另新增真实User写屏障先赢的事件/SQL屏障测试，证明并发原始Project INSERT等待删除，释放后FK拒绝且无孤儿。两层保护均被验证。
- 执行者：检查者直接适配，主会话确认方案；未把产品退回SQLite无效FOR UPDATE。

### 8. 订阅软删测试误把两个既存赢家限定成一个

- 文件：`backend/tests/test_tasks.py`。
- 问题：基线e6ba17c的subscribe/_task_gone已按作品deletion_started_at/deleted_at过滤。取消close_hub先赢会带cancelled Task，生命周期心跳先赢会带task=null/任务不存在。旧测试用50ms心跳只断言前者，存在真实调度竞态；本任务没有修改这段产品可见性。
- 修复：经主会话确认既存契约后，两分支分别确定性验证：取消分支让close先赢；生命周期分支在已提交删除意图后gate实际取消，让心跳精确先结束。分别断言完整终止载荷，并共同断言真实同UUID Task最终cancelled、Chapter状态释放、无delta/多终止事件、后续详情/新订阅404。不是只放宽错误字符串，也没有为了测试去读取已不可见数据。

## 已复核的跨层与安全事实

- 每个管理路由独立AdminUser；接受前真实actor/owner用户写屏障与角色/token复核。普通get_owned_task、output/stream/retry/resume/finish授权未放宽。
- 管理列表/详情只选安全元数据；原Task.title/error/input/result/output_text/checkpoint均不读取。搜索只匹配用户名/作品标题；stage严格固定或数字模板；自动审计标签不复制智能体指令。
- 筛选CTE+聚合+分页为同一SQL；counts覆盖当前筛选全部页。旧queued_at为null，resume换本轮，不以created_at/updated_at假冒排队时间。
- marker与task.cancel_request受理审计同事务；原因只在该动作局部放行且有界。提交前无取消信号，202不等于实际取消；真实终态由原引擎与保存赢家决定。
- queued扫描/claim双屏障；owner cancel/resume/finish/拆书skip真实条件写；finalize/recover观察marker后CAS重读。abort失败pending仍持资源，未知handler/不安全paused断点不猜测清理。
- 只cancel一次，HTTP请求生命周期不传播到独立drain；restart不重放LLM/MCP；shutdown等待真实工作。单删/清理/裁剪/无作品删号/项目purge/expire保护pending和实际project_work。
- 前端独立页面状态，不向个人任务store混入其他用户；5秒/hidden/慢请求去重，筛选/目标/账号/403/卸载失效，确认两阶段、取消中与实际结果区分。首页累计与现存明细入口不混淆。
- 0026原位ADD，不重建SQLite父Task表，旧子引用/外键保留，有pending拒绝降级。

## Verification（检查者亲自执行）

工作目录 `backend/`：

```bash
env UV_CACHE_DIR=/private/tmp/ainovel-check-uv uv run --no-sync ruff check .
env UV_CACHE_DIR=/private/tmp/ainovel-check-uv LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run --no-sync pytest tests/test_admin_task_check_regressions.py tests/test_admin_task_cancellation.py tests/test_admin_task_adversarial.py tests/test_admin_task_monitoring_migrations.py tests/test_admin_tasks.py -q
```

- Ruff全量通过。
- 最终局部：**79 passed in 46.93s**，退出0，覆盖检查者全部4例及全部新管理API/取消/对抗/迁移测试。
- 在最终scan分批修复前、共用提交修复后另有49例通过；最初两个补测试2例通过。最终79例为本轮局部门禁依据。
- 共用drain确认丢失和601-key各先真实红测，随后修复并统一绿测；没有弱化原断言。
- 完整门禁暴露的锁/SSE测试适配后，`tests/test_tasks.py::test_subscribed_task_deleted_ends_with_error tests/test_project_lifecycle.py tests/test_relations_generate.py::test_empty_result_and_errors_never_write`：**21 passed in 11.33s**，退出0。关系空输出原断言不变，未调大10秒SSE超时。
- MCP四个真实本地协议节点，使用经工具批准的 `sandbox_permissions=require_escalated`（只为127.0.0.1临时监听，不调用外部服务）：**4 passed in 5.01s**，退出0。节点为 `test_connect_time_ssrf_check_sends_no_request`、`test_redirect_not_followed`、`test_fastmcp_integration_list_and_call`（两个transport）。未改MCP产品或skip测试。

工作目录 `frontend/`：

```bash
npm run lint
npx tsc -p tsconfig.app.json --tsBuildInfoFile /private/tmp/admin-task-check-app.tsbuildinfo
npx tsc -p tsconfig.node.json --tsBuildInfoFile /private/tmp/admin-task-check-node.tsbuildinfo
env HOME=/private/tmp/admin-task-check-vitest-home npm test -- --configLoader runner --no-cache --fsModuleCachePath /private/tmp/admin-task-check-vitest-cache
npx vite build --configLoader runner --outDir /private/tmp/admin-task-check-dist
```

- Lint、两份TypeScript检查、Vite生产构建全部退出0。
- Vitest：**75文件、594测试通过**，18.40s，退出0；只有既有Node localstorage参数警告。
- 构建：监控页29.36kB、入口185.57kB、vendor-antd1147.01kB，无1500kB超限警告；临时输出不覆盖主会话浏览器产物。
- 首次尝试 `tsc -b --tsBuildInfoFile` 触发TS5094（选项不兼容）；随后按仓库两份真实tsconfig分别 `-p` 检查通过，不计为代码类型错误，也未伪称第一次命令通过。

仓库根目录：`git diff --check -- backend frontend .trellis/spec`，退出0；23个未跟踪产品/测试文件另逐个 `git diff --no-index --check /dev/null <path>`，无空白错误（no-index退出1仅表示新增文件差异）。

标准npm test/build因既有root-owned node_modules缓存不可写的尝试与错误由原前端所有者记录在frontend-checks.md；这里使用等价配置loader/临时缓存/临时buildinfo/临时dist复验，没有改权限、依赖或项目配置。

## 他人执行的验证与待回填

- 原后端所有者的高风险既有205例、114例冻结联合及中止的历史全量见 `backend-checks.md`；它们不是最终完整门禁，也不是检查者亲自运行的命令。
- 首次最终完整pytest由后端所有者唯一运行，session49112，**7 failed、1283 passed、1 warning，584.70s，退出1**；完整可观察trace见backend-checks.md。4项是沙箱loopback bind被拒，检查者获批权限后4项均通过；1项是上述旧锁测试互等，已修；1项是上述既存SSE赢家竞态，已分支验证；关系空输出1项在FastAPI/anyio同步依赖线程检查点处超时，未调用FakeLLM，原节点7参数复跑均通过，未猜测修改产品或超时。
- **修正后最终完整pytest由后端所有者唯一运行，session77921：1292 passed、1 warning，574.90s，进程自然正常退出0，未中断/强杀。** 本次获批loopback权限，完整日志 `/private/tmp/admin-task-monitoring-01a1928f-20261101-final2.log`，检查者已只读核验汇总，后端所有者确认工具exit_code=0。唯一warning为已有Gemini SDK generate_images弃用提醒，无失败/跳过。检查者未重复启动/停止全量；再次冻结后只有后端所有者运行完整套件。
- 真实浏览器由主会话使用独立临时SQLite+FakeLLM/Gate验证，证据见 `browser-verification.md`，检查者已读取报告：三种状态真实UI取消及actor/owner/原因审计、无新增模型调用、组合筛选、5秒/关闭、受控500恢复、实时降权/普通用户拒绝及桌面/375明暗主题均有主会话实测；报告也明确hidden/visible等主要依赖自动化的边界。检查者没有亲自执行浏览器交互，不以SSR或他人记录冒称自身浏览器实测。

## Findings（未修复）与残余边界

- 没有已确认但故意留置的任务范围内产品缺陷；最终完整pytest、前端全量、lint/type-check/构建与差异空白门禁均已通过。前轮失败、定位与修复事实保留，不以单次局部结果替代完整门禁。
- **PostgreSQL真实并发/行锁未验证。** 当前环境无postgres/psql，Docker socket受沙箱限制；SQLite双session/真实RETURNING/CAS测试不能冒称PG运行验证。应在可用PG环境补锁序、接受-claim竞争与迁移实测；不为绕过环境缺口改变产品或降低断言。
- **任意强杀精确赢家不在本期范围。** 正文事务已提交、Task终态尚未提交的强杀窗口仍遵守原启动interrupted规则；未扩大为跨服务事务改造，也不承诺撤销已执行外部效果。
- 未知handler/异常“终态+marker”有意fail-closed、保留恢复依据；需要运维明确修复不一致数据，不将其假装取消成功或删除。
- 新7段式任务规范、500-key缓存核验/601-key回归、受理审计真实“已受理”标签及相关前后端交叉引用均已只读复核，与实际代码一致。SSE既存双赢家仅限定排队软删除场景，未改写运行中已提交保存可succeeded的真实赢家；on_abort窄安全日志例外也与最终实现对齐。规范与最终全量/浏览器总报告由主会话收口。

## 最终结论

本任务完整代码范围的独立检查已完成，已发现的可修复问题均落实代码/测试并复验，无待处理代码finding。**Lint通过；TypeCheck通过；后端1292测试及前端594测试全部通过；等价生产构建通过。** 真实浏览器依据主会话报告通过其明列场景。保留真实PG未验证、标准前端缓存权限限制与任意强杀非本期承诺三项边界；未提交、未归档，由主会话继续维护验收清单与提交授权。
