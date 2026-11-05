# 实施记录

## 2026-11-03：任务启动与早期检查

- 用户审核最终规划后回复“开始”，任务由 planning 转为 in_progress，工作分支为 `codex/setting-progressions`。
- 修改前前端基线：`npm run lint`、`npm run build` 通过；`npm test` 为 95 个文件、732 项通过。
- S1 纯求值器：先复现缺模块，再实现；有界审查额外发现排序负值与嵌套ID溢出，均先加失败测试后修复。最新定向测试 37 项通过，相关文件 Ruff 通过。
- S2 数据库迁移：`tests/test_migrations.py` 25 项、`tests/test_relationships.py` 18 项通过；相关文件 Ruff 通过。未运行真实 PostgreSQL。
- S3 世界观：首轮4项HTTP回归及既有CRUD回归通过；接口／角色初始化等后续工作进行中。
- S9 局部改写：提示词兼容与运行时目标章节设定首轮通过，`test_rewrite.py` 18 项、`test_styles.py` 15 项通过；最终仍需随全量集成复测。
- S11 章节锚点：`test_chapter_numbering.py tests/test_outline_pro.py` 20 项通过；结构化主体物化和智能体注册表须后续接入并复测。
- 备份 v2 首轮：`test_project_transfer.py` 22 项、`test_project_cover.py` 11 项通过；全部主体物化钩子仍待接入，尚不代表最终备份验收。
- 项目规范说明 macOS 下 Alembic 中文配置可能受默认ASCII locale影响；后续完整测试使用 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。`UV_CACHE_DIR=/private/tmp/ainovel-progressions-uv-cache` 避免默认缓存目录的沙箱权限问题。

## 2026-11-04：跨层联调与质量检查

- 完成五类主体的事件、基线、详情、按章上下文、分析写回、章节位置、项目备份 v2、角色卡 v3 和智能体效果接入。拆书、向导和卡片创建入口也建立基线；向导覆盖会检查受保护历史。
- 独立审查发现查询整数越界可能返回 500、删章后当前会话的事件外键缓存可能过期，已分别补真实 HTTP 和同事务回归并修复。五主体与章节组合 85 项通过。
- 真实隔离浏览器：世界观第 5 章追加、第 20 章章末替换，验证章初／章末及第 21 章结果；基础未保存草稿在事件保存后保留。角色关系建立前后、组织成员章末退出、已卸下职业的计划章节预览、旧资料提示与校准、只读账号查看／禁写均已检查。职业切片另验证 375px 暗色、冲突跳转与草稿恢复。
- 前端首轮全量 `npm run lint`、`npm run build` 和 809 项测试通过；随后复核出关系面板刷新失败可能丢草稿及只读提示文案问题，修复与最终全量复测进行中，不把首轮结果记为最终门禁。
- 后端 Ruff 全量首轮通过。全量测试首轮发现迁移号旧断言和 Gemini 单值常量兼容问题，已修复并复测相关 63 项；自动续写／批量／建议重写补充 3 项真实 FakeLLM 时点回归。进一步复核 Gemini 新工具的参数结构，修复及最终全量测试进行中。
- SQLite 迁移及 PostgreSQL 方言编译已通过；本会话另启仅本机访问、内存数据目录的一次性 PostgreSQL 16 测试容器，真实 PG 迁移和集成验证进行中，不连接用户数据库。
- 前端最终 `npm run lint`、`npm run build` 通过，`npm test` 为 109 个文件、813 项通过；计划章职业预览、关系刷新失败保护和只读查看文案均已补回归。真实浏览器进一步验证自定义字段按章独立替换，以及隔离 FakeLLM 的第 5 章局部改写预览与采纳。
- 真实 PostgreSQL 16 测试发现删章同时级联分析来源时的 FK 触发顺序问题；来源 FK 改为 `ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED`。真实 0030→0031 升级覆盖五类旧基线、删章、引用约束、级联及安全降级；HTTP + PG 覆盖章初／章末、自定义字段、v2 备份往返、孤儿与跨项目隔离。最终 PG 测试补充断言进行中。
- 沙箱默认禁止环回监听；经自动审批在隔离本机环境复跑原有 MCP 协议测试 21 项通过。后端最终完整 Ruff 已通过，获得环回授权的完整 pytest 正在运行；Gemini 检查使用真实 SDK mock transport，未请求真实模型。
- `test_migrations.py` 启用隔离 PostgreSQL 后 26 项通过；额外的替代事件级联单项通过。真实 PG 与浏览器临时服务均已停止并清理。
- 先前后端全量结果：1785 passed、1 skipped（未配置 PG 测试地址）、1 failed（阅读器断言仍写 0030）；已将该精确断言改为 0031，阅读器 7 项复测通过。鉴于 Gemini/provider 和引用校验也在该进程启动后修复，覆盖最终代码的第二次完整 pytest 正在运行，不能把第一轮记作全量通过。最终代码的智能体/provider 相关测试 241 项通过（1 条 Gemini 图片 SDK 弃用提示）；随后增强真实上限 ID 后，相关定向回归 4 项通过。
- 使用隔离 SQLite、FakeLLM 和最终构建补做浏览器切换验证：山门与竹林各自显示自己的基础值；竹林第 20 章章初、章末分别显示变化前后内容。章节编辑器依次切换第 5、20、21 章再切回 20 章，世界观与角色状态均显示对应章初值。临时服务、数据库和测试脚本已清理。
- 最终后端完整门禁：`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 UV_CACHE_DIR=/private/tmp/ainovel-progressions-uv-cache uv run --no-sync pytest` 在 `backend/` 运行，**1790 passed、1 skipped、1 warning，796.84 秒**。跳过的是未配置 `AINOVEL_POSTGRES_TEST_URL` 的可选 PG 用例，已另用隔离真实 PostgreSQL 16 运行迁移文件并取得 26 项通过；警告为 Gemini 图片 SDK `generate_images` 弃用。最终代码的后端全量 Ruff 通过。
