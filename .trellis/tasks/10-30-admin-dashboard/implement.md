# 管理员首页与全站统计：执行计划

状态：用户已在最终规划摘要后明确批准“开始”，已激活任务；实现、修复、全范围质量门禁及规范同步均已完成；用户已批准提交及收尾，并追加合并main和推送；代码提交已完成，正在完成文档和收尾。

## 1. 前置门禁

- [x] 用户确认完整首页范围、包含回收站的总量、全部账号、独立累计任务统计、历史缺口规则和时间刷新配置。
- [x] PRD 收敛：需求 R1–R7、验收 AC1–AC12，无阻塞产品问题。
- [x] 调研所有已发现 Task 创建/终态/清理入口、启动与测试约束。
- [x] 完成设计和本执行计划，整理 implement/check 上下文清单。
- [x] 展示最终规划摘要后取得新一轮明确实施批准。
- [x] 在批准后激活本任务，读取 Phase 2.1 详细指南，按 Trellis 实施/检查子代理协议执行。

不要创建重复任务，也不要把“可以”对局部口径的认可当作最终实施批准。工作区原有 `.codex/config.toml`、`.gitignore` 及两个 source-command 技能目录不是本任务改动，不覆盖、不一并提交。

## 2. 执行顺序与所有权

各阶段按依赖推进，避免多个执行者同时改 Task / main / 共享类型。实际以 `research/implementation-boundary.md` 中互斥所有权执行：tracking先建立共享模型，dashboard_api与dashboard_frontend按冻结契约并行；最后统一全范围检查。每次派发开头使用 `Active task: .trellis/tasks/10-30-admin-dashboard`，明确文件责任、不得回退他人改动，并由 trellis-implement 执行。原有代码不足时允许在该阶段责任范围内做最小必要调整，不做全站重构。

### 阶段 A：模型、迁移与事务统计基元

责任：`backend/app/models/dashboard.py`、模型导出、Task 的 statistics_key、迁移0025、`task_statistics_service.py`、新增统计/迁移测试。

- [x] 建立独立统计身份、跟踪起点、baseline、新建/结束/移除字段及索引。
- [x] 增加可空身份列与唯一索引，不为升级重建 SQLite tasks。
- [x] 实现幂等初始化与无内部提交的统计 helper，终态只记录一次。
- [x] 测试 UUID 防整数ID复用、同事务回滚、baseline不冒充历史、空库与存量升级、数据存在时拒绝破坏性降级。
- [x] 验证模型导出使 create_all 测试和迁移路径一致；实例统计不污染项目备份。

### 阶段 B：完整接入任务生命周期与移除

责任：`task_service.py`、`task_engine/engine.py`、`analysis_service.py`、`kb_index_service.py`，以及 `admin_service.py` / `recycle_bin_service.py` 的统计移除接入和对应测试。

- [x] 覆盖通用创建与重试、批量/自动分析、记忆索引的直接插入。
- [x] 覆盖领取、暂停/继续、结束待审核、所有取消分支、失败/成功/中断和启动恢复。
- [x] 保留原父行锁、资源键、事务所有权与审计；统计失败不能被误译为资源冲突。
- [x] 接入单删/清理/裁剪/作品彻删与到期清理/用户删除；统计不被级联抹掉。
- [x] 用 Gate 或可控事务测试并发结束/取消、重复 finalize、恢复重跑；证明累计与结果不重复、回滚不记账。
- [x] 未完成即移除不产生伪终态，不使后台出现虚假的在途数量。

### 阶段 C：采样器、聚合服务和管理员 API

责任：`admin_dashboard_service.py`、采样器、schema、dashboard router、api_router注册、`main.py` 启停，以及相关后端测试。

- [x] 用无生命周期过滤的 SQL 标量聚合所有账号、全部未彻删作品、当前章节/字数和 pending 投稿。
- [x] 实现启动立即采样与60秒独立采样，按北京时间维护当日最后成功值，错误不写零、不回填漏采日。
- [x] 在恢复任务之前初始化统计；正常停机先关闭采样器再释放数据库，测试显式初始化与生命周期清理。
- [x] 提供 overview/tasks/history 三个独立 AdminUser API，schema与设计契约一致。
- [x] 每日任务流量读取持久记录，最近24h使用统一UTC时刻；7/30/90日查询固定北京时间边界，非法值422。
- [x] 缺失、首日/当日不完整、净变化比较点不足、真实零与负净变化均有明确响应。

### 阶段 D：前端页面、入口与历史展示

责任：`frontend/src/api/types.ts`、`api/adminDashboard.ts`、dashboard页面及局部组件/轮询/纯函数、router、AppLayout、WorkshopPage tab解析与对应测试。

