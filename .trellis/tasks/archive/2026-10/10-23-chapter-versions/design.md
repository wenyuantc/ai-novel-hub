# 设计：章节版本与局部改写

## 1. 边界

- 后端新增 `version_service`（版本写入、合并、裁剪）和 `rewrite_service`（局部改写的 prepare/run），并改造 `generation_service`（写版本、自动续写、生成记录）和 `chapter_service.update_chapter`（手动保存写版本）。
- 路由层不碰 ORM，沿用 `api/v1 → services → models` 分层。版本相关路由放在 `api/v1/chapters.py`；如果文件超过约 300 行，就拆出 `api/v1/chapter_versions.py`。
- 前端新增版本抽屉、差异对比组件、局部改写面板、生成记录列表，都挂在 `ChapterEditorPage` 上，不新增路由。

## 2. 数据模型（迁移 `0005_chapter_versions`）

> 迁移编号：封面任务 `10-23-project-cover` 也可能占用 `0005`。谁先合入谁用 `0005`，后合入的一方 rebase 后改为 `0006`，同时修改 `down_revision`。

### `chapter_versions`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| chapter_id | FK chapters CASCADE，index | |
| seq | int | 章节内递增序号，界面显示为 `v{seq}`；取 `max(seq)+1`，被删除的序号不复用 |
| source | String(16) | `manual`（手动保存）、`baseline`（AI 或回滚替换前补的快照）、`generate`、`polish`、`rewrite`（局部改写采纳）、`restore` |
| content | Text | 该版本的完整正文 |
| word_count | int | `count_words(content)` |
| starred | bool，默认 false | |
| note | String(200)，默认 "" | 作者备注 |
| generation_log_id | FK generation_logs SET NULL，可空 | 由哪次 AI 调用产生 |
| created_at / updated_at | TimestampMixin | 合并窗口按 `created_at` 计时（固定窗口）；合并时 `updated_at` 会刷新，但不参与判断 |

唯一约束 `(chapter_id, seq)`。

**语义**：版本是"某个时刻正文的快照"，也就是事件发生后的状态。当前正文等于最新版本，除非之后还有未产生版本的改动。为了满足 D1"覆盖前旧稿必须留存"，任何 AI 覆盖或回滚之前都先执行 `ensure_baseline`：如果当前正文非空，并且和最新版本的内容不同，就先写一个 `baseline` 版本。

### `generation_logs`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| project_id | FK projects CASCADE，index | 用于归属校验 |
| chapter_id | FK chapters SET NULL，可空，index | |
| kind | String(16) | `generate`、`polish`、`rewrite`、`expand`、`condense`、`deslop`、`continue` |
| ai_config_id | FK ai_configs SET NULL，可空 | |
| provider / model | String | 冗余存储，配置删除后仍可查看 |
| params | JSON | `target_words`、`instruction`、`auto_continue`、`continuation_rounds`、选区长度等 |
| messages | JSON | 发送给模型的完整消息，最后一轮续写不含已生成正文；长度上限约为上下文预算 |
| status | String(16) | `success`、`error`、`cancelled` |
| error | Text，默认 "" | 面向用户的中文错误 |
| output_chars | int | 本次产出的字数（`count_words`） |
| duration_ms | int | |
| prompt_tokens / completion_tokens | int，可空 | 现有 `LLMProvider.generate_stream` 只返回文本，本任务一律写 NULL，留给后续 provider 改造 |
| created_at | | |

每个项目保留最近 200 条日志，写入时顺带删除更早的记录。

## 3. 版本写入规则（`version_service`）

```python
async def record_manual(db, chapter) -> ChapterVersion | None
async def ensure_baseline(db, chapter) -> None
async def record(db, chapter, source, *, log_id=None) -> ChapterVersion
async def prune(db, chapter_id) -> None   # 保留最新 50 个未标星版本 + 全部标星版本；最新版本永不删除
```

这些函数都不自己 commit，由调用方统一提交，保证"改正文 + 写版本 + 写日志"在同一个事务里。

- **手动保存**（`update_chapter`，仅当 `content` 字段实际变化时）：
  1. 新正文和最新版本内容相同，跳过。
  2. 最新版本是 `manual`、未标星，且距它的 `created_at` 不到 10 分钟（固定窗口，合并不延长窗口）：原地更新它的 `content`、`word_count`。
  3. 其他情况：新建 `manual` 版本，然后执行 `prune`。
  4. 新正文为空时不写版本，避免清空操作产生空版本。
