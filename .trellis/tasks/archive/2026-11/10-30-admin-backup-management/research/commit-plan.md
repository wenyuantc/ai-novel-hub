# 已批准提交方案

实施、全量及最终增量、真实恢复/存储/邮件和浏览器验收已完成。用户已明确批准“提交、收尾、合并main并推送”。以下为精确工作提交白名单；不amend、不force push。工作提交后按Trellis归档/记录，再合并并推送origin/main。
.lithe/run/local.json属于未识别并行工作，明确排除并保留原暂存。提交不能使用无路径git commit混入它；归档/日志自动提交前也须核对该保护。

## 1. feat(admin): 实现整库备份与独立存储管理（50个文件）

```text
.dockerignore
.env.example
.gitignore
Dockerfile
README.md
backend/.env.example
backend/alembic/versions/0030_admin_backups.py
backend/app/api/v1/__init__.py
backend/app/api/v1/admin_backups.py
backend/app/core/config.py
backend/app/core/permissions.py
backend/app/main.py
backend/app/models/__init__.py
backend/app/models/backup.py
backend/app/schemas/audit.py
backend/app/schemas/backup.py
backend/app/services/audit_writer.py
backend/app/services/backups/__init__.py
backend/app/services/backups/crypto.py
backend/app/services/backups/database.py
backend/app/services/backups/mail.py
backend/app/services/backups/network.py
backend/app/services/backups/process.py
backend/app/services/backups/runtime.py
backend/app/services/backups/service.py
backend/app/services/backups/storage.py
backend/app/services/project_transfer/exporter.py
backend/pyproject.toml
backend/tests/backup_acceptance.py
backend/tests/backup_acceptance/README.md
backend/tests/backup_acceptance/__init__.py
backend/tests/backup_acceptance/infra.py
backend/tests/backup_acceptance/probe.py
backend/tests/backup_acceptance/smtp_capture.py
backend/tests/helpers.py
backend/tests/test_agent_api.py
backend/tests/test_backup_adapters.py
backend/tests/test_backup_contracts.py
backend/tests/test_backup_migrations.py
backend/tests/test_backup_review_contracts.py
backend/tests/test_backup_runtime.py
backend/tests/test_backup_runtime_review.py
backend/tests/test_backup_storage.py
backend/tests/test_project_cover.py
backend/tests/test_rbac.py
backend/tests/test_rbac_migrations.py
backend/tests/test_rbac_permissions.py
backend/tests/test_reader.py
backend/uv.lock
docker-compose.yml
```

## 2. feat(frontend): 增加整库备份页面与分项授权（26个文件）

```text
frontend/src/api/adminBackups.test.ts
frontend/src/api/adminBackups.ts
frontend/src/api/auditLogs.ts
frontend/src/api/client.ts
frontend/src/api/clientHeadReview.test.ts
frontend/src/api/types.ts
frontend/src/components/AppLayout.tsx
frontend/src/components/adminBackups/BackupOverview.tsx
frontend/src/components/adminBackups/BackupRestoreGuide.tsx
frontend/src/components/adminBackups/BackupRunDetails.tsx
frontend/src/components/adminBackups/BackupRuns.tsx
frontend/src/components/adminBackups/BackupSettingsForm.tsx
frontend/src/components/adminBackups/adminBackupsController.test.ts
frontend/src/components/adminBackups/adminBackupsController.ts
frontend/src/components/adminBackups/backupDisplay.ts
frontend/src/components/adminBackups/backupReview.test.ts
frontend/src/components/adminBackups/backupSettings.ts
frontend/src/components/adminBackups/backupTestFixtures.ts
frontend/src/components/adminBackups/backupUI.test.ts
frontend/src/components/adminBackups/useAdminBackups.ts
frontend/src/pages/AdminBackupsPage.css
frontend/src/pages/AdminBackupsPage.tsx
frontend/src/router.tsx
frontend/src/utils/auditLogs.ts
frontend/src/utils/permissions.test.ts
frontend/src/utils/permissions.ts
```

## 3. docs(admin): 固化整库备份契约与验收记录（42个文件）

```text
.trellis/spec/backend/admin-audit.md
.trellis/spec/backend/admin-backups.md
.trellis/spec/backend/index.md
.trellis/spec/backend/project-transfer.md
.trellis/spec/backend/rbac-quotas.md
.trellis/spec/frontend/index.md
.trellis/spec/frontend/type-safety.md
.trellis/tasks/10-30-admin-backup-management/check.jsonl
.trellis/tasks/10-30-admin-backup-management/design.md
.trellis/tasks/10-30-admin-backup-management/implement.jsonl
.trellis/tasks/10-30-admin-backup-management/implement.md
.trellis/tasks/10-30-admin-backup-management/prd.md
.trellis/tasks/10-30-admin-backup-management/research/acceptance/all.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/browser-desktop.jpg
.trellis/tasks/10-30-admin-backup-management/research/acceptance/browser-mobile-light.jpg
.trellis/tasks/10-30-admin-backup-management/research/acceptance/browser.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/cleanup.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/final-smoke.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/history.jsonl
.trellis/tasks/10-30-admin-backup-management/research/acceptance/image.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/roundtrip.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/smtp.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/storage.json
.trellis/tasks/10-30-admin-backup-management/research/acceptance/summary.md
.trellis/tasks/10-30-admin-backup-management/research/commit-plan.md
.trellis/tasks/10-30-admin-backup-management/research/dependency-audit.md
.trellis/tasks/10-30-admin-backup-management/research/findings.md
.trellis/tasks/10-30-admin-backup-management/research/npm-audit.json
.trellis/tasks/10-30-admin-backup-management/research/pip-audit-final.json
.trellis/tasks/10-30-admin-backup-management/research/pip-audit.json
.trellis/tasks/10-30-admin-backup-management/research/quality.json
.trellis/tasks/10-30-admin-backup-management/research/source-manifest.json
.trellis/tasks/10-30-admin-backup-management/research/spec-access-control.md
.trellis/tasks/10-30-admin-backup-management/research/spec-api-client.md
.trellis/tasks/10-30-admin-backup-management/research/spec-components.md
.trellis/tasks/10-30-admin-backup-management/research/spec-database.md
.trellis/tasks/10-30-admin-backup-management/research/spec-errors.md
.trellis/tasks/10-30-admin-backup-management/research/spec-quality.md
.trellis/tasks/10-30-admin-backup-management/research/spec-theme.md
.trellis/tasks/10-30-admin-backup-management/research/spec-transactions.md
.trellis/tasks/10-30-admin-backup-management/research/verification.md
.trellis/tasks/10-30-admin-backup-management/task.json
```

## 不纳入提交的文件

- `.lithe/run/local.json`：本轮没有读取或修改其内容，保留原暂存。

批准后只按本白名单暂存/提交，并再次检查暂存差异与空白。本次批准来自后续用户消息，原三批文件分组保持不变。

## 执行结果

三批118个文件已按原分组提交，工作提交为：

- f3028c406b9d4a962ec5029ea6e9b9784e97bcbf
- 71087fb3b7d3b3d1a3e23e45e1c2cfa43a8365a5
- 4f395f96868affd28ffaac6ceb4727e5723e90b5

文件清单采用工作提交时的原路径；Trellis归档将另以独立提交移动任务记录。用户另明确授权收尾后合并main并推送，.lithe原暂存保留。
