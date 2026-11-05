# 调研：规划信息与 AI 写作上下文

- 问题：矩阵的关联维护是否已有 AI 行为，以及新增 POV／支线的接入影响。
- 范围：本地上下文组装与现有规范。
- 日期：2026-11-05。

## 已有机制

- `.trellis/spec/backend/story-entities.md:160`：涉及角色集合含主角、`chapter_characters` 手动指定与自动匹配；手动关联并非纯展示信息。
- `backend/app/services/context_builder.py:658`：`build_cast_section` 通过 `cast_service.select_cast` 选角，然后解析目标章节的有效设定。
- `backend/app/services/context_builder.py:683`：正文上下文组装包括作品信息、有效世界观、涉及角色、当前卷大纲、伏笔提醒、前情与检索内容，当前调用没有规划 POV／支线段。
- `backend/app/services/context_builder.py:706`：局部改写的 `build_setting_context` 同样复用章节选角与章初有效设定。
- `backend/app/services/context_builder.py:858`：本章任务优先取细纲的梗概、事件、目标与情感；没有细纲才退回大纲。新增支线不属于已有字段。
- `.trellis/spec/backend/outline.md:85`：细纲建章会把 `character_ids` 写入 `chapter_characters`；绑定已有章时合并角色集合。

## 产品边界与已确认决定

- 若矩阵角色复用现有手动出场列表，应保留其已有 AI 选角行为，不另建同义数据源。
- 新增 POV／支线仅作规划时，不应让界面暗示 AI 必然遵循它们。
- 若接入 AI，需要定义适用操作、目标章节点、历史节点与后续节点的可见边界、作者备注和写作指令的区别，以及与写作风格中的视角描述冲突的优先级。
- 2026-11-05 用户明确选择：“A，同时接入 AI 生成和重写。”本任务新增 POV 与本章支线节点必须进入实际写作提示词，并按目标章节截断后续节点。

## 已确认调用链

- `backend/app/services/generation_service.py:275`：生成通过 `build_chapter_context` → `build_chapter_messages`；按建议整章重写在 `:299` 使用同一上下文入口。
- `backend/app/services/rewrite_service.py:110`：选区改写／续写通过 `build_setting_context` → `build_rewrite_messages`，只有改全章上下文会漏掉此入口。
- `backend/app/services/batch_service.py:464`：批量生成复用 `generation_service.build_chapter_job`；应与单章得到相同的规划上下文。
- `backend/app/services/generation_service.py:468`：自动续写使用另外的提示词构建步骤；最终设计须确认新的规划约束不会在续写轮丢失。

## 实施设计仍需验证

各生成入口与提示词预览的共同契约；无规划信息时原有行为；目标章过滤；局部改写保持选区操作目标，不强制在任意选区完成整章的全部支线节点；POV 与风格视角文字的优先级。
