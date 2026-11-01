# 管理员权限分级与用户配额：执行与验证计划

规划日期：2026-11-01；验收更新：2026-11-02。实现、独立检查、浏览器与规范同步已完成，详细证据见verification.md。PostgreSQL按用户要求暂缓；用户已授权提交/收尾/main合并推送；未勾选步骤仍需实际完成，不能预填。

## 0. 实施前门槛（主会话）

- [x] 接续现有admin-rbac-quotas，不重复创建任务，不动其他任务。
- [x] D1–D8逐项确认并收敛PRD；高风险可委派、管理目标保护、两项配额/0/无限/超管不豁免、迁移/生命周期均已决定。
- [x] 持久化三个领域的研究与精确触点；不新增第三方依赖，不把人物/职业role误当账号角色。
- [x] 复杂任务补齐design.md、implement.md，明确唯一super事实、权限目录、共同屏障、四创建入口、真实任务领取及前端只读面。
- [x] 完成上下文清单校验、PRD收敛自查和独立静态规划复核；审计范围P2措辞歧义已消除，最终摘要提交用户审阅。
- [x] **用户在最终摘要之后明确回复“开始”，批准实施**，不同于先前生命周期规则的同意。
- [x] 已记录implementation_approved=true并通过task.py start激活，当前会话指针已核验。
- [x] 记录Git实现基线与四个受保护文件hash，建立`codex/admin-rbac-quotas`并更新任务分支。
- [x] 实施迁移前再次核验唯一head，避免覆盖其他迁移。

原有 `.codex/config.toml`、`.gitignore`、两个未跟踪source-command skill目录不属本任务，不回滚/格式化/暂存/提交。若实施中新出现别人的修改，同样先归属而非覆盖。

## 1. 执行方式、所有权与依赖

Phase2使用Trellis原生子代理，不在最终规划前调实现/检查。每个prompt以 `Active task: .trellis/tasks/10-30-admin-rbac-quotas` 开头；优先native context注入，未注入时按角色协议加载implement/check清单。明确告知工作者并非独自工作，禁止回滚别人修改。

- **后端trellis-implement**负责backend范围：RBAC模型/目录/服务/schema/API/迁移/初始化、现有授权/审计接入、四创建路径和调度准入、所有对应测试；依次完成第2/3/4节。复用同一后端工作者连续执行，避免project_service、admin_service、system_settings_service和schemas被多个后端工作者同时编辑。
- **前端trellis-implement**负责frontend全部本任务变更和测试；新schema/接口/权限目录固定后可与后端后续步骤并行。不得修改后端/规范/规划，契约不符先报告，不各自发明字段。
- **主会话**负责规划、接口契约协调、范围裁定、规范同步及交付；不把backend/frontend之外的清理顺带派给实现者。
- **trellis-check**在实现者退出编辑后做独立全范围检查和范围内修复，避免与实现者同时改相同文件。

RBAC与配额共享授权/用户锁、目标保护及UI身份契约，保持一个既有任务的集成验收，不擅自创建新父/子任务。逻辑阶段不代表它们可在未确认契约时相互跳过。

## 2. 后端基础与迁移（先测后实现）

- [x] 新增集中权限目录及显式依赖，固定33项可委派动作；精确对齐design表，角色/授权管理不进入可委派目录。
- [x] 添加Role/UserRole/AuthorizationState/UserQuota模型、DB约束与计划0029迁移；角色AUTOINCREMENT、用户FK CASCADE/角色FK RESTRICT、无User父表重建。
- [x] 只以User.is_admin作超管事实，super/ordinary特殊身份投影一致；普通成员关系不会伪造超管，复制全部权限角色仍非超管。
- [x] 内置数据/授权state迁移种子与create_all fixture共同初始化；启动顺序为授权初始化→系统设置load→bootstrap→恢复/启动任务引擎。
- [x] 构建同一读快照的授权投影与原token版本primitive；登录/register/me增加安全字段，不破坏最近登录或认证Cookie快照。
- [x] 授权runtime协调器与DB gate统一所有session/manager使用；画出真实锁图，确认设置_lock只在runtime之前。
- [x] 角色CRUD/复制、多角色替换、两项额度读写API；严格schema、mode/0/上界、CAS、在用删除、自身/最后超管及目标账号保护。
- [x] 旧HTTP is_admin创建/PATCH字段拒绝，不能留下无revision提权旁路；内部bootstrap保留已有原语并接统一屏障。
- [x] 新角色/授权/配额安全审计与窄内部bootstrap系统审计；只在真实变更写，绝不记录密码/原始请求。
- [x] 定向回归通过后记录接口/迁移报告，前端从同一schema实现，不以暂时any/宽断言绕过。

建议新增测试文件：`tests/test_rbac.py`、`tests/test_rbac_concurrency.py`、`tests/test_rbac_migrations.py`、`tests/test_user_quotas.py`。可按仓库风格调整文件名，但不能因此遗漏矩阵。

## 3. 后端现有管理能力接入

