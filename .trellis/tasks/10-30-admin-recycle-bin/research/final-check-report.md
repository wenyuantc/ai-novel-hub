# 最终全范围独立检查报告

日期：2026-10-31（Asia/Shanghai）。检查者：`trellis-check` 子代理 `/root/check_frontend`。

状态：最终独立检查与可执行门禁已完成；后端1143测试、前端520测试及lint/等价TypeScript/Vite均通过。未发现剩余确定代码缺陷；真实PostgreSQL验证和原始build生成目录权限仍为明确未解决的环境/发布门禁，不宣称它们通过。没有提交、推送、合并或归档。

## 检查边界与方法

- 阅读 PRD、design、implement、check.jsonl 及其对应规范，重新核对 backend/frontend 索引和新增 recycle-bin 契约，而非只审查最后修改的文件。
- 以任务开始前 HEAD `d61cfd6b05b44d2c3f33c110f53f3a4680d4849c` 为基线审查全部产品差异、新文件、原有消费方和相关测试。
- 后端实施者冻结前只读检查并反馈，正式交接后才直接修复后端局部问题；没有覆盖其他代理写入，没有修改主会话拥有的规划/spec/task 文件。
- 严格排除 `implementation-baseline.json` 的4项无关改动；逐项计算SHA256，均与基线一致。
- 浏览器、最终HTTP及实际应用重启由主会话负责。下文引用这些结果时明确标注来源，不把他人操作记成检查者亲自执行。

## Findings（已修复）

### 检查者直接修复

1. **合法Unicode标题不能完成彻删确认**
   - 文件：`frontend/src/components/project/RecycleProjectModal.tsx`、`recycleBinUI.test.ts`。
   - 原因：输入的 `maxLength=200` 按UTF-16单元截断，合法200个emoji标题不能完整输入。
   - 修复：移除冗余原生长度截断，保留逐字确认与服务器校验；新增200个emoji回归。

2. **API Key的项目ID投影仍显示回收站作品**
   - 文件：`backend/app/services/api_key_service.py`、`backend/tests/test_api_keys.py`。
   - 原因：正常开放授权详情虽然404，`ApiKeyPrincipal.project_ids` 和密钥列表仍直接返回全部持久grant ID。
   - 修复：`_project_ids`联接正常Project条件，只过滤投影，不删除grant；扩充回归证明软删后投影为空/正文404、grant仍在、改名不丢授权、恢复后原授权自动可用且未授权作品仍404。

3. **删号空计数之后并发创建在SQLite返回500而非业务409**
   - 文件：`backend/app/services/admin_service.py`、`backend/tests/test_project_lifecycle.py`。
   - 确定性事件测试在删号count=0之后、业务flush之前创建并提交作品，复现用户及作品未丢但响应500。
   - 原因：SQLite的RESTRICT可由内部触发器报告扩展码1811（SQLITE_CONSTRAINT_TRIGGER），旧翻译只识别787。
   - 修复：仅将1811且错误消息恰为SQLite固定外键失败消息的情况纳入已核实冲突，返回 `user_has_projects` 409；新增无关1811 trigger异常不得被误吞的测试。

4. **旧测试尚按立即硬删/旧schema和旧并发时序断言**
   - 文件：`tests/test_api_keys.py`、`test_project_cover.py`、`test_careers.py`、`test_relationships.py`、`test_reader.py`、`test_agent_api.py`，以及测试fixture/helper。
   - 修复：保留原跨用户/跨项目/数据保留断言，增加软删保留且普通读取404，再通过真实管理员精确确认purge证明级联清理；有作品时RESTRICT拒绝原始删号，purge清空后仍验证用户私有Key级联。备份格式仍v1，APP_SCHEMA断言更新为已批准0024。
   - 关系并发的双到达屏障移到父项目锁之前；显式模拟陈旧查重，继续覆盖数据库唯一约束兜底的201/409、中文错误和完整事务回滚，不为通过测试移除父锁或删除并发覆盖。

