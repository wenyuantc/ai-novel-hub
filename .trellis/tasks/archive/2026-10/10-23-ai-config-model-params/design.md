# Design：模型配置增强

## 1. 数据模型（`ai_configs` 新增列，迁移 `0002`）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `thinking_level` | `String(16)` | NOT NULL, server_default `'off'` | `off` / `low` / `medium` / `high` |
| `context_window_k` | `Integer` | NULL | 上下文大小，单位 K（×1000 tokens） |
| `max_output_k` | `Integer` | NULL | 最大输出，单位 K（×1000 tokens） |

K 取 1000 而非 1024：厂商标称的 64K 输出上限实为 64000，按 1024 换算会超限报 400。
迁移用 `batch_alter_table`（兼容 SQLite 测试），downgrade 删除三列。

## 2. Schema / API 契约

`ThinkingLevel = Literal["off","low","medium","high"]`

- `AIConfigCreate` 增：`thinking_level = "off"`，`context_window_k: int|None (1..2000)`，`max_output_k: int|None (1..256)`；
  `model_validator`：两者都非空时 `max_output_k < context_window_k`，否则 422。
- `AIConfigUpdate` 同字段均可选；`context_window_k` / `max_output_k` 用 `model_fields_set` 区分「未传」与「显式置空」。
  更新后在 service 中对合并后的值再做一次大小校验（`BadRequestError`）。
- `AIConfigOut` 增三字段。
- 新接口 `POST /api/v1/ai-configs/models`
  - 请求 `ModelListRequest { provider, base_url?, api_key?, config_id? }`
  - Key 解析：`api_key` 非空用它；否则若有 `config_id` 用该（当前用户所有的）配置已存 Key；否则空串（OpenAI 兼容本地模型可无 Key）。
  - 响应 `ModelListResult { models: [{ id, name? }] }`，按 id 排序去重。
  - 走 `ensure_endpoint_allowed` + `follow_redirects=allow_private`，与 `build_provider` 一致；超时 30s。
  - provider 报错 → `BadRequestError(中文消息)`（400，前端直接展示 detail）。
  - 路由须声明在 `/{config_id}` 之前（POST 无冲突，但保持习惯）。

## 3. Provider 层

`LLMProvider.generate_stream(messages, model, *, temperature=None, max_tokens=None, thinking="off")`
新增 `async list_models() -> list[ModelInfo]`（`ModelInfo = TypedDict{id, name}`）。

| 等级 | OpenAI 兼容 | Anthropic（4.6+） | Anthropic（旧模型） | Gemini 3+ | Gemini 2.x |
|---|---|---|---|---|---|
| off | 不传 | 不传 thinking | 不传 | 不传 | 不传 |
| low/medium/high | `reasoning_effort=<lvl>` | `thinking={"type":"adaptive"}` + `output_config={"effort":<lvl>}` | `thinking={"type":"enabled","budget_tokens":2048/8192/16384}`，max_tokens 不足时抬到 budget+4096 | `thinking_config.thinking_level=LOW/MEDIUM/HIGH` | `thinking_config.thinking_budget=2048/8192/24576` |

- 旧 Claude 判定：模型名匹配 `claude-3*`、`claude-{opus,sonnet,haiku}-4` / `-4-0`/`-4-1`/`-4-5` 及日期后缀形式。
- Gemini 2.x 判定：模型名含 `gemini-2` / `gemini-1`。
- 「关闭」不强制禁用：新 Claude（Opus 5.5 等）显式 disabled 会 400，故统一「不传」，由模型默认决定。
- 只 yield 正文文本：Anthropic `text_stream` 已不含 thinking；OpenAI 中转的 `reasoning_content` 本就不读；Gemini 跳过 `part.thought` 为真的分片。

list_models：
- OpenAI 兼容：`client.models.list()` → `id`。
- Anthropic：`client.models.list()` → `id`, `display_name`。
- Gemini：`client.aio.models.list()`，保留 `supported_actions` 含 `generateContent` 的（字段缺失则保留），`name` 去 `models/` 前缀，`display_name`。

## 4. 生成链路

- `ChapterJob` / `WizardJob` 增 `thinking: str`、`max_tokens: int|None`，由 cfg 填充：`max_tokens = max_output_k*1000 if set else None`。
- `run_*_job` 调 `generate_stream(..., max_tokens=job.max_tokens, thinking=job.thinking)`。
- `test_config` 固定 `thinking="off", max_tokens=16`。
- 上下文预算 `ai_config_service.context_char_budget(cfg, default)`：
  - 未设 `context_window_k` → `default`（全局 `CONTEXT_CHAR_BUDGET`）。
  - 否则 `(context_window_k - (max_output_k or 8)) * 1000 - PROMPT_RESERVE_TOKENS(4000)`，按 1 字 ≈ 1 token 估算为字符数，下限 1000。
  - 在 `prepare_chapter_job` 中替代传入的 `context_budget`（传入值作为默认）。

## 5. 前端

- `types.ts`：`ThinkingLevel`、`AIConfig`/`AIConfigCreate`/`AIConfigUpdate` 增字段；`ModelInfo`、`ModelListRequest`。
- `aiConfigApi.listModels(body)`。
- `AIConfigPage` 表单：
  - 模型：`Space.Compact` = `AutoComplete`（options 来自拉取结果，按输入过滤）+「获取模型」按钮（loading）。
    切换接口类型时清空候选。获取前校验 provider/base_url；成功提示「共 N 个模型」。
  - 思考等级：`Select`（关闭/低/中/高），新建默认 off。
  - 上下文大小、最大输出：`InputNumber`（整数，`addonAfter="K"`，可清空），并排；最大输出加 `dependencies` 校验小于上下文。
  - 列表新增「参数」列：`思考:低 · 上下文:128K · 输出:8K`，未设显示「默认」。
- 提交时 `null` 表示清空。

## 6. 兼容 / 回滚

- 新列有默认值/可空，旧数据行为不变（off、默认预算、默认 max_tokens）。
- 回滚：`alembic downgrade 0001` + 回退代码。
