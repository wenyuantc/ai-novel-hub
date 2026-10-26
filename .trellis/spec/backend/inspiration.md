# 灵感模式与立项结构化字段

> 来源任务：`10-23-inspiration`（迁移 `0014`）。模板注册与锁定变量见 [prompts-styles.md](./prompts-styles.md)，
> 同步 SSE 帧格式见 [error-handling.md](./error-handling.md)，`wizard_progress` 见 [outline.md](./outline.md)。

---

## 场景：项目结构化字段

### 1. 范围 / 触发条件

`projects.synopsis / theme / tags / selling_points / audience / target_words`、`ProjectCreate / ProjectUpdate / ProjectOut`、
`ProjectInfo` + `format_project_info`、`project_service.project_info`。

### 2. 契约

| 字段 | 列 | 校验（schema，422） |
|---|---|---|
| `synopsis` 简介 | Text | ≤ 2000 |
| `theme` 主题 | String(200) | ≤ 200 |
| `tags` 类型标签 | JSON list | `normalize_tags`：去首尾空白、丢空串、去重（保序）后 ≤ 8 个、每个 ≤ 10 字 |
| `selling_points` 核心卖点 | Text | ≤ 500 |
| `audience` 目标读者 | String(100) | ≤ 100 |
| `target_words` 目标总字数 | Integer 可空 | 1000–10,000,000；PATCH 显式 null = 清空 |

- 字符串写入时 strip；`genre` 仍是主题材，没有叙事视角字段（归写作风格）。
- `ProjectInfo` 新增 `synopsis / theme / tags(tuple) / selling_points / audience`，由 `project_service.project_info` 统一组装；
  `format_project_info` 在原有行之后依次追加 `类型标签：A、B` / `主题：` / `简介：` / `核心卖点：` / `目标读者：`，**非空才输出**，
  所以字段全空时所有提示词与改造前逐字一致（parity 测试守护）。`target_words` 不进提示词。

### 3. 必需的测试

`tests/test_inspiration.py::test_format_project_info_each_field`（每个字段单独非空、空白不输出、全部非空的顺序）、
`test_project_structured_fields_validation`（去重去空白、各项超限 422、null 清空、新字段进入向导提示词）。

---

## 场景：灵感会话（`inspiration_service` / `/api/v1/inspiration`）

### 1. 范围 / 触发条件

`app/models/entities.py::InspirationSession`、`app/schemas/inspiration.py`、`app/services/inspiration_service.py`、
`app/api/v1/inspiration.py`、`app/ai/prompts/inspiration.py`、`templates/inspiration.*.j2`。

### 2. 签名

```python
# schemas/inspiration.py
STEPS = ("title", "synopsis", "theme", "tags", "selling_points", "audience")   # 顺序即 stale 传播顺序
MAX_SESSIONS = 50; MAX_ROUNDS = 10; MAX_NOTES = 20; NOTE_MAX = 200; FEEDBACK_MAX = 500
class SessionState: idea, notes[], genre, steps{step: StepState}, scale: Scale | None, current
class StepState: value: str | list[str] | None, status: pending|decided|skipped, stale, source: ai|edited|manual|project|None, rounds[]
class Round: options[], feedback, created_at(ISO)
class Scale: target_words?, volume_count 1–20, chapters_per_volume 1–100, character_count 3–30
# services/inspiration_service.py
def apply_step(state, step, StepUpdate) -> None          # decide / skip / keep / clear + stale 传播
def suggest_scale(target_words) -> Scale
def normalize_options(step, raw) -> list[str]            # 结构不对 → JSONExtractError
def build_decided(state, step) -> str; def build_history(state, step) -> str
async def prepare_generate(db, *, user_id, session_id, step, req, cipher, factory, ai_timeout, allow_private) -> InspirationJob
async def run_generate(job, sessionmaker) -> AsyncIterator[Event]
async def convert / apply_preview / apply / from_project
```

### 3. 契约

**state 结构**（JSON 列，读写都经 `SessionState` 校验；`steps` 缺的 key 自动补为 pending；库里的 state 不合法（手工改库 / 限制收紧后的旧数据）→ 409 `灵感会话数据已损坏…`，列表按空 state 展示、删除不读 state，生成中途保存时同样以 `error` 事件给出该文案）

