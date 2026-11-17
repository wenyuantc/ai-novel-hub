# Research: 笔名分组、署名、权限与兼容设计证据

- Query: 在现有多作品、协作、系列和导入导出架构中，怎样实现作者私有笔名库、每书单笔名和安全的只读署名投影。
- Scope: internal；只读代码、规范和测试，未运行产品测试或迁移。
- Date: 2026-11-16。
- 输出范围：本文件。未修改产品代码、spec、其他任务或任务状态；未执行 Git 操作。

## Findings

### 1. 用户已确认范围与本报告建议的边界

用户确认的 D13.2：作品署名＋分组，每部作品选择一个笔名；作者管理，协作者可见所获授权作品的笔名，但不能修改或浏览作者其他笔名。旧作品兼容未设置笔名。

D13.1 模板为配置＋可选设定／大纲，新书正文为空；D13.3 为四类设定本地图片上传。这两项仅作为跨功能约束，不在本报告中展开其实现。

主代理已指定以下**设计建议，供最终方案评审**：`PenName`＋`ProjectPenName` 关联表；作品署名不替换协作／审计中的真实账号；EPUB 默认署名跟随笔名、保留显式 author 覆盖；DOCX 不新增署名；删除笔名保留全部作品（含回收站）；单书备份只保存当前笔名文本；恢复时只在当前用户私库内同名复用；模板建书由用户当前选择笔名，不自动绑定来源笔名。

### 2. 事实源与主要改动落点

| 位置 | 当前事实／规划作用 |
| --- | --- |
| `backend/app/models/entities.py:236` | `Project` 没有笔名列；`:240` 已有 `(id,user_id)` 唯一索引，`:249` 的 User FK 为 RESTRICT。 |
| `backend/app/models/series.py:23` | `SeriesBook` 已示范以共享 `owner_id` 同时约束项目和上级资源属于同作者的双复合 FK。 |
| `backend/app/services/project_service.py:293` | `_to_out` 按 `ProjectOut` 字段读取 ORM 属性；新增响应不能无意把私人库属性直接暴露。 |
| `backend/app/services/project_service.py:349` | `_final_project_projections` 在最终同一查询中复核登录身份、作品访问与系列摘要。 |
| `backend/app/services/project_service.py:421` | 个人书库包含本人和已接受邀请的协作作品；不能将可见书集合视为可分配笔名的集合。 |
| `backend/app/services/project_service.py:478` | 详情输出在返回前重验权限与生命周期；署名应纳入同一最终投影。 |
| `backend/app/services/project_service.py:554` | 普通 PATCH 使用 `content.edit`，允许 editor 和有权管理员；仅已有风格／AI 字段有 owner 额外保护。 |
| `backend/app/services/project_access.py:42` | 统一 access 计算区分 owner／协作者／管理员；`:64` 的 content edit 不是 owner-only。 |
| `backend/app/services/admin_project_service.py:14` | 管理列表返回真实 owner ID／username，不应被笔名替换。 |
| `frontend/src/components/shelf/LibraryBookCard.tsx:26` | 文字书封当前传入 `owner.username`；`:31` 的作者行同样用 username。 |
| `frontend/src/pages/ProjectListPage.tsx:26` | 按 actor ID／授权修订／请求代次重新挂载内容；`:51` 使用纯函数筛选。 |
| `frontend/src/pages/project/OverviewPanel.tsx:107` | 概览人工表单复用 canEdit；笔名输入必须单独按 owner 控制。 |
| `frontend/src/utils/shelf.ts:9` | 当前筛选只有 query/status/sort/view；`:16` 的 localStorage key 不是账号专属。 |
| `frontend/src/components/shelf/CreateProjectModal.tsx:24` | 普通新建通过 `projectApi.create`，可以在现有新建表单中增加当前用户的可选笔名。 |

### 3. 推荐数据模型：不改作品父表

建议新增独立模块 `backend/app/models/pen_names.py`，并在 `app/models/__init__.py` 注册：

