# 系列作品与跨书共享设定 · 实施与验证计划

状态：作者已审阅 [PRD](prd.md) 和 [设计](design.md) 并明确授权，任务已进入实施。上下文分别登记在 `implement.jsonl` 和 `check.jsonl`。

代码与质量检查已完成，验证详见 [verification.md](verification.md)。工作提交 `357071f` 已合并 main；作者明确要求合并后不重跑测试。正式数据库迁移留待部署。

这是一条必须一起交付的流程（系列分组 → 档案 → 本书使用 → 安全调整归属），保留一个 Trellis 任务；以下为依赖顺序与内部验收点。存储、授权、旧入口守卫、传输和 UI 都完成前不对外放开系列写入。

## 0. 启动门

- [x] 核对用户批准的是当前完整方案；记录工作区基线，不动无关文件。确认 Alembic 唯一 head、后端测试隔离方式、可执行真实 PostgreSQL 并发测试的专用库。
- [x] 加载任务清单，约定系列 API 类型、绑定约束、字段白名单和写锁序；不要将同一核心服务拆给重叠所有者。按 `Active task: <path>` 协议给实现／检查代理上下文。

## 1. 数据不变量与 owner 管理（AC1、AC5）

- [x] 新增 migration、ORM、schema、service 和 v1 路由；先写升级与约束测试：已有作品默认独立，一书一系、同 owner、同系列且同类型实体绑定、稳定 ID、无损降级限制。
- [x] 完成系列列表／详情／创建／改名、空系列删除、加入／退出的权限与 CAS 骨架。严格 owner；为正常书库／概览补批量系列投影，不改已有书库统计、权限和阅读进度。正常删除、回收、恢复与 admin 删号路径测试先行。
- [x] 为单书备份注册表登记新项目相关表的排除原因；系列用户级表不注册。临时切点：只新增空表时无旧数据迁移，未暴露系列写入即可安全退回。

## 2. 基础档案、采用与归属转移（AC1、AC2、AC4）

- [x] 统一 shared 字段白名单与 schema；新增权威档案 CRUD、采用、提升及来源预览，复用现有时间线初始化／重物化。验证用已确认 baseline 而非章末物化值、legacy AI 确认、保留源 ID、同名不合并、重复采用唯一。
- [x] 锁定系列行 → AI 出站门禁（多书排序）→ 项目生命周期写锁；以两连接控制竞态验证并发重复加入、两书草稿冲突、软删／恢复或彻删与系列编辑、系列编辑中一个投影失败整体回滚。普通单书路径不反向取系列锁；不在持锁事务里发外部请求。
- [x] 做引用预检、影响摘要、取消采用／退出、移回、条目删除。覆盖在回收站、仅大纲快照、未建章细纲和待关联规划中的引用，以及停用事件、字段和 AI 边；移回目标 ID 保留。使用可控屏障测试预检后另一事务新建／恢复引用，不能两边同时成功产生悬挂引用。空白投影与纯文本提及不阻断。

## 3. 保护旧入口并接入写作（AC2–AC4）

- [x] 先补反例，再在真实写原语封住普通 CRUD、基础 PATCH、AI 策略写入、向导受理与覆盖落库、旧分析确认／撤销、Agent Core effects、卡片同名映射与拆书同名复用。依据 [投影写入调查](research/projection-write-boundaries.md) 区分直接写共享键与合法的书内事实；无绑定的作品保持旧行为。
- [x] 采用后复用本书目录、角色出场、规划 POV／世界观、字段／提及匹配和章初／章末投影。验证 A 与 B 给同一角色记录不同事件，修改共享基础后分别重算，书内追加、替换、撤销不反写系列或其他书。
- [x] 实际投影变化同时推进受影响书的发送戳、适用的历史封存与读版本。以截获的 Provider 请求证明：改为「永不加入」或隐藏后，A／B 并发的旧负载、重试及旧对话不会继续发送相应结构化设定；本书手选、字段和一跳不绕过。已显式采用和仅浏览分别覆盖，另书私有来源永远不进入此书。
- [x] 用锁内版本复核保护旧 Agent 确认和人工撤销、旧任务、待分析；验证合法书内撤销可重算，不因整行投影有派生值就误拒绝。安全守卫失败不得提交半套变更或给旧载荷重签。

