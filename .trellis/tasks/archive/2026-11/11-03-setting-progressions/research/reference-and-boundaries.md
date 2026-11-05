# Research: 参考行为与本项目边界

- Query: 第 1 项的最小完整交付范围，以及章节级实现需要明确的行为。
- Scope: mixed
- Date: 2026-11-03

## 已获用户确认

本期为章节级回溯，支持追加／替换、最小文本自定义字段、角色状态／关系／职业阶段、生成／重写与详情维护。独立场景及完整字段配置分别留给 TODOS 第 7、4 项。本确认不等于批准技术设计或开始实现。

## 官方参考

已读取：

1. [Progressions/Additions](https://www.novelcrafter.com/help/docs/codex/progressions-additions)，页面标记更新于 2025-03-17。变化从所挂接的故事位置开始进入 AI 上下文，返回更早位置时排除；支持追加、替换和从设定条目查看关联位置。
2. [Progressions on Codex Details](https://www.novelcrafter.com/help/docs/codex/progressions-codex-details)，页面标记更新于 2025-05-06。变化可以针对文本详情，单行、下拉、条目引用类型不适用。

这些文档只作为行为参考；本项目没有场景模型，不能据此宣称本期实现了章内场景定位。

## 本项目的边界分析

- `TODOS.md:14` 起的第 1 项接受章节或场景作为位置；用户已选择章节。
- `backend/app/models/entities.py:229` 的世界观条目含地点、物品、规则等分类；不必为地点、物品新增平行实体体系。
- `backend/app/models/entities.py:241` 的角色拥有固定文本、存活状态；最小自定义文本字段需新建基础能力，但不等同于第 4 项完整字段系统。
- `backend/app/services/context_builder.py:430`、`:479` 使用当前表中的设定，必须在格式化及裁剪之前替换为目标位置的有效结果。
- `backend/app/services/generation_service.py:279`、`:304` 为整章生成和按建议重写的共用入口；其他模式覆盖见 `integration-and-persistence.md`。

## 建议的时间语义（供最终设计审核）

一章内没有精确场景位置，因此显式区分“从本章起”与“本章结束后”。手工变化由作者选择；分析从已完成正文提取的结果属于章末。章节写作上下文使用章初快照，避免重写第 N 章时把第 N 章自身分析得到的结局当成已经发生。

例如基础境界为练气，第 20 章末晋升筑基：第 5 章和第 20 章的章初快照为练气，第 20 章章末／第 21 章章初为筑基。若作者明确将变化设置为“从第 20 章起”，第 20 章的写作上下文才使用筑基。

文本按故事顺序逐条应用：基础 `A`，先追加 `B`，再替换 `C`，再追加 `D`，得到 `A` → `A\nB` → `C` → `C\nD`。替换仅影响指定字段，不能清空同一条目的其他字段。

## Caveats / Not Found

- 官方文档未规定本项目的章节分析、撤销、旧 API 更新或存量迁移行为；这些必须依据现有源码制定。
- 不能把当前内容自动视为真实的最初状态；可靠历史与历史缺口需分开表达，详见 `timeline-and-legacy.md`。
- 位置、数据模型、同章顺序及兼容策略最终以经用户审核的 `design.md` 为准；本文件是研究依据，不是实现批准。

## 相关规范

- `.trellis/spec/backend/story-entities.md`
- `.trellis/spec/backend/prompts-styles.md`
- `.trellis/spec/backend/outline.md`
- `.trellis/spec/backend/project-transfer.md`
- `.trellis/spec/guides/cross-layer-thinking-guide.md`