```text
pen_names
  id PK（SQLite AUTOINCREMENT）
  owner_id NOT NULL -> users.id ON DELETE CASCADE
  name varchar(100) NOT NULL
  revision integer NOT NULL DEFAULT 1 CHECK revision >= 1
  created_at / updated_at
  UNIQUE(owner_id, name)
  UNIQUE(id, owner_id)

project_pen_names
  project_id PK
  owner_id NOT NULL
  pen_name_id NOT NULL
  FK(project_id, owner_id) -> projects(id, user_id) ON DELETE CASCADE
  FK(pen_name_id, owner_id) -> pen_names(id, owner_id) ON DELETE CASCADE
  INDEX(pen_name_id)
```

- 项目主键作为关联表主键：一部作品至多一条关联；无行表示未设置，老数据无需批量回填。
- 两个复合 FK 使用**同一**非空 owner_id，数据库直接拒绝把 A 的作品挂到 B 的笔名。沿用 `SeriesBook` 的实证模式；不会因为服务漏校验而静默跨作者。
- 删除作品只级联其关联，保留个人笔名；删除笔名只级联关联，保留正文、设定、作品和回收站记录。
- **不可**在 `projects(pen_name_id,user_id)` 上使用普通复合 `ON DELETE SET NULL`：那会尝试同时清空不可空的 user_id。新建关联表也避免对已有大量引用的 projects 父表进行 SQLite 重建。
- 名称建议保存前 trim、拒绝全空白，限制 1–100 字符；同作者 trim 后完全同名返回 409，不跨作者占用名称。此处建议使用大小写敏感的确定性比较，与 `UNIQUE(owner_id,name)` 保持一致；若最终决定忽略大小写，应增加显式规范化键，不能让 SQLite 与 PostgreSQL 采用不同规则。
- `revision` 用于笔名改名／删除 CAS，旧版本返回 `409 stale_revision`。作品关联首版可沿用现有项目元数据的串行、后提交覆盖语义；若要增加关联 CAS，不能用可删除关联行的初始 revision 复用造成 ABA，需要持久的状态版本，属于额外设计而非当前用户要求。
- 不将笔名或别名写进 `Project.user_id`、`User.username`、协作邀请身份、审计 actor、任务执行身份或模型凭据。

### 4. 锁序与事务所有权

现状：`authorization_service.py:220` 的写认证先 `gate` 再按 User ID 加锁；`project_access.py:70` 之后取得项目锁并重读授权。`quota_service.py:41` 的作品创建只锁 owner User，不反向取全局 gate。`admin_service.py:228` 的删号与作品创建共用 User 屏障。

建议分两类调用，避免死锁和提前提交：

| 路径 | 建议锁序与提交 |
| --- | --- |
| 笔名库新建／改名／删除 | 原登录认证 → `authz.require_authenticated(write=True)` 的 gate／User → 私有 PenName 校验／行锁 → 写入／级联 → 一次 commit。 |
| 给已有作品分配／清除笔名 | 原登录认证 → gate／User → 严格 owner 校验及活跃父项目锁 → 当前用户 PenName 存在性复验／关联 upsert 或 delete → 一次 commit。项目锁不能先于 User 屏障。 |
| 普通新建／恢复／模板新建 | 沿用创建入口的 `check_project_creation`（User 屏障）→ 在该锁内查当前用户笔名 → 创建 Project 并 flush → 插入关联 → 外层一次 commit。内部 helper **不得**再取 gate 或自行 commit。 |
| 删除笔名与正在新建／分配竞争 | 两者以同一个 owner User 锁串行；先删则后续显式选择的失效 ID 拒绝，先分配则删除级联解除关联，不产生孤儿或悬空引用。 |

改名无需锁遍所有作品，因为作品读时 join 当前名称；删除的 FK 级联只删关系行，无需锁后再调用只接受活跃作品的修改服务，才能正确处理回收站和删除中作品。

只接受经过认证／当前角色的实际 actor。不能把 `project.user_id` 伪装成调用者，也不能调用普通 `update_project` 后再补查 owner。权限检查期间保持 `db.no_autoflush`，遵守已有未授权暂存数据保护。

