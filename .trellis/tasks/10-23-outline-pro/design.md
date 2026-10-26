# 设计：大纲进阶

## 1. 边界

- **后端新增**
  - `outline_service`：续写和展开后的采纳、排序。
  - `plan_service`：细纲 CRUD、重新展开时的保留规则、建章。
  - `chapter_numbering.shift_chapter_numbers`：统一的章节号平移。
  - `outline_snapshot_service`：快照的保存、恢复和裁剪。
  - `outline_transfer_service`：导入导出。
  - `api/v1/outline_pro.py`（或拆成多个文件）。
  - 两个新的任务类型：`outline.continue`、`outline.expand`。
- **修改**
  - `wizard_service` / `wizard handler`：outline 步骤支持 append 模式，覆盖前先存快照。
  - `projects`：新增 `wizard_progress`。
  - `context_builder`：本章任务优先使用细纲。
  - `chapter_service.create_from_outlines`：跳过已展开的大纲。

## 2. 数据模型（迁移 `0011_outline_pro`）

> 开工时确认迁移编号，当前最新是 `0010_foreshadows`。

### `chapter_plans`（细纲）

| 字段 | 说明 |
|---|---|
| id, project_id CASCADE, outline_id CASCADE（index） | |
| seq int | 在大纲内的顺序，从 1 开始 |
| title String(200)、summary Text | |
| key_events JSON（`string[]`） | |
| character_ids JSON（`int[]`） | 读取时过滤掉已删除的角色；导出时转成角色名 |
| emotion String(100)、goal Text、est_words int（默认 3000） | |
| chapter_id FK chapters SET NULL，可空，**unique** | 已建章时有值 |
| created_at / updated_at | |

### `chapters.plan_id`

FK chapter_plans SET NULL，可空。与 `chapter_plans.chapter_id` 双向冗余，方便双向查询，由 `plan_service` 维护两边一致。

**一条大纲"已展开"的定义**：该大纲下至少有一条细纲。已展开的大纲建章时按细纲建，`create_from_outlines` 会跳过它们；未展开的大纲仍按"1 纲 1 章"处理。

### `outline_snapshots`

`id, project_id CASCADE, reason String(32), note String(200), data JSON, created_at`。`reason` 取值：wizard_replace / import_replace / reexpand / manual / restore。

`data` 的结构：

```json
{
  "outlines": [{原始 id + 全部字段}],
  "plans": [{原始 id + 全部字段，不含 chapter_id}],
  "links": [{"chapter_id", "outline_id", "plan_id"}]
}
```

每个项目保留最近 20 条，保存时裁剪。

### `projects.wizard_progress`

JSON，默认 `{}`。结构为 `{"done": ["world", "careers"], "skipped": ["careers"], "current": "characters"}`。

## 3. 章节号平移（R3，核心约束）

```python
async def shift_chapter_numbers(db, project_id, *, from_no: int, delta: int) -> None  # 不 commit
```

- 把项目内所有 `>= from_no` 的章节号整体加上 `delta`。涉及的字段：
  - `chapters.chapter_no`
  - `outlines.chapter_no`
  - `chapter_analyses.chapter_no`
  - `analysis_changes.chapter_no`
  - `relationship_history.chapter_no`
  - `career_history.chapter_no`
  - `foreshadow_history.chapter_no`
  - `foreshadows` 的 `planned_plant_chapter`、`actual_plant_chapter`、`planned_resolve_chapter`、`actual_resolve_chapter`
- 实现方式：每张表一条 `UPDATE ... SET x = x + :delta WHERE project 归属 AND x >= :from_no`。没有 `project_id` 列的表通过子查询限定，例如 relationship_history 经 relationships.project_id 限定。
- 字段清单定义为模块常量 `CHAPTER_NO_REFS`，并写进 spec：**以后新增引用章节号的字段，必须登记到这里**。另配一个测试：通过反射扫描所有模型中名为 `chapter_no` 或以 `_chapter` 结尾的 Integer 列，断言它们都已登记在 `CHAPTER_NO_REFS` 里，防止遗漏。
- `chapters.chapter_no` 没有唯一约束（已核实），所以平移的顺序不受约束冲突影响。
- 文本里出现的章节号不做改写，例如 reason 中的"已有第 N 章的更新"。这类文本属于历史描述，在 spec 中注明。