```json
{"idea": "…", "notes": ["…"], "genre": "",
 "steps": {"title": {"value": "…", "status": "decided", "stale": false, "source": "ai",
                     "rounds": [{"options": ["…"], "feedback": "", "created_at": "…"}]}, "...": {}},
 "scale": {"target_words": 300000, "volume_count": 4, "chapters_per_volume": 25, "character_count": 13},
 "current": "idea|title|…|audience|scale|confirm"}
```

- `title` 列 = 已定书名，否则想法前 30 字；`genre` 只在从项目发起时有值（项目题材，参与「已定内容」）。
- 每用户最多 50 个会话：新建 / 从项目发起时满了先删 `updated_at` 最旧的 `converted` 会话，没有可删的 → 409 `灵感会话已达上限（50 个），请先删除一些会话`。

**步骤与 stale**（`PATCH /sessions/{id}/steps/{step} {action, value?, source?}`）

| action | 结果 |
|---|---|
| `decide` | 值校验（文本 strip 后非空、≤ 列长度；tags 走 `normalize_tags` 且至少 1 个）→ decided，清除自己的 stale |
| `skip` | skipped，值清空 |
| `clear` | 回到 pending |
| `keep` | 「保持不变」：只清除 stale |

- 某一步**原本是 decided** 且有效值（decided 的 value，否则 None）发生变化（改值 / 跳过 / 清除）→ 排在它后面、状态为 decided 的步骤
  `stale = true`，**不清空**。pending / skipped → decided 不传播（补全场景下填空不应把其他已定字段标脏）。
- `idea` 上限 `IDEA_MAX = 5000`，与项目 `premise` 一致：从项目发起时原样带入，应用回项目不截断。
- `PATCH /sessions/{id} {idea}` 且 idea 变化 → 所有 decided 步骤 stale；`current` / `scale`（显式 null 清空）同一接口修改。
- 补充想法 `POST /notes {text ≤200}`（最多 20 条，超出 400）/ `DELETE /notes/{index}`（越界 404），不触发 stale。

**生成选项（同步 SSE）** `POST /sessions/{id}/steps/{step}/generate {feedback?, ai_config_id?}`

- 变量：`idea`、`notes`（`- ` 列表）、`decided`（项目题材 + **除本步外**所有 decided 步骤，按步骤顺序）、`history`（本步各轮选项与
  该轮反馈：`第 2 轮（作者反馈：…）：\n1. …`）、`feedback`（本次）、锁定 `json_rule` / `output_format = {"options": [...]}`。
- 事件：`delta {text}` … → 不合格时 `retry {attempt, max: 2, reason}`（前端清空已显示原文）→ `delta` … → `done {round, session}` /
  `error {detail}`。不合格 = 空输出 / `extract_json` 失败 / 结构不是 `{"options": [...]}` 或数组 / 有效选项少于下限（tags 6，其余 4）。
  最多重试 2 次（共 3 次调用），仍失败 → `error "AI 输出不合法：…，已自动重试 2 次，请稍后重试"`，会话不变。
  `AIProviderError` 不重试，直接 `error`。
- `normalize_options`：字符串 / 数字 / 带 `text|value|content|title|name` 的对象 → strip、书名去《》、去重、丢弃空的与超长的
  （书名 30、简介 500（提示词要求 150–400，留余量）、主题 60、标签 10、卖点 200、读者 60），多于上限截断（tags 10，其余 6）。
- **只在成功时写库**：用独立 session 重新读取会话、追加一轮（只保留最近 10 轮）后 commit，再发 `done`；生成期间会话被删除 →
  `error "灵感会话已被删除，选项未保存"`。客户端断开 → `aclosing` 关闭生成器，GeneratorExit 发生在 yield 处，不会写入半截选项。