### 5. API 与可见性建议

所有私人笔名路由以当前登录用户确定 owner，不接受 `owner_id` 参数。列表与详情只返回本人库；其他用户的 ID 与不存在 ID 统一 404。管理员代管作品不会获得作者笔名库权限。

| 方法／路径（建议） | 请求／响应／关键约束 |
| --- | --- |
| GET `/pen-names` | 本人 `PenNameOut[]`；如返回使用数，只统计本人关联，不返回其他作品名单。 |
| POST `/pen-names` | `{name}` → 201 `PenNameOut`；trim 非空，同作者重复 409。 |
| PATCH `/pen-names/{id}` | `{name,expected_revision}` → 更新后的 `PenNameOut`。 |
| DELETE `/pen-names/{id}?expected_revision=...` | 204；解除所有相关作品关联（包括回收站），不删作品；确认文案明确该效果。 |
| PUT `/projects/{id}/pen-name` | `{pen_name_id:number|null}` → 当前项目输出或最小署名输出；只允许 owner。清除重复执行可幂等。 |
| POST `/projects` 新增可选 `pen_name_id` | 只接受本人有效 ID；未提供兼容无笔名，不改变 quota 检查。 |

响应建议分离账户和署名：`owner` 保持 `{id,username}`；另有当前作品 `pen_name` 摘要或 `author_name`。当前笔名库 ID 无需提供给非 owner；最小投影为 owner 可见 `{id,name}`、其他已授权读者只见名称。不能从署名点击进入作者完整私库，也不能返回该笔名的其他作品列表。

`ProjectOut` 笔名投影应在 `_final_project_projections` 同一授权事实查询中 join 当前关联／名称：列表保持固定查询次数，详情避免先读旧私有名称、后用新协作权限包装。拒绝后端跨用户并不等于前端可以保留旧账户结果。

owner-only 作品关联入口可沿现有 owner-only 能力采用 404；如果选择对已可见但不可写的协作者返回 403，也需在设计中统一并测试。无项目可见性者必须 404。新请求 schema 建议 `extra='forbid'`。不要为了这一接口全局改变既有 ProjectUpdate 的额外字段兼容；也不要把笔名字段接入其 `setattr` 通用循环。

### 6. 前端入口与身份隔离

- 作品书库提供「管理笔名」与「全部／未设置笔名／本人各笔名」筛选。选定笔名时只筛本人作品；「未设置」也只能统计本人的无关联作品，不能把协作者不可见库 ID 的 null 误判成未设置。全部作品保留现有协作书可见性。
- 书架作者行与文字书封使用当前作品笔名，无笔名回退 `owner.username`；协作邀请列表、管理员归属、删除确认的真实账号标识保持原义。现有 `LibraryBookCard.test.ts` 可扩展对署名和账号来源的验证。
- 新建弹窗与作品概览提供 owner-only 笔名选择；协作者和管理员只看当前署名。不要只按 `canEdit` 显示选择器：editor/admin 也有此权限。
- 笔名变化成功后刷新库及可见作品署名，删除当前筛选笔名时回到有效筛选条件；清除筛选需要同时清除新增笔名条件。
- 不在共享 `ainovel.shelf` key 中直接持久化未按账号隔离的笔名 ID／名称。建议新的选择按 actor ID 分区存储，或仅组件内存保存；原有通用视图排序偏好可继续复用。
- `ProjectListPage.tsx:34` 的 key、`api/client.ts:33` 请求代次、`stores/project.ts` 的 scope guard 是现有隔离机制。私人笔名加载／保存还需捕获 actor／代次，旧成功响应也不得写入新账号界面；API 客户端对旧 401/403 的保护不能替代成功响应守卫。
- 若新增 Zustand 笔名 store，加入 `App.tsx:46` 的同步账号变更清理；不需要全局 store 时沿用带身份 key 的组件局部状态更小。
- 笔名独立保存只合并署名字段，不用返回完整 Project 强行重置概览未保存的书名、简介等表单；参考现有封面只合并版本的模式。