## 4. 细纲的建章与重新展开（R2/R3）

**建章** `POST /outlines/{oid}/plans/build`：

1. 取出该大纲下还没建章的细纲，按 seq 排序。
2. 确定插入位置：
   - 如果该大纲原来以"1 纲 1 章"的方式关联了章节 C（C.plan_id 为空）：把 C 绑定到 seq=1 的细纲（C 保留正文和章节号），其余细纲插在 C 之后。
   - 否则以本大纲 `chapter_no` 为基准，插在"已建章的细纲"中最后一章的后面；如果都还没建章，就插在大纲章节号对应的位置。
3. 先执行 `shift_chapter_numbers(from_no=插入点, delta=新建章数)`，再创建章节：设置 outline_id、plan_id、title、空正文、draft，并把细纲的 character_ids 写进 `chapter_characters`。
4. 大纲自身的 `chapter_no` 保持为第一章的章节号，后续大纲的章节号已在平移中一并更新。

**重新展开的采纳** `PUT /outlines/{oid}/plans {plans: [...]}`（整体同步）：

- 采纳前先存快照（reason=reexpand，仅当该大纲原本已有细纲时）。
- 已建章且章节有正文的细纲：**保留**，提交的数据只能修改其 title、summary 等描述字段，不能删除它。如果请求中缺少它，返回 409「已有正文的细纲不能删除」。
- 已建章但章节没有正文的细纲：允许删除。删除细纲时，对应的空章节一并删除，并对其后的章节号执行 `delta=-1` 平移。
- 未建章的细纲：按 id 更新，没有 id 的新建，缺失的删除。
- 最后把 seq 重新编号为 1..N。

**前端预览**：`outline.expand` 的结果附带当前已有细纲及其状态（has_content），预览中把有正文的细纲标为"保留"、置灰，不可删除。

## 5. AI 任务

| kind | resource_key | 输入 | `done.result` |
|---|---|---|---|
| `outline.continue` | `outline:{pid}` | `count(1–20)`、`direction`、`new_volume?: {title}`、`ai_config_id?` | `{drafts: [{volume_no, volume_title, chapter_no, title, summary}], warnings}` |
| `outline.expand` | `expand:{oid}` | `outline_id`、`count(2–10)`、`instruction`、`ai_config_id?` | `{outline_id, drafts: [{title, summary, key_events, characters: [{name, character_id, resolved}], emotion, goal, est_words}], existing: [{id, seq, title, chapter_id, has_content}], warnings}` |

- 两个任务都只产出草稿，不写库，也不写生成记录；在 TaskPanel 中注册结果查看器。
- **续写的上下文**：
  - 项目信息和世界观（截断）；
  - 最近 10 条章纲；
  - 对应章节最近一次分析的摘要，没有则用大纲梗概；
  - 未回收的伏笔：`foreshadow_service.reminders` 的长线和超期两档，再加上所有已埋、未回收的伏笔标题，最多 20 条；
  - 写作方向。
  - 提示词新建在 `ai/prompts/outline_pro.py`。
- **展开的上下文**：
  - 当前大纲；
  - 前后各 2 条章纲；
  - 角色名单（名字和别名）；
  - 已保留细纲的梗概，要求新细纲不得重复其中的关键事件。
  - 超过 5 章时分批生成，每批附上已生成细纲的关键事件。
- **采纳接口**：
  - `POST /outlines/append {items: [...]}`：章节号从当前最大值加 1 开始顺延，单事务。
  - `PUT /outlines/{oid}/plans`：见 §4。

## 6. 快照与恢复（R6）

- `save_snapshot(db, project_id, reason, note)`：序列化当前的大纲、细纲和章节关联，然后裁剪到 20 条。
- **恢复** `POST /outline-snapshots/{sid}/restore`：
  1. 先把当前状态存为快照（reason=restore）。
  2. 删除项目内全部大纲。细纲随之级联删除，chapters.outline_id、plan_id 被置空。
  3. 按快照中的**原始 id** 重新插入大纲和细纲。PostgreSQL 需要在插入后用 `setval` 校正序列，SQLite 不需要。
  4. 按 links 恢复关联：只恢复章节仍然存在、且快照中对应大纲或细纲也存在的关联，同时回填 `chapter_plans.chapter_id`。
  5. 恢复不修改章节号。快照保存后如果发生过章节号平移，快照中大纲的 `chapter_no` 可能与章节不一致，这种情况在预览中提示；恢复后大纲的 chapter_no 以快照为准。
  - 整个过程在一个事务里完成。