- **创建章节**时如果带非空正文，写一个 `manual` 版本。
- **AI 整章生成或润色成功**（`run_chapter_job` 的落库事务）：先 `ensure_baseline`，再改正文，然后 `record(source=generate|polish, log_id)`，最后 `prune`。
- **局部改写采纳**：先 `ensure_baseline`，再替换选区，然后 `record(source=rewrite, log_id)`。
- **回滚**：先 `ensure_baseline`，把正文改为目标版本内容，然后 `record(source=restore)`，`note` 自动填为 `恢复自 v{seq}`。
- 10 分钟窗口和上限 50 写成 `version_service` 里的模块常量 `MANUAL_MERGE_WINDOW`、`MAX_VERSIONS`，不做成配置项。
- 合并判断用服务端时间，因为前后端时钟可能不一致。

## 4. 整章生成与自动续写（D3）

`GenerateChapterRequest` 新增 `auto_continue: bool = False`。接口默认关闭以兼容旧客户端，编辑器开关默认开启并显式传 `true`（实现时调整）。润色不做续写。

`run_chapter_job` 的 generate 流程：

```
text = stream(messages)
rounds = 0
while auto_continue and count_words(text) < 0.85 * target and rounds < 2:
    rounds += 1
    yield "continue", {"round": rounds, "words": count_words(text), "target": target}
    msgs = messages + [assistant(text), user(续写指令(剩余字数 = target - words))]
    piece = stream(msgs)
    if not piece.strip(): break
    text = join(text, piece)   # 用 "\n\n" 拼接；提示词要求续写从新段落开始，并且不重复前文
落库：ensure_baseline → content=text → version(generate) → log(success, continuation_rounds=rounds)
```

- 新增 SSE 事件 `continue`，必须同步更新 `.trellis/spec/backend/error-handling.md` 里的 SSE 契约表和前端 `sse.ts` 的事件处理。旧客户端会忽略未知事件，不影响兼容。
- 续写轮次本身报错时，保留已生成的正文照常落库，`done` 的 payload 增加 `"warning": "自动续写失败：…"`；客户端断开时，沿用现有规则不落库并回退状态。
- 对于极端长的目标，续写消息里的 `assistant(text)` 会让上下文变长；Anthropic、Gemini 都支持 assistant 轮，`split_system` 不受影响。
- 续写提示词放在 `ai/prompts/chapter.py` 的 `build_continuation_messages`。

## 5. 局部改写（D1：先预览再确认）

### 5.1 预览：`POST /api/v1/chapters/{cid}/rewrite`（SSE）

请求体 `RewriteRequest`：

```
ai_config_id?: int
op: "rewrite" | "expand" | "condense" | "deslop" | "continue"
start: int, end: int        # 码点偏移，范围 [0, len(content)]；op=continue 时 start == end，表示光标位置
instruction: str ≤ 2000
```

- prepare 阶段：校验归属；`status=generating` 时返回 409；偏移越界时返回 400；非续写的 op 要求选区非空，且选区不超过 5000 字（400）；续写要求光标前的正文非空。
- 预览阶段**不抢占 `generating` 锁，也不改正文**，只把结果流式返回给前端，并写一条 `generation_logs`。
- 上下文组成：项目写作风格、本章梗概（如有）、选区前约 1500 字和选区后约 500 字（续写时只取光标前约 3000 字）、选区原文、按 op 给出的要求。提示词放在新文件 `ai/prompts/rewrite.py`。
- `done` 事件：`{"text": <结果>, "start", "end", "original": <选区原文>, "log_id"}`。

### 5.2 采纳：`POST /api/v1/projects/{pid}/chapters/{cid}/rewrite/apply`

请求体：`{start, end, original, replacement, log_id?}`

- 如果 `content[start:end] != original`，返回 409 `正文已变化，请重新选择后再改写`。这是乐观并发校验。
- `status=generating` 时返回 409。
- 事务内依次执行：`ensure_baseline` → 拼接新正文 → `record(rewrite, log_id)` → `prune` → 更新 `word_count`，最后返回 `ChapterOut`。
- `log_id` 必须属于本章，否则忽略，不报错。

### 5.3 偏移换算

前端 `TextArea` 的 `selectionStart`/`selectionEnd` 是 UTF-16 偏移，Python 字符串按码点计。前端发请求前用 `Array.from(text.slice(0, i)).length` 换算成码点偏移，收到响应后再反向换算。换算函数放在 `utils/textOffset.ts`，并配单测，覆盖 emoji 和 CJK 扩展 B 区的生僻字。