### 7. 各创建入口的兼容矩阵

| 入口 | 代码证据 | 建议行为 |
| --- | --- | --- |
| 普通新建 | `project_service.py:525` | 支持可选笔名 ID；校验与关联和原 Project 同一事务。 |
| 灵感转新书 | `inspiration_service.py:645` | 当前直接构造 Project；未选笔名时照常创建、之后在概览配置。无需本次为灵感步骤新增笔名问题。 |
| 拆书导入 | `book_import/service.py:292` | 当前直接建书并检查 quota；未选笔名仍无关联，不自动把上传文件作者元数据当本人的笔名。 |
| 项目恢复 | `project_transfer/importer.py:1407`、`:1594` | 在同一次导入事务中恢复当前笔名文本为本人关联；见下一节。 |
| 模板新建 | 新子任务 | 由新建表单选择本人当前笔名；模板快照不保存来源笔名库 ID，不因同源自动继承笔名。 |

新内部关联 helper 必须提供「不提交」形式；不能先调用现有会 commit 的 `create_project`，再单独写笔名关联，使失败时留下部分新书。

### 8. 单书备份、整库备份和删号

**单书格式建议**：顶层可选 `pen_name: string|null`，只包含当前作品笔名文本；不带原库 ID、owner ID、未使用笔名或作者其他作品。旧格式缺失该字段表示无笔名。

- `exporter.py:267` 在独立 session 中流式导出；将当前项目的单一笔名读取并写入 head，与当前活跃检查衔接。不要枚举笔名库。
- `importer.py:1152` 的预览是用户隔离的 token → 已解析 Backup 对象缓存，**不是内容哈希**。将已校验文本纳入 Backup；无需新增另一套预览机制。
- 在 `import_backup` 的 `check_project_creation` 取得 User 屏障后，按 `(当前 user_id, trim 后 name)` 查找本人的同名笔名；存在则复用，不存在则创建；与 Project／关系同事务提交。另一个作者拥有同名笔名绝不影响匹配。
- 预览可提示「将复用／新建笔名」，但最终按锁内真实状态重算；若并发删除后已不存在，同次恢复可重新新建，不依赖旧库 ID。
- 验证数据类型、长度、空白和未知结构；无效笔名使导入整体失败，不先创建私库残留。旧无字段备份兼容。
- `tests/test_project_transfer.py:53` 的注册表反射沿「引用项目的表」扩散。`project_pen_names` 必须登记为 `NOT_EXPORTED`，理由是通过顶层当前署名文本恢复；`pen_names` 只引用 users，是用户级表，**不要**写入该 NOT_EXPORTED。
- `agent/effects.py:85` 有显式 EFFECT_TABLES；笔名属于 owner 管理元数据，不进入智能体效果快照和撤销。单独关联表也避免 Project 通用快照意外回滚署名。
- 主代理的串行整合建议为：笔名子任务将单书格式升级到 v10（兼容 v1–v9），随后缩略图子任务升级到 v11（兼容 v1–v10）；模板使用独立格式 v1。各子任务明确拥有对应格式／迁移改动，不让半成品共享同一版本号，也不同时维护两个 VERSION 常量。旧应用因版本较高而拒绝恢复，避免静默忽略新署名／图片字段。

**整库备份**：`services/backups/database.py:43` 使用 SQLite 在线 backup，`:109` 使用 pg_dump；覆盖整个数据库，没有需逐表添加的导出白名单。新增表、FK 和索引随原生快照保存；恢复测试应包含笔名与关联数据。不要把文档中的「37 项」误认为表目录，那是管理动作权限数。

**删号**：`admin_service.py:217` 只允许删除已无任何作品（包括回收站）的账号；`:238` 持真实 User 写屏障，私人配置沿 FK CASCADE。新 PenName→User CASCADE 与此契合：有作品时仍阻止删号；作品已清理后笔名随账号删除。不能为清笔名而放宽该保护。

### 9. EPUB、其他导出与开放接口

