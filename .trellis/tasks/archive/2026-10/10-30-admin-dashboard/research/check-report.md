# Phase 2.2 全范围检查报告

日期：2026-10-31（Asia/Shanghai）。检查者：`full_check`。结论：本次范围内发现已修复；最终后端全量、前端全量、lint、前端类型与等价生产构建通过。PostgreSQL真实实例未验证，标准前端build命令受既有目录权限限制，详见限制。

## 范围与依据

- 已读取packages范围、后端/前端两层index、check.jsonl引用规范，以及本任务prd/design/implement、`api-contract.md`、`implementation-boundary.md`、`statistics-evidence.md`；不是只检查最后一块代码。
- 覆盖新增模型/0025迁移、初始化/采样、三GET/schema/时区历史、全部Task直接创建与状态/清理入口、审计/作品级联删除、内存Hub/输出/stream；前端类型/API、页面入口、权限、独立请求、竞态、轮询、图表和移动主题。
- 主会话随后新增的 `.trellis/spec/backend/admin-dashboard.md` 及两层index/数据库/组件入口已与最终UUID、DML、Hub、取消原子保存和移动SVG行为核对，没有发现签名或错误语义漂移。
- 不改HTTP契约，不扩展到全站任务处置或账号整体身份系统；没有提交、归档，也没有覆盖原有配置/忽略文件/两个source-command技能目录的改动。

## Findings（已修复）

### P1：SQLite整数Task ID复用可使旧请求/执行体作用新任务

- 文件：`backend/app/services/task_service.py`、`task_engine/engine.py`、`task_engine/hub.py`、`task_statistics_service.py`、`recycle_bin_service.py`、`backend/app/api/v1/tasks.py`。
- 证据：真实双session交错旧请求加载Task、删无作品用户导致Task级联删除、新任务复用相同整数ID。修复前旧取消可将新任务改为cancelled，却给旧UUID补终态，新UUID统计仍queued。只给finalize加UUID不足以隔离其他DML、refresh与内存收尾。
- 修复：条件更新/刷新/删除/裁剪快照与调度领取绑定原`statistics_key`；`mark_removed`返回同事务锁定UUID集合，删除只使用该集合。Hub和运行执行体显式绑定UUID，旧flush、output、stream、close、cancel、recover与项目forget不能触及同ID新身份。保留queued→running正常取消竞争；无key内部测试构造不能绕过真实绑定校验。
- 回归：`test_task_statistics_identity.py`共17项，使用真实SQLite ID复用，覆盖旧取消/暂停修改/删除/裁剪/恢复/领取/执行、旧或无key Hub、flush/output/stream、跨await收尾与执行体取消。

### P1：取消活跃INSERT RETURNING可残留SQLite写锁，Task与统计无法收尾

- 文件：`backend/app/services/generation_service.py`、`task_service.py`、`backend/tests/test_task_statistics_integration.py`。
- 证据：修复前一次完整全量为1208通过/1失败，取消与完成竞争后Task停在running。事务观测证明HTTP请求实际SQLite事务为False；生成保存事务已UPDATE章节，正在INSERT生成日志RETURNING时被cancel，SQLAlchemy失效/关闭连接后，活返回游标仍可持写锁，随后恢复、on_abort与finalize均database locked。独立sqlite3及SQLAlchemy/aiosqlite可控复现一致；单例复跑成功不能排除此缺陷。
- 修复：仅将正文/版本/生成日志的有界原子保存放入受保护协程，不屏蔽LLM等待。取消不直接打断进行中的SQL；提交前收到取消先完整回滚并释放会话，再进入独立清理/终态事务。commit已开始的极晚取消等待确定保存，产出done、以成功收尾，不声称绝对撤销已提交正文。running取消等待前结束请求侧事务。
- 回归：真实aiosqlite活RETURNING游标与已commit两种Gate；断言请求事务已结束、底层保存SQL未被直接cancel、正文/版本/审计全成全败、Task与独立统计只有一个真实终态、随后独立SQLite写入成功。未改WAL/busy_timeout，未放宽原wait_task超时，未跳过失败或加入重试。临时诊断probe已删除。

### P2：移动端SVG轴刻度因固定大viewBox缩成不可读字号

