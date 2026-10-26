# 技术设计：灵感模式与项目结构化字段

## 1. 项目结构化字段（迁移 `0014_inspiration`）

`projects` 新增以下列：

| 列 | 类型 | 约束 |
|---|---|---|
| `synopsis` | Text，默认 "" | ≤ 2000 |
| `theme` | String(200)，默认 "" | — |
| `tags` | JSON list[str]，默认 [] | 最多 8 个，每个 ≤ 10 字；去掉首尾空白、去重、丢弃空串，保持顺序 |
| `selling_points` | Text，默认 "" | ≤ 500 |
| `audience` | String(100)，默认 "" | — |
| `target_words` | Integer，可为空 | 1000 ~ 10,000,000 |

- `ProjectCreate`、`ProjectUpdate`、`ProjectOut` 同步增加这些字段。`ProjectOut` 已有 `total_words`，前端据此和 `target_words` 计算进度。
- `ProjectInfo` 新增 `synopsis`、`theme`、`tags`、`selling_points`、`audience`。`format_project_info` 在原有几行之后依次追加：

  ```
  类型标签：A、B
  主题：…
  简介：…
  核心卖点：…
  目标读者：…
  ```

  每一项都是有内容才输出。所以字段全空时，输出与现在逐字一致。
- `【作品信息】` 由 `project_service.project_info(db, project)` 统一组装，所有调用方自动带上新字段。
- parity：`tests/prompt_legacy.py` 的对照实现不改，`test_prompt_parity.py` 里新字段全部为空，所以照常通过。另外新增 `format_project_info` 的单测，覆盖每个字段非空时的输出。

## 2. 灵感会话

### 2.1 数据（同在迁移 `0014`）

`inspiration_sessions` 表：

- 字段：`id`、`user_id`（CASCADE）、`project_id`（可为空，SET NULL；从已有项目发起时有值）、`title`（列表显示用，取会话中当前的书名或想法前 30 字）、`status`（`active` / `converted`）、`state` JSON、`converted_project_id`（可为空，SET NULL）、时间戳。

`state` 的结构如下（服务端定义 schema，并由 Pydantic 校验）：

```json
{
  "idea": "一句话想法",
  "notes": ["补充想法1", "补充想法2"],             // 每条 ≤ 200 字，最多 20 条
  "steps": {
    "title": {
      "value": "已定的值" | null,                 // tags 步骤为 list[str]
      "status": "pending" | "decided" | "skipped",
      "stale": false,                              // 前面的步骤被修改后为 true
      "source": "ai" | "edited" | "manual" | "project",   // project = 从项目预填
      "rounds": [                                  // 每次生成的选项，最多保留 10 轮
        {"options": ["…"], "feedback": "本轮反馈" | "", "created_at": "…"}
      ]
    },
    "synopsis": {...}, "theme": {...}, "tags": {...}, "selling_points": {...}, "audience": {...}
  },
  "scale": {"target_words": 300000, "volume_count": 3, "chapters_per_volume": 30, "character_count": 8} | null,
  "current": "title"                               // 前端当前所在步骤
}
```

- 步骤顺序在常量 `STEPS` 中定义：`title → synopsis → theme → tags → selling_points → audience`。想法 `idea` 是第 0 步，由用户填写；规模 `scale` 和确认 `confirm` 不调用 AI。
- 每个用户最多保留 50 个会话，超出时按 `updated_at` 删除最旧且状态为 `converted` 的；没有可删的就返回 409，提示用户先删除一些会话。

### 2.2 规则

- **决定一步**（PATCH）：可以设置 `value` 和 `source`，或者 `status=skipped`。
  - 某一步的值发生变化时，排在它后面、状态为 decided 的步骤一律标记 `stale=true`。
  - 重新决定一个 stale 的步骤后，清除它自己的 stale。用户也可以选择"保持不变"，同样清除。