5. **Key编辑整表提交会误撤销隐藏授权，或阻止仅改元数据**
   - 文件：`frontend/src/components/apiKeys/ApiKeyFormModal.tsx`、`utils/apiKeys.ts`及其测试。
   - Key项目投影过滤后，若全部授权暂不可见，原表单会以至少选1部阻止改名/有效期；正常+回收站混合授权则会把仅改名提交成可见ID范围完整替换，隐式撤销隐藏grant。
   - 经主会话批准做最小兼容修复：编辑按all_projects和规范化ID集合比较，范围未变就省略两范围字段，允许空投影仅改元数据；创建及明确更改范围保留原验证/完整替换。帮助文案明确未改范围保留暂不可见授权，更改范围按当前选择替换。不新增公开字段。
   - 4项新增回归覆盖空投影/混合授权仅改元数据、顺序/重复ID、显式范围修改和创建/编辑空范围校验。

### 独立检查发现、由原后端所有者在交接前修复并经本轮复核

- `project_lifecycle.load_record`重复刷新会丢失未flush父项目标量/JSON：保留非生命周期dirty字段，仍从数据库刷新权威生命周期；agent TransactionSession重复锁回归覆盖。
- 智能体确认等待conversation_lock之后先执行 `recover_external` 再重验生命周期：调整为进入锁后先准入，修复提交后再重验，防止陈旧external提案在回收站中被正常HTTP请求修改。
- running任务on_abort失败后执行体退出但DB仍running：可靠写终态并保存失败证据，后续可以重试安全收尾；孤立running也有幂等恢复路径。
- 组织add/remove成员漏父行事务屏障：补集中锁与双请求事件回归。
- KB/Batch handler吞on_abort异常：向引擎/协调器传播，不能误判安全删除。
- 独立MCP确认请求排空及rollback后访问过期Task：cleanup_scope提前保存primitive project_id；远端返回遇删除时只做受控“结果未知”收尾，不应用结果、不续跑，不承诺撤回外部副作用。
- **公开deleted_at代次在冻结/回拨时可能复用**：检查者用独立临时用例复现“删除→恢复→相同时间再删→旧purge204误删”；经主会话批准增加内部持久last_deleted_at高水位，父锁内取 `max(now,last+1微秒)`，restore保留，备份和agent效果排除。
- **仅时间高水位不足以防SQLite最高项目ID复用**：经主会话批准Project启用AUTOINCREMENT；新实体不能复用已purge的ID。正式测试覆盖冻结/回拨/引擎重启/独立session、同名新实体和带存量子数据迁移；独立临时复现复跑为旧代次409、旧实体ID404。

## AC1–AC8证据

