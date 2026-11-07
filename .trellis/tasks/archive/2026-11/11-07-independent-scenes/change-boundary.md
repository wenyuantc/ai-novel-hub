# 实施变更边界与协作所有权

2026-11-07：用户在最终方案之后回复“开始”，任务已进入 in_progress，分支 codex/independent-scenes。

## 最小行为差距

目前只有章正文、章级规划与时点；目标是同章独立场景，并让规划、历史、AI 和备份共同识别真实场景。场景正文是新权威，不能只在 UI 切段或把章正文与场景正文独立双写。

## 正确业务位置

- 数据／版本：Scene 模型、scene_service、chapter/version 初始化与保存、迁移、project_transfer。
- 规划／位置：PlanningUnit 和类型化关联、planning_*、progression 模型／schema／resolver／service。
- AI／旧写：generation、rewrite、chapter task handler、提示词、Agent 文本／effects。
- UI：src/api/types 与 scenes API、章编辑器场景工作区、规划网格／矩阵、历史与定位。

## 首轮写代理所有权

| 代理 | 独占写范围 |
|---|---|
| foundation | backend 新 scenes model/schema/service/API、Chapter content_revision／ChapterVersion scene_snapshot、0039 迁移、versions、chapter/plan/book import 等初始场景入口、project_transfer 核心格式／注册／remap 及相应测试 |
| planning | models/planning.py、models/progressions.py、schemas/planning.py、schemas/progression.py、planning_*／setting_progression_*／相关位置消费者与系列引用预检、相关 API 和测试；不得直接改 0039、registry/remap、entities.py |
| frontend | frontend/src 下场景 API／类型、场景工作区、规划场景视图、历史／任务／提及定位及测试；不得改 backend 或包依赖 |
| main | 任务文档、基线／集成验证与最后 README／spec；后续 AI 代理在 foundation 交接后获得 generation/rewrite/task/prompts/agent 的明确所有权 |

Planning 对新模式字段、progression 列和注册表映射的需求要向 foundation 发消息，由 foundation 单点写迁移／注册文件；frontend 等待 foundation／planning 提供确切 schema 后冻结公开类型，不能自创不一致 payload。

## 临时共享接口提议（由 foundation 先发布实际冻结版本）

Scene：id、chapter_id、title、content、summary、sort、separator_before、active。Chapter.content_revision 为章级内容／结构版本。PlanningUnit 新 scene_id/source_level 与按项继承模式，具体模式列由 planning 先发布。

基础层应尽早提供：初始场景添加、按章排序读取、只读场景校验、统一投影／码点区间、外层已鉴权持锁的正文保存原语、结构基线／快照记录。这些是 AI／planning 共用边界，不允许代理分别重建另一套投影或版本系统。

## 明确不做

不跨章移动，不引入富文本、完整自动保存、场景自动分析或新服务架构；不改无关管理员功能／样式，不提交用户原有 source-command skill 目录。不自行缩减已确认闭环，不以删旧断言通过测试。

所有代理不独占代码库，不回退他人编辑，不再派生 implement/check 代理。共享文件需要变更时先协调；主会话负责最终集成和质量门禁。普通重构只允许为实现上述行为必要的最小提取，并以原回归测试证明无额外行为变化。

## E2 临时追加所有权

main已明确将 context_builder.py 的 position/manual_character_ids/source_text 可选参数与有效投影传递交给 planning 独占，先完成场景预览与E2测试；交接后 AI 代理使用冻结接口。如需再改由main协调，不并发改文件。
