# Implement：模型配置增强

## 后端
1. [ ] `models/entities.py` AIConfig 增三列；新建 `alembic/versions/0002_ai_config_model_params.py`。
2. [ ] `schemas/ai_config.py`：ThinkingLevel、字段与校验、`ModelListRequest`、`ModelInfo`、`ModelListResult`。
3. [ ] `ai/providers/base.py`：`ThinkingLevel`、`ModelInfo`、`generate_stream(thinking=)`、`list_models()`。
4. [ ] 三个 provider 实现思考映射与 list_models。
5. [ ] `services/ai_config_service.py`：create/update/to_out 新字段与校验、`context_char_budget`、`list_models`（含 Key 解析、SSRF、超时、错误转换）；`test_config` 显式 off。
6. [ ] `api/v1/ai_configs.py`：`POST /ai-configs/models`。
7. [ ] `generation_service.py` / `wizard_service.py`：Job 带 thinking/max_tokens，上下文预算按配置。
8. [ ] `tests/helpers.py` FakeLLM：记录 thinking/max_tokens，支持 list_models 预设。
9. [ ] 测试：CRUD 新字段与校验、迁移 0002、models 接口（新 Key / 用已存 Key / SSRF 拒绝 / provider 报错 400）、生成调用透传 thinking 与 max_tokens、预算计算、provider 参数映射单测（旧/新 Claude、Gemini 2/3）。

## 前端
10. [ ] `api/types.ts`、`api/aiConfigs.ts`。
11. [ ] `pages/AIConfigPage.tsx` 表单与列表列。

## 校验
- `cd backend && uv run ruff check . && uv run pytest`
- `cd frontend && npm run lint && npm run build && npm test`
- 手工：启动前后端，新增配置 → 获取模型 → 保存 → 列表展示参数。

## 回滚点
- 迁移可 downgrade；代码改动集中在上述文件，git 回退即可。