- 文件：`frontend/src/components/dashboard/DashboardHistory.tsx`、`frontend/src/utils/adminDashboard.ts`及响应式测试（复用既有`useElementWidth`，由frontend实施代理按主会话浏览器发现修复，检查者复核）。
- 修复：按真实容器宽度设置绘图区/viewBox，保留210px图高与12px轴字，90日柱宽/间距动态计算；新增2项动态宽度回归。
- 实浏览器复验：375px轴字包围盒高度不低于9px，无页面横向溢出；证据见`browser-verification.md`。

### 机械问题：0025迁移import排序

- 文件：`backend/alembic/versions/0025_admin_dashboard.py`。
- 原项目默认Ruff排除迁移目录；显式检查发现I001。最终全量结束后，仅交换`import sqlalchemy`和`from alembic import op`的顺序，没有其他语义改动。按主会话批准重跑迁移3项、显式迁移/全后端Ruff和diff检查，均通过。

## 产品要求逐项核对

| 要求 | 真实代码/验证依据 | 结论 |
| --- | --- | --- |
| R1 全站总览 | `_totals`独立标量聚合全部User/Project/Chapter，当前word_count求和；含禁用/管理员、回收站/删除中作品，pending与paused分开 | 通过 |
| R2 入口与权限 | 三GET各自AdminUser；RequireAdmin懒加载，侧栏/账户菜单/面包屑及现有管理入口；工坊review检查管理员；无未交付监控链接 | 通过 |
| R3 加载异常 | overview/tasks/history独立状态；失败保留旧值并标过期、成功时间及重试；真实零与无记录不同 | 通过 |
| R4 趋势历史 | 默认30及7/30/90；UTC时间/北京时间日界；规模净变化与四类任务结果，日期/采样时间及HTML表格一致 | 通过 |
| R5 起点缺口 | 幂等tracking；缺失null/断线；已记录任务事件不被缺失规模采样吞掉；严格首尾基准，缺端点不补0或缩短比较范围 | 通过 |
| R6 独立任务累计 | 全部创建/直接入队、暂停继续、重试、终态/恢复及删除入口同事务接入；清理/裁剪/作品/用户级联不删统计；baseline和removed无伪终态；24h按终态事件 | 通过 |
| R7 刷新适配 | 手动/60秒/关闭/hidden/visible；卸载、范围、401/403使旧响应失效并停轮询；桌面/375px/明暗及数据表 | 通过 |

## AC1—AC12验收矩阵

| 标准 | 自动化及实际证据 | 结论 |
| --- | --- | --- |
| AC1 多用户/作品/任务对账 | dashboard聚合/任务测试；隔离浏览器3账号、2作品、2章节、20字、1待审实对账 | 通过 |
| AC2 空平台/部分失败 | `test_empty_counts_are_real_zero_not_missing`；前端独立失败测试；实浏览器受控tasks500/旧值过期/重试恢复 | 通过 |
| AC3 管理权限 | 三GET401/403参数测试；未登录管理页跳login、普通用户跳/且overview403 | 通过 |
| AC4 正确入口 | UI/工坊深链测试；管理员实进入审核tab，普通用户无审核tab并回browse | 通过 |
| AC5 适配主题 | 1440桌面/375移动、浅暗实截图；图表响应式回归和移动字高/无溢出复验 | 通过 |
| AC6 真实历史及缺口 | 北京时间/空值/任务事件缺采样测试；SVG分段、分页精确表；浏览器90天缺口/无记录 | 通过 |
| AC7 回收/恢复/彻删与快照 | `test_recycle_restore_keep_totals_but_purge_changes_current_not_snapshot`及回收站全量 | 通过 |
| AC8 清理后累计不丢 | prune/clear/purge/expire/delete_user统计测试及真实业务回归；pause/resume/retry测试 | 通过 |
| AC9 范围、日界和净变化 | 7/30/90、默认30、422、跨北京时间零点、负值、缺首尾/单点测试 | 通过 |
| AC10 每日事件/24h/baseline | 独立事件来源、跨日终态及精确24h边界测试；清理后统计保留 | 通过 |
| AC11 刷新及旧请求隔离 | 控制器轮询/hidden/visible/范围乱序/卸载/401/403测试；浏览器真实60秒推进、关闭超过3分钟不更新 | 通过 |
| AC12 事务、恢复、重复与可访问数据 | 创建/终态回滚、竞争finalize、重复终态/恢复、17项UUID复用及2种取消保存屏障；缺口断线/可访问HTML表 | 通过 |