- 站内 `export_manual_project` 当前默认作者为 `User.username`（`export_service.py:116`）；显式 `author` 为非 null 时保留覆盖，空字符串有既有含义。
- EPUB 的标题页、NCX 作者与 OPF `dc:creator` 都使用传入 author（`epub_service.py:202`、`:276`、`:310`）。推荐默认优先当前作品笔名、未设置回退真实作者 username；保留 author 覆盖及现有最大长度。
- 开放接口 `open_api/catalog.py:178` 最终在 `:199` 显式传 `principal.username`。若只改站内默认，开放下载仍旧署账号名；应通过同一个「当前作品默认署名」内部 helper 修正这一路默认，不新增私有笔名 API。
- 开放 `Novel` schema（`schemas/open_api.py:57`）当前没有作者或笔名字段；本任务无需扩展公开小说 JSON 契约，仅保证已有 EPUB 的作者展示一致。
- DOCX/TXT/Markdown 组合导出使用 `DocumentModel(nodes,warnings)`（`document_export/model.py:20`），`renderers.py:61` 当前不设置 author 元数据；保持该行为即可，不因笔名任务新增封面页或署名字段。
- 笔名不是 AI 写作提示内容；`project_service.project_info` 当前不含 author，首版不要求新增 AI 注入或 AI 可见性版本变更。

### 10. 迁移与回滚建议

- 调查时磁盘已有 `0053_agent_summary_source.py` 和 `0054_agent_conversation_round.py`，分别继承 0052／0053；原 `exporter.APP_SCHEMA` 仍是 0052。新增编号需由主代理在实施前核对实际 HEAD 后分配，不能沿用早期调查假设。
- 新建两张表和索引，不重建 projects；SQLAlchemy 模型和 Alembic 的复合 FK、CASCADE、唯一键保持一致，并验证 SQLite 外键开启与 PostgreSQL 实际约束。
- 从旧库升级应零关联、旧作品可用；空新表可安全降级。存在笔名或关联数据时建议拒绝有损 downgrade，而不是静默丢失署名；回滚优先应用层撤回新入口并保留新增表。
- `quality-guidelines.md:155` 提醒历史 0037 模型冻结测试会受给旧表加列影响。关联表方案无旧 projects 列变化，可降低触发面，但仍要跑迁移和反射契约，不假定自动通过。

### 11. 验证计划（未执行）

建议新增定向 HTTP／迁移测试文件 `tests/test_pen_names.py`、`tests/test_pen_name_migration.py`、`tests/test_pen_names_postgresql.py`，至少覆盖：

1. 私库 CRUD、trim／空白／超长／重复名、不同用户同名允许、改名与删除版本冲突；所有新私有端点跨用户 404。
2. owner 关联、清除、重复清除、无笔名旧书；协作者 viewer/editor、纯管理员和管理员兼协作者不能修改或列私库，但可见有访问权作品的名称。
3. 恶意跨作者关联由 API 和真实 DB FK 各自拒绝；直接 SQL 尝试是检验模型契约的测试，不以服务校验代替。
4. 删除笔名保留正常／回收站／删除中作品内容、解除相应关联；彻删作品保留仍存在的私库；删号保护与合法级联。
5. 书库查询不按作品数 N+1，最终权限撤销拒绝旧署名响应；前端账号切换后的旧成功／失败响应均不污染新账号。
6. 单书备份只含当前名称，不含库 ID 或其他名称；同用户／跨用户恢复各自本地匹配，旧格式兼容，无效字段整笔回滚，quota 失败无新增笔名残留。
7. EPUB 站内及开放下载默认笔名、无笔名回退用户名、显式 author 覆盖／空字符串保持；DOCX现有导出回归。
8. 模板新建显式选择当前笔名，不继承来源；普通新建关联失败不留下半本书；灵感／拆书原流程保持可用。

PostgreSQL 真实并发应双向控制以下提交顺序，使用 gate／事件而非 sleep：