- **列表** `GET /outline-snapshots`：返回 id、reason、note、created_at，以及大纲数和细纲数。
- **手动存快照** `POST /outline-snapshots {note}`。

## 7. 导入导出（R5）

- **格式**：

  ```json
  {
    "format": "ainovel.outline",
    "version": 1,
    "volumes": [{"volume_no", "volume_title", "outlines": [{"chapter_no", "title", "summary",
      "plans": [{"title", "summary", "key_events", "characters": [名字], "emotion", "goal", "est_words"}]}]}]
  }
  ```

  大小上限 2MB，作为 JSON body 提交。
- **预览** `POST /outlines/import/preview {data}`：返回 `{volumes, outlines, plans, unresolved_characters: [...], warnings}`。
- **导入** `POST /outlines/import {data, mode: append|replace}`：
  - append：卷号和章节号整体顺延到当前末尾之后。
  - replace：先存快照（import_replace），删除全部大纲，章节自动解除关联，再写入。
  - 角色按名字或别名匹配，匹配不上的忽略并写入 warnings。
- **导出** `GET /outlines/export`。

## 8. 向导（R7）

- `WizardRequest` 新增 `outline_mode: "replace" | "append"`，默认 replace，与现状一致。
  - append：不删除旧大纲。提示词附上已有大纲的末尾 10 条，要求从下一卷、下一章接着写；写入时章节号顺延。
  - replace：写入前存快照（wizard_replace）。原有逻辑是 `delete(Outline)` 再插入（`wizard_service.save_wizard_result`），章节通过外键 SET NULL 自动解除关联，不会被删除（已核实）。
- `PATCH /projects/{pid}/wizard-progress {done?, skipped?, current?}`：向导页每完成或跳过一步时调用；页面加载时读取并定位到对应步骤。`ProjectOut` 增加 `wizard_progress` 字段。

## 9. 上下文接入细纲（R4）

`context_builder` 的"本章任务"按以下优先级取：
1. `chapter.plan_id` 对应的细纲：标题、梗概、关键事件、叙事目标、情感基调；
2. 大纲梗概，与现在的行为一致。

生成请求未指定 `target_words` 时，默认值取细纲的 `est_words`：前端在打开生成面板时预填，后端不改变请求语义。选角的手动出场角色已在建章时写入 `chapter_characters`，无需额外处理。

## 10. 前端

- **`OutlinesPanel` 重做**：
  - 按卷分组的大纲列表，每行显示：章节号、标题、梗概、展开状态（未展开 / N 章细纲·M 章已建）、操作（编辑、展开、建章、上移下移、删除）。
  - 已展开的大纲可以折叠，显示细纲子表，细纲支持行内编辑。
  - 工具栏：AI 续写、导入、导出、快照（Drawer 列表，可恢复、可手动保存）。
- **续写和展开的预览组件**：可编辑的表格或卡片，支持勾选和删除；注册到 `taskResultViewers`（`outline.continue`、`outline.expand`）。
- **导入弹窗**：读取文件后先预览，再选择追加或替换。
- **向导页**：大纲步骤增加"追加 / 覆盖"切换；读写 `wizard_progress`，进入页面时定位到对应步骤。
- **章节编辑器**：有细纲时，在生成面板上方展示细纲要点，目标字数预填 `est_words`。
- **类型与标签**：TaskKind 和标签补充 `outline.continue`、`outline.expand`。

## 11. 风险与回滚

- **章节号平移是全局性的改动**，测试要覆盖：每个被登记的字段都随之平移、未登记字段的反射检测、平移边界（`>= from_no`）、负向平移（删除空章节时）。
- **快照恢复时插入原始 id**，PostgreSQL 序列需要校正：迁移测试在 SQLite 上运行，PostgreSQL 的分支要写单测或标注 TODO 并在 spec 中注明。
- **回滚**：回退代码后执行 `alembic downgrade -1`。