- [x] 逐个管理路由声明读取/变更具体权限；角色/授权入口始终超管专用，普通CurrentUser不可凭“任意管理”通行。
- [x] 逐个审计所有is_admin/AdminUser残留：只允许超管事实/兼容输出/历史审计/明确super检查；不要机械删除历史字段，也不留二元全量放行。
- [x] get_manageable_project显式read/edit/recycle；覆盖研究清单的资料、章节、版本/选角、设定/组织/关系/职业/伏笔、大纲/快照、封面、知识库人工维护/剧情记忆/目录。所有写在资源锁之前获取共同授权屏障。
- [x] 保持get_owned_project/strict owner能力不变，跨用户私人资源仍404；任何人工管理读写无额外模型/向量/AI副作用。
- [x] 回收站两阶段各自重新授权，等待drain不持gate；恢复/彻删/系统expire的代次、期限与原保护不改。
- [x] 任务取消和设置独立会话写前复核原token/具体能力；保留持久受理、UUID/轮次、shield和提交不确定恢复，不能把已受理取消误当最终取消。
- [x] 既有明确审计范围内的动作由自定义角色执行时不能因is_admin=false漏审；新增角色/授权/配额/bootstrap按本任务接入，原未采集人工子模块不扩大全量采集。清理仍有显式权限，不依赖audit.enabled，个人写排除不扩大。
- [x] 工坊普通接口内的管理分支与路由同步拆分，作者隐私/隐藏/审核/发布/上下架/删除独立；无关管理权不免审、不看匿名身份。
- [x] 全局Skill创建/导入/更新/references/删除/恢复分别授权；editable不当全操作放行，他人私人Skill仍不可见。
- [x] 角色编辑影响多成员、普通目标并发变管理、等待应用锁/资源锁/任务drain期间撤权的双向屏障测试通过。

## 4. 后端配额真实执行链

- [x] quota_service集中解析与SQL计数，作品inherit固定默认无限、任务inherit读取现有有效设置，不新增第七个系统设置或额度缓存。
- [x] 四处创建在副作用前锁owner User并持至原整体提交；样式/灵感状态/备份令牌/拆书解析记录/章节和任务不能出现半成品。
- [x] 拆书重排为User→BookImport并重新校验revision；灵感锁后重新读converted状态；备份预览不预占，真正导入再准入。
- [x] 全生命周期作品数、0/无限、减少配额保留数据、软删不释放/恢复不再扣/purge成功才释放、全部角色不豁免。
- [x] `_dispatch`按用户有效值批量预筛选，候选与admitted分离，0不导致notify热循环，FIFO/serial/resource互斥保持。
- [x] 真实领取在共同runtime/DBgate/User屏障后用最新额度+全局上限复核，commit确认后runtime内同步发布admitted，再开始执行。
- [x] 占用直到真实_on_done释放，覆盖runner finally及DB终态后的on_success；paused退出释放、resume重新准入；候选不会相互判满。
- [x] 所有Settings发布路径（含load/_reload/recovery）与领取共享runtime，固定设置_lock顺序；提高/恢复继承后notify。
- [x] 用可控commit返回延迟测“DB已提交但内存未发布”，未知提交不执行/重放/过早释放占位；任务身份/轮次与既有恢复一致。
- [x] 多owner、有限/无限/继承混合、不同时刻调低/提高、自动分析/重试/继续/拆书派生均按真正执行体峰值断言。

## 5. 前端实现

- [x] types.ts镜像权限/角色/目标管理属性/两种revision/配额判别联合；所有API走统一Cookie客户端，测试路径/AbortSignal/错误码/0与省略语义。
- [x] auth私有权限刷新独立于init/publicConfig，复用可见轮询；403只刷新能力、不登出或重放业务，401才清；失败/慢响应/切账号/退出/同actor撤权均有epoch保护。
- [x] 逐路由权限守卫及统一菜单/移动/账户/面包屑；无首页权限时不会固定跳dashboard；角色页超管专用懒加载。
- [x] 角色表格/移动卡片、创建/复制/编辑复用具体表单，分组操作项/危险标识/依赖可见；内置只读、编辑全成员提示、在用不可删、CAS冲突保留草稿。
- [x] 列表/详情多角色标签与替换分配，旧“设为管理员”布尔toggle撤除；原页级账号controller按actor+action+target保护，管理目标不能被非超管改写。
- [x] 用户详情配额占用/来源/有效值和三态表单，0不转null，管理目标只读保护；显式依赖users.read与quotas.read，不另建通用后台。
- [x] 用户详情所属作品/回收站/审计关联版本、任务与作品owner搜索分别检查读取权限，不靠users.read或audit.read隐式访问他域。
- [x] projectAccess扩展后逐个人工页面和子组件迁移：正文/标题/选角/Cmd-S、设定关联、章节版本/恢复、大纲/快照、封面、知识库人工维护等只读真实生效；本人资源不误锁。
- [x] 工坊/全局Skill逐动作，清理本页拥有的过期确认框；只失写权保留只读数据，失读权才清对应内容；不使用全局destroyAll。
- [x] 普通创建/灵感/备份/拆书额度错误保留表单；任务并发达限保持队列反馈，不误报失败/取消。
- [x] 单测覆盖实际异步prepare/submit/迟到响应及权限复核，不只检查源码字符串；StrictMode、375px、浅暗、键盘可用。

