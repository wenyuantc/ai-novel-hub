# 提交与收尾计划

2026-11-01用户已明确授权，不再重复询问。

1. `feat(admin): 新增系统设置与运行时配置覆盖`：backend/frontend产品、迁移、测试及README。
2. `docs(admin): 固化系统设置契约与验收记录`：相关规范和本任务规划/研究/验收文档。
3. Trellis归档（no-commit后单独中文commit）：`chore(task): 归档管理员系统设置任务`。
4. Trellis日志（no-commit后单独中文commit）：`chore(workspace): 记录系统设置交付会话`。
5. 合并到main并普通推送origin/main；禁止force/amend。

所有路径清单见research/commit-plan.json。排除用户原有.codex/config.toml、.gitignore和两个source-command skill文件，SHA256保护不变。其他三个planning任务不归档、不修改。质量证据见verification.md：后端1332+最终40项、前端634、浏览器验收，PG未实测及标准构建权限边界不变。