- 笔名删除与项目分配／普通新建／恢复同名匹配竞争：无死锁、悬空 FK 或跨作者绑定。
- 同名笔名创建竞争、相同 expected_revision 改名竞争：一个成功，另一个安全冲突。
- 账号删除／停用先提交与旧笔名写请求恢复竞争；作品软删／purge 与分配竞争。
- 协作撤权先提交与作品列表／详情最终署名投影竞争；不得借旧可见性返回其他资料。

PG fixture 优先复用 `tests/pg_support.py:76` 的本机专用库守卫和 `pg_schema_lease`，账号必须 `collab_test`，公共回退库为 `collab_test`。构造 schema 使用 `tests/schema_helpers.create_isolated_schema`，不能污染共享 ORM metadata。不得把 SQLite 成功或 PG skip 宣称为并发已验证。

定向命令（将新增测试文件实现后执行）：

```bash
cd backend
uv run pytest tests/test_pen_names.py tests/test_pen_name_migration.py tests/test_pen_names_postgresql.py
uv run pytest tests/test_project_transfer.py tests/test_epub.py tests/test_open_api.py tests/test_admin.py tests/test_project_lifecycle.py tests/test_projects_and_story.py tests/test_inspiration.py tests/test_book_import.py tests/test_migrations.py
uv run ruff check .
uv run pytest

cd ../frontend
npm test -- src/utils/shelf.test.ts src/components/shelf/LibraryBookCard.test.ts src/pages/ProjectListPage.test.ts
npm run lint
npm run build
npm test
```

真实浏览器验收桌面和 375px：新建／改名／删除笔名、选书分组和清筛选、owner 与协作者切换、私库入口隐藏、无笔名回退、旧响应隔离、概览草稿保留。截图与网络证据不能用静态字符串测试替代。

### 12. 相关规范与外部资料

应加入子任务上下文的规范：`backend/database-guidelines.md`、`backend/quality-guidelines.md`、`backend/rbac-quotas.md`、`backend/admin-projects.md`、`backend/auth-admin.md`、`backend/recycle-bin.md`、`backend/project-transfer.md`、`backend/admin-backups.md`、`backend/open-api.md`、`frontend/type-safety.md`、`frontend/state-management.md`、`frontend/quality-guidelines.md`（均位于 `.trellis/spec/`）。

本调查未调用外部来源；不需要新第三方库。SQL 模型建议以本仓库 `SeriesBook` 双复合 FK、现有授权与创建屏障为依据，仍需由实施阶段的迁移和真实 PG 测试验证。

## Caveats / Not Found

- 当前未发现笔名对应模型、API 或 UI；报告是设计输入，不是功能实现或测试结论。
- 两个新增表、接口路径、100 字符限制、唯一比较规则、删除交互和复用行为是待最终评审的具体建议，不冒充用户已逐条确认。
- 当前备份预览缓存是单进程进程内 token，不是持久内容哈希；沿用其既有边界，本任务不扩大为分布式导入改造。
- 迁移磁盘状态可能变化，实施前需再确定最新编号和 APP_SCHEMA；不同子任务对备份版本／注册表的改动必须统一安排。
- 未运行 ruff、pytest、npm、数据库迁移或浏览器；不能以本调查替代这些验收。

## 规划对照检查

2026-11-16：仅对照父任务及 `../11-16-pen-name-groups/` 的 `prd.md`、`design.md`、`implement.md` 共六份文档与本报告已核对的代码事实，未发现会阻止实施的矛盾或遗漏。

重点契约已一致：子任务 `design.md:41`–`:45` 保持 gate／User／Project 顺序及新建 helper 不反向取 gate；父任务 `design.md:85`–`:92` 与 `implement.md:15`–`:17` 明确 v10→v11 串行交付；子任务 `design.md:33`–`:35` 仅向非 owner 投影当前名称，`:62`–`:64` 限定备份文本、导入者本人库恢复和双 EPUB 路径的默认署名／显式覆盖。此结论只覆盖规划一致性，不代表迁移、并发、权限或导出测试已通过；未修改规划或运行产品测试。