- [x] 三个API模块分别加载/失败/重试，保留旧值时明确过期，不能 `undefined ?? 0` 冒充成功。
- [x] 管理员专用懒加载页面、侧栏/账户菜单/面包屑；不改变普通用户默认首页。
- [x] 全站规模、工坊待办、24h异常、实时任务和累计结果摘要；标注含回收站、初始化存量与统计起点。
- [x] 7/30/90日切换、规模趋势与净变化、每日任务新建/结果图表、可读历史数据表。
- [x] SVG遵守主题，缺口断线，单点/零值可读；不引入重图表依赖或硬编码颜色。
- [x] 手动+60秒刷新、开关、隐藏暂停/恢复，旧请求失效、卸载与账号/权限变化清理。
- [x] 工坊 review 深链接正确且受权限约束；总量的正常作品/回收站跳转语义明确，不加未交付任务监控链接。

全范围检查追加了原UUID贯穿SQL/Hub/运行体/输出的保护，以及 `generation_service._save_chapter_result` 的有界保存取消保护；这些修复落实AC8/AC12，不改变冻结HTTP字段或扩展任务处置功能。可重复证据与取舍见 `research/bug-analysis.md`。

## 3. 验证矩阵

| 组别 | 必须覆盖 | 对应AC |
| --- | --- | --- |
| 规模 | 管理员/普通/禁用账号；多个作者；回收/恢复/删除中/彻删；不同章节状态及非空白字数；版本不重复 | AC1、AC7 |
| 权限与入口 | 未登录401，普通用户403，禁用/降级即时失效；已交付入口及工坊审核深链接 | AC3、AC4 |
| 历史 | 北京时间零点±1秒；7/30/90日；起点前、停机漏采、采集失败、首日/当日、单点、零值、负净变化 | AC6、AC9 |
| 任务 | 所有创建路径、paused继续/提前结束、retry、成功/失败/取消/中断、重启恢复、跨日结束、滚动24h | AC8、AC10 |
| 持久化 | 清理超过200条、普通删除、purge/expire、用户删除、SQLite ID复用、事务回滚、竞争/重复回调 | AC8、AC12 |
| UI状态 | 部分失败、独立重试、旧值过期、乱序响应、范围切换、轮询开关、hidden/visible、卸载/账号变化 | AC2、AC11 |
| 可访问性 | 375px/桌面、明暗主题、键盘、SVG语义与数据表、无页面横向溢出 | AC5、AC12 |

建议新增后端 `test_admin_dashboard.py`、`test_task_statistics.py`、`test_admin_dashboard_migrations.py`；任务统计集成回归可拆独立文件，避免让原大型套件难维护。前端新增 API/纯函数/轮询逻辑测试及轻量静态UI契约测试；当前 Vitest 是 node 环境，不为本任务改造全站测试环境或增加 jsdom。

各阶段先运行覆盖改动的定向测试，再运行完整质量门禁。命令分别在对应目录执行：

```text
backend/: uv run ruff check .
backend/: env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest
frontend/: npm run lint
frontend/: npm run build
frontend/: npm test
repo/: git diff --check
```

新库迁移、0024存量升级和空库downgrade使用 tmp_path 临时数据库，不执行开发者真实数据库迁移。PostgreSQL路径应在可用测试环境验证；环境缺失时明确记录，不能把SQLite通过当作PostgreSQL已经运行通过。可用浏览器工具时进行375px与桌面、浅/暗色真实渲染及上述交互检查，记录实际使用的工具与结果；不能用代码字符串测试代替浏览器验收。

## 4. 全范围检查与收尾

- [x] 按 Phase 2.2 派发 trellis-check，对全部变更做跨层、事务/迁移、权限、数据准确性、主题与竞态检查；不是只检查最后一个阶段。
- [x] 核查 R1–R7 与 AC1–AC12 均有实现和证据；未运行的检查如实标注，不能声称全通过。
- [x] 主会话按需补充 `backend/admin-dashboard.md`，更新后端/前端索引及数据库、组件规范中相关真实契约。
- [x] 仅对本任务文件组织提交，遵循中文 Conventional Commits；原工作区配置/技能改动保持不动。
- [ ] 在确认质量与实际交付后再归档任务、记录会话；规划完成不代表功能完成。

最终证据见 `research/check-report.md` 和 `research/browser-verification.md`：后端1211项自然exit0、前端542项、全后端/迁移Ruff、前端lint/两套类型/等价生产构建、真实浏览器均完成。PG实例未运行，标准build权限限制及唯一既有SDK弃用警告已如实记录。提交文件分组与明确排除项见 `research/commit-plan.md`；已获用户提交授权，代码提交为 `415fc45e280fce00dc45ad2e792fffbab58ed49b`，文档及收尾提交随后完成。

## 5. 回退点

- 每阶段发现统计口径或完整性承诺无法满足，先停止下一阶段并回到设计，不偷偷降为保留记录计数。
- 未部署阶段可回退本任务代码；不得重置整个工作树或覆盖他人配置。
- 部署后回退先停止引擎与采样、备份数据库；非空统计库不得直接downgrade。回退期间旧版本写入造成的统计缺口必须保留并披露，禁止生成连续历史的假象。