## 6. 版本与日志 API

全部挂在 `/api/v1/projects/{pid}/chapters/{cid}` 下，先经过 `get_project_chapter` 做归属校验，非本人一律 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/versions` | `ChapterVersionBrief[]`，按 seq 倒序，不含正文 |
| GET | `/versions/{vid}` | `ChapterVersionOut`，含正文 |
| PATCH | `/versions/{vid}` | `{starred?, note?}` |
| DELETE | `/versions/{vid}` | 删除单个版本；删除最新版本时返回 400 |
| POST | `/versions/{vid}/restore` | 回滚；生成中返回 409；返回 `ChapterOut` |
| GET | `/generation-logs` | 本章日志简表，倒序，不含 messages |
| GET | `/generation-logs/{lid}` | 详情，含 messages、params |

差异对比在前端计算：先取两个版本的全文，再在前端做 diff，后端不提供 diff 接口。

## 7. 前端

- **依赖**：新增 `diff`（jsdiff，BSD-3），用 `diffChars` 做字符级对比；中文没有词边界，逐字对比最直观。新增后用 `vite build` 检查包体积，仍需低于 `chunkSizeWarningLimit`（1500 kB）；如果超出，把对比组件改成 `React.lazy` 懒加载。
- **`api/chapterVersions.ts`**：版本与日志的 CRUD；`rewriteChapter()` 走 `runGeneration`，`applyRewrite()` 走普通 POST。`types.ts` 镜像新 schema。
- **`components/TextDiff.tsx`**：输入 `a`、`b`，输出行内高亮（删除标红并加删除线，新增标绿），附带增删字数统计。
- **版本抽屉**（编辑器工具栏新增「版本历史」按钮）：
  - 列表显示 `v{seq}`、来源标签、相对时间、字数、星标、备注。
  - 每个版本可操作：查看、与当前对比、恢复、删除；删除用 `Popconfirm`，恢复用 `modal.confirm`。
  - 可以勾选两个版本对比。
  - 有未保存修改时，恢复前先自动保存，与 AI 操作的规则一致。
- **局部改写 Tab**（AI 助手新增第三个 Tab「局部改写」）：
  - 文本域有选区时，显示选区摘要和字数；没有选区时，只能选「续写」，在光标处续写。
  - 可选操作：改写、扩写、缩写、去 AI 味、续写；可填补充要求。运行前如有未保存修改，先自动保存。
  - 流式输出区实时显示结果。完成后显示 `TextDiff`（原文对比结果），提供三个按钮：「采纳」（调用 apply 后执行 `applyServerChapter`）、「重新生成」、「放弃」。
  - 从开始预览到采纳或放弃之前，正文文本域保持只读，避免选区失效。
- **整章生成 Tab**：新增「字数不足时自动续写」开关，默认开启，用 `localStorage` 记住选择；收到 `continue` 事件时，进度提示显示「字数不足，自动续写第 n 轮」。原来的「重新生成正文？」确认文案改为提示「当前正文会自动存为历史版本」。
- **生成记录**：在版本抽屉里加第二个 Tab「生成记录」，列表显示类型、模型、状态、字数、耗时、时间；点开一条可以看参数和完整提示词（只读）。
- **`useStreamTask`、`sse.ts`**：支持可选的 `onEvent(name, data)` 回调，用来接收 `continue` 事件；`delta`、`done`、`error` 的原有语义不变。

## 8. 兼容与迁移

- 迁移只新建表，不改现有表，存量章节没有版本也不影响使用。首次 AI 覆盖或手动保存时会自然产生 `baseline` 或 `manual` 版本，不做回填。
- 降级时 `drop_table` 两张表。
- 现有 SSE 测试的事件顺序不变（没有触发续写时不会出现 `continue` 事件）。

## 9. 取舍记录

- **全文快照，不存增量 diff**：实现简单，回滚是 O(1)，按 50 个上限估算单章最多约 300KB，可以接受。
- **局部改写预览期间不加锁**：预览期间作者可以放弃，锁会让放弃后状态复位变复杂；采纳时由乐观校验兜底。
- **token 统计暂缓**：需要改造三个 provider 的流式接口，收益低于成本，先预留字段。
- **diff 在前端做**：避免传输两份全文之外的冗余数据，交互也更顺畅。
