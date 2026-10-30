# 本任务提交清单

当前分支：`codex/openapi-api-keys`。用户于2026-10-30明确回复“提交 归档”，已授权按以下四组清单执行工作提交，再归档当前任务和记录开发日志；不 amend、不推送。

## 1. feat(api-keys): 支持小说范围授权与可再次查看的访问密钥

- `.env.example`
- `backend/.env.example`
- `docker-compose.yml`
- `backend/alembic/versions/0022_api_keys.py`
- `backend/app/models/api_key.py`
- `backend/app/models/__init__.py`
- `backend/app/schemas/api_key.py`
- `backend/app/services/api_key_service.py`
- `backend/app/core/open_api_deps.py`
- `backend/app/core/config.py`
- `backend/app/api/v1/api_keys.py`
- `backend/app/api/v1/__init__.py`
- `backend/app/services/project_transfer/registry.py`
- `backend/tests/test_api_keys.py`
- `backend/tests/test_migrations.py`

## 2. feat(open-api): 开放小说与创作设定只读接口及文档

- `backend/app/api/open/__init__.py`
- `backend/app/api/open/docs.py`
- `backend/app/schemas/open_api.py`
- `backend/app/services/open_api/__init__.py`
- `backend/app/services/open_api/catalog.py`
- `backend/app/services/open_api/common.py`
- `backend/app/services/open_api/entities.py`
- `backend/app/main.py`
- `backend/tests/test_open_api.py`

## 3. feat(frontend): 增加密钥管理与公开开发文档

- `frontend/src/api/apiKeys.ts`
- `frontend/src/api/apiKeys.test.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/apiKeys/ApiKeyFormModal.tsx`
- `frontend/src/components/apiKeys/ApiKeySecret.tsx`
- `frontend/src/components/StudioNavigation.tsx`
- `frontend/src/pages/ApiKeysPage.tsx`
- `frontend/src/pages/ApiKeysPage.css`
- `frontend/src/pages/HomePage.tsx`
- `frontend/src/pages/HomePage.css`
- `frontend/src/pages/DeveloperDocsPage.tsx`
- `frontend/src/pages/DeveloperDocsPage.css`
- `frontend/src/components/developerDocs/AccessAndResponses.tsx`
- `frontend/src/components/developerDocs/ApiReference.tsx`
- `frontend/src/components/developerDocs/CodeBlock.tsx`
- `frontend/src/components/developerDocs/ErrorsAndDownloads.tsx`
- `frontend/src/components/developerDocs/QuickStart.tsx`
- `frontend/src/components/developerDocs/content.ts`
- `frontend/src/router.tsx`
- `frontend/src/utils/apiKeys.ts`
- `frontend/src/utils/apiKeys.test.ts`
- `frontend/src/utils/apiKeySecrets.ts`
- `frontend/src/utils/apiKeySecrets.test.ts`
- `frontend/src/utils/developerDocs.ts`
- `frontend/src/utils/developerDocs.test.ts`

## 4. docs(open-api): 记录接入方式、接口契约和验收结果

- `README.md`
- `.trellis/spec/backend/open-api.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/auth-admin.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/spec/frontend/component-guidelines.md`
- `.trellis/spec/frontend/directory-structure.md`
- `.trellis/tasks/10-30-openapi-api-keys/`：本任务的 PRD、设计、实施计划、清单、研究与规范摘录、验收和审核记录。

## 明确排除

- `pelican-bicycle.html`：本任务开始前已有的未跟踪用户文件，保持原样，不纳入任何提交。
- 浏览器截图与导出的 OpenAPI JSON 位于本聊天的 visualizations 输出目录，不进入项目代码提交。
- 临时 QA 数据库、测试密钥和进程脚本不在仓库中，不提交。

## 确认后收尾

工作提交完成后，再按 Trellis 流程归档本任务与记录开发日志；它们分别产生归档 / 日志记账提交。除当前任务之外不归档其他任务，不推送远程，不部署生产实例。