- **补充想法**：追加或删除一条，不触发 stale。
- **规模建议**：纯函数 `suggest_scale(target_words)`，不调用 AI。
  - 每章约 3000 字，章数 = 目标字数 ÷ 3000，最少 10 章。
  - 卷数按每卷约 30 章计算，范围 1–20，每卷章数范围 5–100。
  - 角色数 = 5 + 卷数 × 2，上限 30。
  - 前端在规模这一步调用 `/inspiration/scale-suggest?target_words=` 预填数值，用户可以修改后保存进 `state.scale`。

### 2.3 生成选项（同步 SSE）

- 请求：`POST /inspiration/sessions/{id}/steps/{step}/generate`，请求体 `{feedback?: str ≤ 500, ai_config_id?: int}`。
- 组装变量：
  - 想法、补充想法；
  - 已定内容：本步之前所有 decided 的步骤；从项目发起时，项目的题材和一句话设定也算在内；
  - 本步历史：之前各轮的选项和反馈；
  - 本次反馈；
  - 本步的约束：书名每个 ≤ 30 字；简介每个 150–400 字；主题 ≤ 60 字；类型标签给 6–10 个候选，每个 ≤ 10 字；卖点 ≤ 200 字；读者 ≤ 60 字。
- 模板：每一步一个 key，`inspiration.title`、`inspiration.synopsis`、`inspiration.theme`、`inspiration.tags`、`inspiration.selling_points`、`inspiration.audience`，共 6 个，注册到 prompt-styles 的模板注册表。
  - 锁定变量是输出格式 `{"options": ["…"]}` 和 `json_rule`。
  - 数据块变量（作品信息、本步历史等）由代码拼好后传入。
- 流程：
  1. 流式输出模型原文，事件为 delta。
  2. 结束后执行 `extract_json` 和 `normalize_options(step, raw)`：去重，丢掉空的和超长的；数量要求 4–6 个，类型标签步骤要求 6–10 个。
  3. 解析失败或有效选项少于下限时，在同一个请求里重试。重试前先发一个 `retry` 事件，前端据此清空已显示的原文；最多重试 2 次。
  4. 成功后，在一个事务里把这一轮追加进 `rounds`（超出 10 轮时裁掉最旧的），然后发出 `done {round}`。
  5. 重试 2 次仍失败，发出 `error`，会话不变。
- 客户端断开时，生成器在 finally 里不写库，所以会话里不会留下半截选项。
- 并发：同一会话同一步骤同时只能有一个生成请求，用进程内的 `set[(session_id, step)]` 去重，冲突时返回 409。部署是单进程，所以内存锁就够了。
- 温度：沿用配置的默认值，不按步骤调整。用户可以在提示词模板里写措辞来引导发散程度。
- 不写生成记录：`generation_logs.project_id` 不能为空，而新建立项时会话还没有项目。在 spec 里注明这一点。

### 2.4 转为项目、应用到已有项目

- `POST /inspiration/sessions/{id}/convert`，请求体 `{title_override?, scale}`：
  1. 要求书名已定（或在请求里给出），否则返回 400。
  2. 创建项目：书名、简介、主题、类型标签、卖点、读者取自已定的步骤；`genre` 取第一个类型标签；`premise` 取想法；`target_words` 取自 scale。
  3. 把 `wizard_progress.params = {volume_count, chapters_per_volume, character_count}` 写入项目。
  4. 会话改为 `converted`，记下 `converted_project_id`。

  返回 ProjectOut，前端跳转到 `/projects/{id}/wizard`。
- 向导页：`WizardGenerator` 初始化参数时，优先读取 `project.wizard_progress.params`，没有时才用原来的默认值。
- `GET /inspiration/sessions/{id}/apply-preview?project_id=`：逐个字段返回 `{field, current, proposed, default_checked}`。项目中该字段为空时，`default_checked` 为 true。
- `POST /inspiration/sessions/{id}/apply`，请求体 `{project_id, fields: [...]}`：只写勾选的字段，返回 ProjectOut。会话状态不变，可以反复应用。
- `POST /inspiration/sessions/from-project/{project_id}`：
  - 用项目现有字段预填，非空的步骤状态为 decided，`source=project`；
  - `idea` 取 `premise`；
  - 记下 `project_id`。
  - 同一个项目已有 active 会话时，直接返回那个会话，不新建。