## 4. 单书迁移兼容与发布准备（AC5）

- [x] v6 往返：成员书含绑定角色／地点、细纲快照、字段引用、AI 策略、独立剧情时，导出到不同实例或账号仍完整可读；未采用的条目和其他书信息不入文件，导入无系列关联。不接受伪造绑定字段。并发更改投影的导出不能生成可导入的混合内容。
- [x] 旧 v1–v6 合法作品、旧卡片仍通过；已选共享角色的卡片只导出独立快照。隔离实例整库备份／恢复验证系列表与成员关联，核对恢复时 DB 完整性、回收恢复和级联。开放 API 回归所有列表／详情及关系、组织成员等派生入口：只读获授权书的本地资料及已采用投影，不含系列元数据和其他书内容。
- [x] 已在测试库完成迁移与恢复验证，未改生产库。上线时仍须先备份、停止旧执行器再切换；已有系列数据不允许有损降级，不用单书 JSON 当整库回滚点。

## 5. 前端入口与集成验收（AC1–AC5）

- [x] 书库系列入口、成员管理、作品系列摘要、共享库分页和空态；保留作品列表原搜索、排序、阅读、统计和新建。角色／世界观页归属筛选、只浏览与已采用状态、详情分区、书内采用、提升、冲突提示与安全移回。
- [x] 首次采用解释 AI／本书权限，破坏性操作展示影响摘要；列表、弹层和深链确保不会把 series ID 当本书 ID。通过 API 测试和 UI 状态测试覆盖换书/换身份、旧请求、版本冲突保留草稿、部分资源刷新、无权限和回收站。
- [x] 使用真实浏览器走 A／B／非成员 C 的主流程，验证切换作品后的新鲜数据、既有出场和规划、备份提示、移动端筛选与弹层，以及没有绑定时旧界面可用。不用截图代替行为校验。

## 6. 命令与全量门禁

| 验证 | 位置与命令 |
| --- | --- |
| 后端增量 | `backend/`：`uv run pytest -q tests/test_series.py tests/test_series_projection_guards.py tests/test_series_transfer.py tests/test_series_migration.py tests/test_planning.py` |
| 高风险回归 | `backend/`：`uv run pytest -q tests/test_planning_lifecycle.py tests/test_setting_progressions.py tests/test_progression_agent_effects.py tests/test_progression_writeback.py tests/test_setting_ai_context.py tests/test_ai_visibility_dispatch.py tests/test_wizard.py tests/test_cards.py tests/test_book_import.py tests/test_mentions.py tests/test_project_transfer.py tests/test_open_api.py tests/test_recycle_bin.py tests/test_recycle_bin_tasks.py` |
| PostgreSQL 并发与外键 | `backend/`：`tests/test_series_postgresql.py` 用 `AINOVEL_SERIES_POSTGRES_TEST_URL` 指定本机专用 `series_test/series_verify` 库，测试使用独占随机 schema。运行 `uv run pytest -q tests/test_series_postgresql.py` 时须启用该 fixture 并核对无 skip；跨事务引用竞态与级联不能只用 SQLite 推断。 |
| 后端全量 | `backend/`：`uv run ruff check .`、`uv run pytest` |
| 前端全量 | `frontend/`：`npm run lint`、`npm run test`、`npm run build` |

- [x] 检查 migration、绑定写入、导出与回滚的两个数据库证据；聚焦审查权限、失效代次及跨项目 SQL 查询。补充本任务的可执行规范、README 功能说明与 `TODOS.md` 第 6 项实际实现状态；仅在全部验收通过后勾选。记录延期项和未验证项，不把规划或 skip 记成完成。