真实浏览器由主会话执行；本报告复核其记录，不冒充检查者独立操作。隐藏恢复等未手工执行的组合竞态明确由自动化覆盖，详见`browser-verification.md`。

## Verification（实际命令与结果）

### 后端（工作目录backend）

1. 最终完整测试：

   ```sh
   env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest -o cache_dir=/tmp/admin-dashboard-final-pytest-cache
   ```

   **1211 passed，1 warning，525.06s，进程自然退出exit 0**。运行时获准本地测试权限，仅用于绑定本机随机端口的隔离MCP假服务，不访问真实模型/外网。

2. 修复后受影响定向：

   ```sh
   env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest tests/test_tasks.py tests/test_task_statistics.py tests/test_task_statistics_identity.py tests/test_task_statistics_integration.py tests/test_chapters.py tests/test_chapter_versions.py tests/test_revise.py tests/test_batch_generate.py tests/test_recycle_bin_tasks.py -q --tb=short -o cache_dir=/tmp/admin-dashboard-final-targeted-cache
   ```

   **125 passed，64.21s，exit 0**。

3. 全后端含显式新迁移lint：`.venv/bin/ruff check . alembic/versions/0025_admin_dashboard.py`，通过。
4. 迁移纯import调整后：`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest tests/test_admin_dashboard_migrations.py -q -o cache_dir=/tmp/admin-dashboard-final-migrations-cache`，**3 passed，2.00s，exit 0**。
5. 后端没有配置mypy/pyright独立类型门禁；不将Ruff冒充Python类型检查。

保留失败历史：首次沙箱全量1188通过/4失败，四项均为MCP fake绑定本地socket的PermissionError，获准本地权限后MCP21项通过；随后真实全量1208通过/1失败暴露RETURNING锁问题，已定位修复并重新完整运行得到上述1211通过。没有删除/跳过失败用例或沿用失败前结果。

唯一warning来自既有`test_image_providers.py::test_gemini_current_sdk_imagen_unavailable_is_chinese`调用SDK `generate_images`的弃用提示；与本任务无关，未屏蔽。

### 前端（工作目录frontend，响应式修复后的最终源码）

- `npm run lint`：通过，exit 0。
- `node node_modules/typescript/bin/tsc -p tsconfig.app.json --tsBuildInfoFile /tmp/admin-dashboard-check-app.tsbuildinfo`：通过，exit 0。
- `node node_modules/typescript/bin/tsc -p tsconfig.node.json --tsBuildInfoFile /tmp/admin-dashboard-check-node.tsbuildinfo`：通过，exit 0。
- `env HOME=/tmp/admin-dashboard-vitest-home npm test -- --configLoader runner --cache=false --fsModuleCache=false --maxWorkers=2`：**71文件/542测试通过，23.28s，exit 0**（含新增移动响应式2项）。
- `node node_modules/vite/bin/vite.js build --configLoader runner --outDir /tmp/admin-dashboard-check-frontend-dist`：等价生产构建通过，exit 0；管理员页面懒加载chunk约16.73KB，未引入重型图表依赖。
- 浏览器验收使用frontend实施代理从相同最终源码构建的`/tmp/admin-dashboard-frontend-dist`，与检查者产物互不覆盖。
- 仓库根目录`git diff --check`：通过。

## Findings（未修复）与验证限制

没有遗留已确认、范围内未修复的产品缺陷。以下不是已验证通过项：

1. **PostgreSQL实例实跑未执行**：本地Docker daemon不可访问。迁移/SQL方言及Task→统计锁顺序做了静态核对，SQLite真实测试不能代替PostgreSQL迁移/并发实跑。
2. **标准`npm run build`未通过**：既有`frontend/node_modules/.tmp`与`frontend/dist`为root所有不可写；未改权限或改依赖。上列两套TS配置检查与Vite使用/tmp缓存/产物的等价生产构建通过，不能写成标准命令通过。
3. `uv run`的默认用户缓存目录受限；使用仓库现有`.venv`执行门禁，没有修改依赖或放宽业务验证。
4. 统计从初始化起记录，不回填上线前或停机漏采历史；部署仍为既有单进程，不支持新旧版本同时调度。这是已批准边界，不是伪造“完整历史”。

最终测试结束后唯一代码变化是已说明且定向复验的迁移import排序；其余是本报告/主会话规范记录。没有为取得通过结果而中途修改测试中的产品行为。