- 并发：进程内 `_generating[(session_id, step)]`，`prepare_generate` 在**全部校验通过后**占锁（冲突 → 409 `该步骤正在生成选项，请稍候再试`），
  `run_generate` 的 `finally` 释放；条目超过 600 秒视为遗留（响应体从未开始迭代时 finally 不执行）。依赖单进程部署（同任务引擎）。
- **不写生成记录**：`generation_logs.project_id` 非空，而立项时会话可能还没有项目；以后需要用量统计再单独加。
- 生成不改变步骤状态；「AI 只为空着的步骤生成」由前端决定何时调用（后端不拦截已定步骤的「换一批」）。

**规模建议** `GET /scale-suggest?target_words=`（1000–10,000,000）：章数 = max(10, round(字数 / 3000))；卷数 = ceil(章数 / 30) 夹到 1–20；
每卷章数 = ceil(章数 / 卷数) 夹到 5–100；角色数 = min(30, 5 + 卷数 × 2)。纯函数，不调用 AI。

**转为项目** `POST /sessions/{id}/convert {title_override?, scale?}` → 201 `ProjectOut`

- 书名 = `title_override` 或已定书名，都没有 → 400 `请先定下书名`；已转换且项目仍在 → 409（项目被删后可再次转换）。
- 字段取 decided 的步骤；`genre` = 第一个类型标签（没有则会话 `genre`）；`premise` = 想法；规模 = 请求 `scale`（同时写回会话）或会话 `scale`；
  有规模时 `wizard_progress.params = {volume_count, chapters_per_volume, character_count}`、`target_words = scale.target_words`。
- 会话 `status = converted`、`converted_project_id` = 新项目。

**应用到已有项目**：`GET /sessions/{id}/apply-preview?project_id=` → `[{field, label, current, proposed, default_checked}]`
（只列会话里有值的字段：6 个步骤 + `premise`（想法）+ `target_words`（规模）；`default_checked` = 项目该字段为空）；
`POST /sessions/{id}/apply {project_id, fields}` 只写勾选的字段，勾选了没有值的字段 → 400；会话状态不变，可反复应用。

**从项目发起** `POST /sessions/from-project/{project_id}`：同一项目已有 active 会话 → 直接返回它；否则用项目非空字段预填为
`decided / source=project`，`idea = premise`、`genre = 项目题材`，有 `target_words` 时预填 `suggest_scale`，`current` = 第一个 pending 步骤（都定了为 `scale`）。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 会话不存在或属于他人（所有 `/sessions/{id}...`） | 404 `灵感会话不存在` |
| 项目不存在或属于他人（from-project / apply-preview / apply） | 404 `项目不存在` |
| 未知步骤 / feedback > 500 / scale 越界 / note > 200 | 422 |
| 定下的值为空、超长、类型标签超限 | 400 |
| 没有 AI 配置 / Base URL 被拦截 | 400（不占锁） |
| 同会话同步骤正在生成 | 409 |
| 会话数达上限且没有已转换会话可删 | 409 |
| 库里的 state 不合法（详情 / 修改 / 生成 / 转换 / 应用） | 409 `灵感会话数据已损坏，无法继续，请删除后重新创建` |

### 5. 必需的测试

`tests/test_inspiration.py`：持久化（换客户端登录可继续）与跨用户 404（全部路由）、决定 / 编辑 / 自填 / 跳过 / keep / clear 与 stale 传播、
补充想法、生成成功保存一轮且提示词含全部轮次反馈 / 已定内容 / 补充想法、保留 10 轮、首轮非法第二轮合法（一个 `retry`、只存一轮）、
连续 3 次非法（两个 `retry` + `error`、会话不变）、直接驱动 `run_generate` 在 Gate 处 `aclose()`（不落库、`fake.aborted == 1`）+ 同步骤 409 +
释放后重试成功、`normalize_options`、规模建议边界（参数化）、转为项目（字段齐全、`wizard_progress.params`、409）、
应用对比默认勾选与只写勾选字段、应用到他人项目 404、从项目预填与复用（填空不标 stale、换一批不带本步自己的值）、会话上限淘汰与 409、库里 state 损坏时 409 且列表 / 删除可用；`test_cross_user_isolation` 含灵感路由；`test_migrations.py::test_0014_inspiration`。
