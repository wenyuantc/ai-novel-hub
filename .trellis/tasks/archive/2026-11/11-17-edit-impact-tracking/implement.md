# 执行计划：修改影响追踪

依据 prd.md 与 design.md。切片内只跑受影响测试加哨兵；完整回归在 I4 收口执行。不执行破坏性 git 命令；不改 `collaboration_ai_guard.py`、`task_service.py`、`project_ai_config.py`、`ai_dispatch.py`、`stream_lease.py` 与任务引擎核心的既有分支行为（登记新种类除外），必须改时先停下说明。

## 开始前
- [ ] 阅读 `.trellis/spec/backend/consistency-check.md` 全文与子任务 1 的 design.md 第 14 节、check-report.md「已知限制」。
- [ ] `git status` 确认工作区：子任务 1 的改动尚未提交，与本任务在同一工作区叠加；不回退、不整理他人改动。

## I1 数据与第一级（T1、T2、T8）
- [ ] 迁移 0059：台账三列、`consistency_impacts` 表；模型、schemas；`chapter_delete_guard` 登记；0058 冻结结构测试按需排除新列。
- [ ] `save_ledger` 保留上一版；补测试（哈希不同才移动、相同不移动）。
- [ ] `services/consistency/impact.py`：单章与作品级的第一级判定（摘要、记忆、设定变化），记忆判定复用 `memory_service` 的既有逻辑。
- [ ] 备份 v13：注册表、重映射、`_migrate_v12_to_v13`、版本常量与相关测试。
- [ ] 测试：改章后三类分别过期、其他章不受影响、重新分析后摘要与记忆提示消失、空哈希旧记录不误报、批量查询不逐章；迁移（SQLite + PostgreSQL 用例）；v12 导入、v13 往返。

## I2 评估影响任务与接口（T3–T7、T9）
- [ ] 模板 `consistency.impact` 与 schema，登记注册表并更新键数量。
- [ ] 差异：确定性比较、模型判定、输出校验；受影响章节聚合与 `uncovered`；确认状态继承。
- [ ] 任务 `consistency.impact`：受理（`ai.use`、模型预检、无改动前台账 400、与检查互斥 409）、授权重验、落库；登记任务种类的各处白名单（对照子任务 1 登记过的位置）。
- [ ] 四个接口；确认状态 CAS。
- [ ] 测试：佩剑示例（改第 5 章后评估，第 12 章进入清单并带证据）；只改错别字 → 未发现变化且不列章节；台账已新鲜时不调用抽取；无改动前台账 400；后续章节无台账进入 `uncovered`；重新评估继承确认；被列章节改动后 `changed`；评估后本章再改 `outdated`；评估前后 `consistency_issues` 不变；查看者 403、非协作者 404、编辑者可评估、撤权后不落库；协作路径的 PostgreSQL 用例。

## I3 前端（T1–T7）
- [ ] API 与类型、`TaskKind` 与任务面板映射、`transfer.ts` 表名标签。
- [ ] 「修改影响」页签、单章详情抽屉、评估进度、确认操作、快捷发起范围检查。
- [ ] 章节编辑页的过期提示（保存后与进入时读取，失败静默）。
- [ ] 测试：计数与文案的纯函数、确认请求体、冲突保留、只读显隐、编辑页提示的显示条件。

## I4 收口（含父任务）
- [ ] 全范围 trellis-check（两个子任务合并后的改动）。
- [ ] 规范 `consistency-check.md` 补影响追踪契约；project-transfer、prompts-styles、索引的版本与键数量；README；TODOS.md 第 14 项第四条与当前状态。
- [ ] 完整门禁：后端 ruff + 全量 pytest（SQLite）、PostgreSQL 一致性用例、前端 lint／build／test。
- [ ] 浏览器验收（隔离数据 + 本地假模型）：父任务 AC-P1–AC-P4；并复验子任务 1 修复后未复验的三项（证据高亮、协作者进度流兜底、键盘焦点）。
- [ ] 如实记录未覆盖项。
