# 执行计划：一致性检查与问题清单

依据 prd.md 与 design.md。每个切片结束跑「受影响 + 哨兵」测试；完整回归只在 S7 收口执行。不执行 git 提交以外的任何破坏性命令；提交在阶段 3 统一进行。

## 开始前
- [ ] 读取 implement.jsonl 所列规范；对照 design.md 中引用的代码位置复核行号与签名。
- [ ] 清单中 error-handling、database-guidelines、prompts-styles、component-guidelines、type-safety 超过注入上限会被截断：实施与检查时须直接读取相关章节全文（协作者执行 E4／E5、模板登记、页面模式）。
- [ ] 开始前查看 `git status`；工作区中与本任务无关的改动（仓库常有并发会话）不触碰、不回退。

## S1 数据模型与备份登记（C12 基础）
- [ ] `backend/app/models/consistency.py` 三张表并导出到 `app.models`；迁移 `0058_consistency_check.py`（`down_revision="0057"`）。
- [ ] `schemas/consistency.py`：事实、台账、问题、证据、概要、预览的 Pydantic 模型。
- [ ] project_transfer：`RULES` 加两表、`NOT_EXPORTED` 加 runs、remap 证据 `chapter_id`、`VERSION=12`、`APP_SCHEMA="0058"`、`_migrate_v11_to_v12`。
- [ ] 测试：迁移升降级与冻结结构契约、`test_registry_covers_all_project_tables`、v11 → v12 导入、v12 往返（含证据重映射与映射缺失）。
- 回滚点：迁移与模型独立成组，可单独撤回。

## S2 事实抽取（C2、C11）
- [ ] 纯函数：`subject_key` 归一化、引句定位（码点偏移，含 emoji、重复引句取首个、找不到返回 null）、事实规范化。
- [ ] 模板 `consistency.extract` 与 schema，登记注册表，补 parity 测试。
- [ ] `services/consistency/ledger.py`：新鲜度判断、单章抽取与落库（落库事务内重验指纹）。单章抽取函数签名保持可被子任务 2 直接调用。
- [ ] 测试：FakeLLM 抽取落库、未变章复用不调用模型、指纹变化不保存、关闭 AI 可见的作者区块不出站、畸形输出的容错。

## S3 跨章比对与问题合并（C3、C4、C8）
- [ ] 分组与分批（按 `subject_key`／类别，时间线独立，预算内分批）；伏笔组附带 `include_in_context` 的伏笔记录。
- [ ] 模板 `consistency.compare` 与 schema；输出校验（证据必须引用存在的事实、范围过滤）。
- [ ] `services/consistency/issues.py`：`identity_key`、合并规则、`needs_recheck` 与 `not_reproduced` 的批量计算。
- [ ] 测试：佩剑示例产出一个物品类问题；范围口径（AC3）；三种状态的重复检查表现；`existing_issue_id` 指认与越权 id 被忽略；不存在的事实引用被丢弃。

## S4 检查任务（C1、C10、C13）
- [ ] `services/consistency/check.py`：预览、受理校验、断点、逐章抽取、比对、概要；`handlers/consistency.py` 注册 `consistency.check`，三项协作标志与各检查点重验。
- [ ] 测试：预览计数；互斥 409；`MAX_EXTRACT` 400；取消后台账保留；重试从断点续跑；单章失败不中断；检查期间改章 `skipped_changed`；查看者 403、非协作者 404、编辑者可发起；撤权后停止且不落库。协作路径补 PostgreSQL 用例（需 `AINOVEL_COLLAB_POSTGRES_TEST_URL`）。
- 风险文件：`collaboration_ai_guard.py`、`task_service.py` 只按既有扩展点接入，不改其既有分支行为；若必须改动，先停下说明。

## S5 接口（C5–C7、C9）
- [ ] `api/v1/consistency.py` 七个接口并挂载路由；状态 CAS；单问题重新核查。
- [ ] 测试：筛选与分页（含 `needs_recheck` 过滤后分页）、概要与台账覆盖数值、冲突 409、删章后的证据展示、跨项目与回收站请求被拒。
- [ ] 开放读取接口与管理员任务监控如按种类白名单枚举任务，补登记 `consistency.check`（先 grep 任务种类枚举处）。

## S6 前端（C1、C4–C7、C9、C10）
- [ ] `api/consistency.ts`、类型、路由、导航项。
- [ ] 页面与组件：概要、发起检查弹窗与预览、任务进度、问题列表与筛选、详情抽屉、证据跳转、状态与备注表单。
- [ ] 测试：证据链接生成（有偏移／无偏移／章节已删）、筛选参数、冲突保留草稿、迟到响应丢弃、只读显隐。
- [ ] 任务面板如按种类映射标题或图标，补 `consistency.check`。

## S7 收口
- [ ] 新增 `.trellis/spec/backend/consistency-check.md` 并更新后端、前端规范索引与 project-transfer、prompts-styles 中的版本与键数量。
- [ ] 更新根目录 README（功能说明、备份版本、命令表如有变化）；TODOS.md 第 14 项前三条与当前状态（第四条留给子任务 2）。
- [ ] 完整门禁：
  - `cd backend && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check . && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest`
  - 配置 `AINOVEL_COLLAB_POSTGRES_TEST_URL` 后跑 PostgreSQL 用例
  - `cd frontend && npm run lint && npm run build && npm test`
- [ ] 浏览器验收（隔离数据 + FakeLLM）：AC1 佩剑示例全流程、范围检查、取消与重试、改稿后需重新核查与失效跳转、状态冲突、查看者与编辑者、窄屏与深色主题、备份导出再导入。
- [ ] 记录未覆盖项与实际执行结果，不把未验证项记为通过。

## 开始实施前的检查
- [ ] prd.md、design.md、implement.md 经用户确认。
- [ ] implement.jsonl、check.jsonl 含真实规范与研究条目。
- [ ] `python3 ./.trellis/scripts/task.py start` 指向本子任务而非父任务。