| AC | 代码与自动化证据 | 浏览器/运行时来源与边界 |
|---|---|---|
| AC1 删除语义与正常入口隔离 | `test_complete_project_is_hidden_restored_in_place_then_purged` 覆盖三身份正常列表、人工目录统计、直接ID/子资源、开放API、Task输出/SSE/全局清理；`test_direct_analysis_and_agent_ids_cannot_read_or_write_trash` 补分析/回写/agent直接ID；KeyInfo投影回归。普通DELETE仅调用统一协调器。 | 主会话确认普通用户无入口/回收站路由拒绝、各原删除入口统一提示。已交付的历史字节/浏览器缓存不承诺撤回。 |
| AC2 原位恢复与完整性 | 完整项目表快照软删→恢复比对；原ID/owner/status、章节版本/设定/快照/封面/检索/阅读/Task与agent历史保留，高水位另行验证。禁用owner恢复不启用账户；FakeLLM调用为0；现存Key授权恢复。 | 主会话真实浏览器确认取消恢复不操作、确认恢复反馈/刷新；实际HTTP另验原ID。 |
| AC3 精确确认与代次 | `test_permissions_record_generation_and_exact_confirmation`、两项正式代次高水位/同名实体ID测试；strict boolean/aware datetime/逐字title；原始微秒字符串直传。旧请求不能命中新代次；purge仅目标外键图，其他作品/用户资源保留。 | 主会话在UI验证空名称/多余空白禁用、精确名称启用后取消，不点击最终不可逆UI确认；物理purge由隔离HTTP明确执行。Unicode确认回归已补。 |
| AC4 到期与自动清理 | `test_retention_boundary_system_expiry_and_independent_failures` 覆盖到期前1微秒/恰到期拒绝；周期启动/失败重试/stop测试；固定cutoff、键集每批100、单作品独立事务；恢复/purge竞争只一方成功。 | 主会话最终fresh库启动补清与系统actor结果另记verification；SQLite通过不等于PG行锁实测。 |
| AC5 排空与并发隔离 | queued/running/paused、并发queued取消清理、DB已终态但on_success未退、自动分析禁止派生、超时/请求取消释放intent、abort失败保留并可重试、MCP请求排空和等待会话锁后拒绝、正文/组织成员父行屏障、dirty JSON、备份跨chunk中止等事件测试。普通多事务入口逐次准入，内部任务收尾由真实执行链登记等待。 | 测试只用FakeLLM/fake MCP；外部已经执行的副作用无法撤回。单进程前提明确，不声称支持多实例。 |
| AC6 删号保护 | 正常/回收站/过期作品均409且count包含全部；清空后允许删号，保留自身/最后管理员护栏。原始RESTRICT和新增“count=0后并发创建”测试证明SQLite不级联新作品并返回409；无关trigger仍上抛。 | 主会话确认作品数含回收站、UI不能删号但可禁用；PG User锁/FK竞争尚未实测。 |
| AC7 管理员边界 | 所有回收站路由AdminUser；普通403/匿名401，降级403/禁用401；正常人工helper未放宽严格owner，管理员删除中的内部Task取消未成为他人任务通用权限。 | 主会话普通账号无导航/路由拒绝，并验证管理员页面可用。 |
| AC8 审计与界面 | 管理员delete/restore/purge及系统expire，空actor显示系统；安全摘要白名单、审计失败注入整体回滚，既有审计日期清理/高水位/历史保留继续回归。前端API/utils/UI/store覆盖原始代次、迟到响应/会话失效、目标级cache丢弃、明确404和错误重试。 | 主会话确认23条分页/搜索、375px明暗无横溢出、危险操作取消、过期禁恢复、控制台error为空；真实HTTP/system actor UI结果见verification。 |

## Findings（未修复）/ 迁移与验证边界

没有遗留未修复的已确认产品代码问题。以下为不能用局部代码改动掩盖的环境/发布验证缺口，明确保留：

- 0023→0024测试使用真实父子存量数据，分别在SQLite外键开/关下验证章节、版本、封面、API grant和历史审计不丢，`foreign_key_check`为空，Project RESTRICT/AUTOINCREMENT及审计sqlite_sequence高水位保留；受保护降级包括trash/pending/last_deleted_at及新动作/摘要/系统actor。
- 对照主会话更新的回收站、auth-admin、admin-projects、admin-audit、database、project-transfer和前端component/state/quality规范，公开API未增加UUID或高水位字段，备份仍v1/APP_SCHEMA0024。
- **PostgreSQL没有实际验证**：本机工具/实例和Docker守护进程不可用，由主会话已核实；SQLite确定性竞争及PG错误形状单测不能替代真实PG迁移、行锁和并发事务。发布前仍需临时PG执行既定矩阵。
- **原 `npm run build` 仍受既有权限阻止**：`frontend/node_modules/.tmp`、`frontend/dist` 为root:staff 0755。未sudo、未改依赖/项目配置。等价TS/Vite验证通过但不能写成原命令通过；应由用户/环境所有者另行处理生成目录权限。
- 未触碰真实用户库、真实模型或外部MCP。主会话的隔离浏览器/HTTP验收与本检查的临时测试库彼此独立。
- 主会话确认最后1811修复后，fresh SQLite/FakeLLM短运行时53769的12项HTTP全通过：实际purge204、正文/PNG字节往返、旧代次409、严格confirm422、普通403/匿名401、启动expire系统actor、恢复无活动任务、仍有作品禁删号409。该运行时已退出，四类模型调用全部0。此前真实IAB已恢复作品并查看系统actor详情。
- 最后Key编辑适配亦由主会话在53770真实IAB复验：先建仅B和A+B两把Key，再软删B使投影[]/[A]，分别只改名并实际保存，随后只读临时SQLite确认名称更新且持久grant仍为[B]/[A,B]；三项结果全true，未显示/复制秘密Key，控制台error空，模型调用全0。四个自建服务端口均已关闭，浏览器标签及主题/视口覆盖清理。具体证据见 `research/runtime-verification.md` 及主会话 `verification.md`，不是检查者自行操作的浏览器结论。