### 2.5 API 汇总（`/api/v1/inspiration`，按 user_id 隔离，他人的会话返回 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/sessions` | 列表：标题、状态、进度、项目、更新时间 |
| POST | `/sessions` | 新建会话，请求体 `{idea}` |
| GET | `/sessions/{id}` | 会话详情 |
| DELETE | `/sessions/{id}` | 删除会话 |
| PATCH | `/sessions/{id}` | 修改 `idea`、`current` 或 `scale`；修改 idea 时，所有已定步骤标为 stale |
| PATCH | `/sessions/{id}/steps/{step}` | 决定、跳过、保持不变（keep）或清除某一步 |
| POST | `/sessions/{id}/notes` | 添加一条补充想法 |
| DELETE | `/sessions/{id}/notes/{index}` | 删除一条补充想法 |
| POST | `/sessions/{id}/steps/{step}/generate` | 生成选项（SSE） |
| GET | `/scale-suggest` | 规模建议 |
| POST | `/sessions/{id}/convert` | 转为新项目 |
| GET | `/sessions/{id}/apply-preview` | 应用到已有项目前的逐字段对比 |
| POST | `/sessions/{id}/apply` | 应用到已有项目 |
| POST | `/sessions/from-project/{project_id}` | 从已有项目发起会话 |

## 3. 前端

- 新页面 `/inspiration`（列表）和 `/inspiration/:id`（会话）。
  - 入口：项目列表页"新建项目"按钮旁边的"灵感模式"；项目概览页的"用灵感模式补全"。
  - 会话页布局：
    - 左侧是步骤条，显示每一步的状态：已定、已跳过、可能需要更新、待定。
    - 中间是当前步骤：
      - 选项卡片，可以点选，也可以编辑后选；类型标签步骤用多选卡片，另有自定义输入框；
      - "换一批"，可以附带反馈；
      - 历史轮次可以折叠查看；
      - "自己填"和"跳过"。
    - 右侧是"立项草稿"：汇总所有已定的值，以及补充想法列表和一个添加框。
  - 规模一步：输入目标总字数后，自动建议卷数、每卷章数和角色数，这几个数字可以修改。
  - 确认一步：显示汇总，提供"创建项目并进入向导"和"应用到已有项目"。后者先选项目，再看逐字段对比、勾选，最后应用。
  - 生成过程流式显示原文。收到 `retry` 事件时清空并提示"输出格式不对，正在重试（第 n 次）"。卸载组件时中止请求。
- 项目概览：新增 6 个字段的表单项；类型标签用 Select 的 tags 模式；显示目标总字数的进度条。
- 新建项目弹窗：除书名、题材、一句话设定外，再加一个可折叠的"更多信息"区域，放新字段。
- 新增 `api/inspiration.ts` 和对应类型。纯函数放在 `utils/inspiration.ts`：步骤状态推导、stale 文案、规模建议（与后端规则一致，只用于即时显示）、应用对比的默认勾选。

## 4. 取舍

- **会话状态整体存在一个 JSON 列**：步骤固定、结构清楚，不需要关系查询；用 Pydantic 校验可以避免脏数据。代价是会话之间无法跨会话检索选项，这个需求本来也没有。
- **同步 SSE，在请求内重试**：每步只要几秒，交互即时。断开时不落库，状态不会出现半截。
- **规模建议用纯函数，不用 AI**：结果确定、可测试，也不消耗额度。
- **会话生成不写生成记录**：生成记录要求有项目，会话可能还没有项目。以后如果需要用量统计，再单独加。

## 5. 兼容与回滚

- 迁移只新增可空列或带默认值的列，以及一张新表；downgrade 时删除它们。
- 字段为空时提示词不变，由 parity 测试守护。
- 旧客户端不发送新字段，后端按默认值处理。
