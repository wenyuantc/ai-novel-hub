# 实施环境核实

2026-11-01（Asia/Shanghai）。任务分支codex/admin-system-settings，原有四个用户文件SHA256见implementation-baseline.json。Git分支写默认沙箱拒绝，经工具审批创建；未提交或推送。

- 已有backend/.venv、uv、Node/npm/npx和前端node_modules，不安装新依赖。uv缓存可能受限，代理使用现成虚拟环境等价运行。
- 标准前端缓存/dist在前任务已证实可能权限受限；允许使用本任务临时HOME/cache/buildinfo/生产输出，不修改权限或工具配置。实际结果待门禁报告。
- PATH没有postgres/psql/pg_ctl；不读取真实数据库连接，不把SQLite验证声称PG通过。
- 现有本地8000服务不是本任务启动，不操作或关闭。浏览器预览使用独立127.0.0.1:8769、新临时SQLite、合成账号与FakeLLM。
- 本地缓存Playwright CLI可用，无需联网安装。独立Chrome会话admin-system-settings；默认沙箱启动失败，经工具审批使用临时资料启动，不访问日常浏览器资料。daemon目录使用CLI已核实的PWTEST_DAEMON_SESSION_DIR=/private/tmp/admin-system-settings-playwright。
- 预览脚本/private/tmp/admin_system_settings_runtime.py；浏览器资料与证据/private/tmp/admin-system-settings-browser。仅在真实验收完成后记录通过，不以准备脚本冒充已验收。