## Verification

### 已完成

- `backend/.venv/bin/ruff check .`：通过；后端无独立静态类型检查器门禁，Pydantic/运行时契约由pytest覆盖。
- 旧失败归因6模块：`test_api_keys.py test_relationships.py test_careers.py test_project_cover.py test_reader.py test_agent_api.py`，**74 passed**（72.62s）。
- 新增生命周期4模块，明确 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`：**41 passed**（24.80s）。此前未设置locale的一轮迁移读取中文配置被ASCII环境阻止，8项失败/33项通过；已归因并以UTF-8完整重跑，不修改项目配置。
- 独立临时冻结时钟安全复现：2 passed；同实体旧purge由原204变409，purge后同名新实体分配新ID、旧ID请求404。
- 前端 `npm run lint`：通过。
- app/node两份等价TS检查（分别指定 `/private/tmp/ainovel-recycle-check-tsconfig.{app,node}.tsbuildinfo`）：通过。
- 前端完整测试：`HOME=/private/tmp/ainovel-recycle-test-home npm test -- --config /private/tmp/ainovel-recycle-frontend.config.mts --configLoader runner --fsModuleCachePath /private/tmp/ainovel-recycle-module-cache`，最后Key适配后重跑为**67文件520测试通过**；lint和app/node两份等价TS亦重新通过。日志 `/private/tmp/ainovel-recycle-final-frontend-key-scope.log`。
- 等价Vite生产构建：临时配置只导入原配置并重定向cacheDir，`--configLoader runner --outDir /private/tmp/ainovel-recycle-frontend-dist`，通过。最终回收站chunk `AdminRecycleBinPage-ct1m770m.js` 9.59kB、入口 `index-h2zMVLbJ.js` 182.71kB、Key页 `ApiKeysPage-Da5vkxWL.js` 12.37kB、最大vendor-antd 1147.01kB，无1500kB超限；主会话已通知reload。
- `git diff --check`：通过；4项排除文件SHA256保持基线。

### 最终后端完整套件

命令（backend工作目录）：`LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest`。

已核实fixture使用每项临时SQLite、FakeLLM、fake DNS；MCP协议集成仅绑定127.0.0.1临时服务。通过require_escalated获准本机socket后执行，日志 `/private/tmp/ainovel-recycle-final-backend-verified.log`。此前修复前/收集后继续改测试的旧快照运行已精确停止，不作为最终验收证据。

结果：**1143 passed，1条既有Gemini SDK ExperimentalWarning，487.19秒；进程exit 0**。所有测试均实际执行，包括需要loopback socket的21项MCP插件测试；未用skip替代验证。

完整套件结束后再次运行Ruff、`git diff --check`及4项基线SHA256检查，全部通过。后端产品自1811精确翻译后未再改动；前端最后Key适配之后重新通过完整520项及全部等价构建门禁。
