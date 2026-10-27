# Implement

1. schema 字段 + 前端类型
2. wizard_service：校验旧任务、trim/join 工具、build/run 续写分支；提示词模板 wizard.continue.user.j2（registry 注册）
3. handler：传入 retry_of、合并参数
4. WizardGenerator：恢复失败任务、继续按钮
5. 测试：tests/test_wizard*.py 新增续写用例
6. 验证：`cd backend && pytest -q && ruff check . && mypy app`；`cd frontend && npm run lint && npx tsc -b`