## 6. AC与必要证据矩阵

| 验收 | 证明/测试重点 |
| --- | --- |
| AC1、AC6 | 每项权限正反直接请求；只读与写分离；固定角色矩阵、多角色并集、未知/deny/通配符拒绝；复制super非super |
| AC2、AC7 | 专用角色接口超管保护；旧字段注入无副作用；角色编辑后同账号旧会话下一请求失权而不登出；双向撤权写屏障、自身/最后超管竞争 |
| AC3 | 四入口混合独立session争最后作品名额最多一个成功；任务真实峰值、候选/领取竞态、DB终态但success未完、commit返回延迟 |
| AC4 | inherit/limited0/正整数/unlimited、默认更新/显式覆盖、全局硬上限；降低不删作品/强杀任务；软删/恢复/物理删除；超管不豁免 |
| AC5 | 既有明确采集动作在自定义角色下不漏审；新增角色/授权/配额/bootstrap同事务安全审计，不扩大其他人工子模块采集；故障回滚；FakeLLM零副作用；他人模型/Key/私有Skill/MCP/输出/阅读进度仍隔离 |
| AC8 | 不同管理权限组合、禁用管理目标仍保护、普通目标并发被Role编辑赋权；读安全信息和本人自助改密不误禁 |
| AC9 | 内置不可改删、复制、在用删除409/DB RESTRICT；edit/edit、edit/delete、delete/assign、同目标分配CAS；旧role整数ID不得复用 |
| AC10 | 实际浏览器只读工作区/快捷键/关联表单、权限路由直达/刷新、两会话撤权、迟到弹窗、跨域请求隔离、移动/明暗；403不误登出 |
| AC11 | 0028存量→新head、普通/启用/禁用管理员及私人子行完整、FK检查；bootstrap幂等不改密码/登录时间；新语义数据降级拒绝；新写接口删号重建旧revision拒绝 |

并发测试用仓库Gate/asyncio.Event控制，不用sleep猜顺序。SQLite真实独立session之外，原计划使用隔离PostgreSQL实例重跑关键锁竞争；2026-11-01用户在得知本机Docker守护进程未运行后明确回复“先继续，暂不做 PostgreSQL 验证”，本轮按其要求暂缓PG，verification须逐项标明，不以SQLite通过冒称PG已验，也不擅自启动Docker/连接开发库。

## 7. 检查命令与审阅门禁

各条命令独立运行，不串联掩盖中间失败。建议定向文件名如按实际实现调整，先确认文件存在，再运行对应测试。

后端工作目录 `backend/`：

```bash
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_rbac.py tests/test_rbac_concurrency.py tests/test_rbac_migrations.py tests/test_user_quotas.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_auth.py tests/test_admin.py tests/test_admin_user_details.py tests/test_admin_projects.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_admin_audit_logs.py tests/test_admin_tasks.py tests/test_admin_task_cancellation.py tests/test_admin_task_adversarial.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_recycle_bin.py tests/test_recycle_bin_tasks.py tests/test_project_lifecycle.py tests/test_system_settings.py tests/test_system_settings_runtime.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_workshop.py tests/test_skills.py tests/test_project_transfer.py tests/test_book_import.py tests/test_tasks.py tests/test_migrations.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest
```

前端工作目录 `frontend/`：

```bash
npm test
npm run lint
npm run build
```

全范围check须包括所有本任务已改/新增/未跟踪产品文件，不仅暂存diff。浏览器在隔离测试账号/数据下验证第6节，不用真实用户密码；Node/SSR不替代交互。环境阻碍须记录命令/错误/未验项，不写预填成功。

## 8. 规范、交付与回滚（主会话）

- [x] 对照实际实现更新auth-admin、admin-projects、admin-audit、system-settings、task-monitoring、recycle-bin、skills/workshop及database的相关契约；新增专用rbac-quotas规范并在前后端index互链，避免复制多份目录事实。
- [x] 更新前端state/type/component中的真实权限快照、读写动作与失效语义，保留原Cookie/owner/任务/时间处理契约。
- [x] 落盘implementation报告、check报告、verification，明确真实通过/未验/遗留风险；新锁/发布竞态若发现缺陷，记录根因与可执行防回归，不靠放宽断言通过。
- [x] 本轮未改变已确认产品边界、超管事实、配额口径与锁序；只读UI/隐私与取消窗口均在原范围内修复，没有悄悄缩范围。
- [x] 用户已明确授权“提交 收尾 合并到main并推送”；按Conventional Commits中文描述精确提交，保护原四项修改，不amend、不强推。
- [x] 质量门禁、规范同步和获批工作提交已完成；按finish-work归档，会话记录由紧随其后的独立journal提交保存；原有四项用户修改保持不变。

回滚点：尚未实施前仅规划文件；迁移后无新语义数据可验证空状态结构回退；一旦自定义授权/显式配额/新审计产生，旧应用不具语义兼容性，停止写入/备份并前滚修复，不自动降级或扩大角色权限。
