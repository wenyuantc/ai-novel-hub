# 现有契约核实

日期2026-11-01；未读取.env、真实数据库或部署凭据。

## 后端

- config.py已有六项；整数最小1，单用户可大于全局。main.py:95-107与core/deps.py:17-18共享Settings；仅替换app.state会遗留旧引用。
- task_engine/registry.py:25-33说明每次使用settings；engine.py:1-15、120-122、210-246有单进程前提、notify和两级动态调度。无需用cancel降低并发。
- analysis_service.py:586-617在成功后enqueue_auto检查开关；batch_service.py:136-137、510-520在分析阶段检查。手动与已排队任务保持原行为。
- api_key_service.py:200-265传入每请求阈值并数据库原子扣减UTC分钟；不可重置rate_count/rate_window。
- mcp/service.py:280-305、334-383在数据库/连接await之后才调用，需补实际出站前guard。agent/mcp_tools.py:107-138的resolve检查不足以覆盖202-244的独立Worker等待/连接/队列。
- audit_writer.py:185-249同事务审计及安全失败，确认丢失不追加相反结果；运行时另需协调发布。schemas/audit.py封闭传输定义，必须窄扩展。
- 当前Alembic head0026；tests/conftest.py:50-67用create_all并显式引擎start、不触发lifespan。
- project_transfer/registry.py:62及test_project_transfer.py:64-66仅接受项目相关NOT_EXPORTED；无业务FK的实例设置表不要加入。
- Dockerfile:26和数据库规范485-489明确单进程，不承诺多worker一致性。

## 前端

- stores/auth.ts:25-39仅init加载auth/config；App.tsx覆盖公开/登录/工作区，适合单一公开配置同步，不重复me。
- dashboardController.ts:130-151已有可见轮询；adminTasks/useAdminTasks.ts:24复用5秒，dashboard默认60秒。提取小调度器时必须保留旧节奏和测试。
- AppLayout导航/账户/面包屑一起补入口；router.tsx后台使用RequireAdmin+lazyPage。
- api/client.ts统一Cookie、ApiError/401、AbortSignal；api/auditLogs.ts为审计标签源。
- Vitest为node环境、只收集.test.ts，无jsdom；controller/纯函数及SSR加真实浏览器交互，不能以SSR替代。

## 质量边界

遵守backend/frontend质量规范、auth-admin、skills-mcp、open-api、admin-audit及跨层指南。后端独立tmp_path SQLite、FakeLLM/FakeMcp、Gate/Event，迁移UTF-8 locale；前端test/lint/build与真实浏览器。测试必须通过设置API验证真实消费行为，不只读内存属性。保护既有.codex/config.toml、.gitignore及两个source-command skill未提交修改。
