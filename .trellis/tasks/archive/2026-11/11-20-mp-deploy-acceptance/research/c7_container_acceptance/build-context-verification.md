# C7 构建上下文与最终镜像验证

`.dockerignore` 覆盖根目录和嵌套目录的 `.env` 变体，只保留精确的 `.env.example`；排除常见 SQLite 数据库、WAL/SHM/journal、租约锁、数据库导出、备份归档、日志及轮转日志。检查已追踪文件时，需要保留的匹配项为根目录及后端 `.env.example`、`backend/uv.lock`；没有发现需要保留的已追踪数据库、导出或备份归档源码。

## 假数据上下文

```sh
cd backend
python3 tests/c7_build_context_smoke.py
```

使用系统临时目录中的虚构文件和独占镜像标签，以 Docker 默认 BuildKit 实际构建；39 个代表性敏感文件未进入镜像，7 个示例和源码文件保留，包括两份 `.env.example` 与 `uv.lock`。测试覆盖 `.env.local`、SQLite 伴随文件、租约锁、数据库导出及 `.log.1` / `.log.gz`。本轮镜像与容器已清理。旧版构建器也在此前规则版本做过对照；最终规则的上述结果来自默认 BuildKit。

## 最终应用镜像

```sh
cd backend
python3 tests/c7_application_build_smoke.py
```

构建上下文仅来自 Git HEAD 的受控归档和显式列出的工作区 C7 文件；在归档提取时排除真实环境配置、数据库和备份，再覆盖最终 `.dockerignore`、Dockerfile、迁移环境、容器入口与恢复提示文件。实际使用默认 BuildKit 构建，容器使用随机测试配置、`--network none` 和 tmpfs 内的临时 SQLite；`/healthz`、读取数据库设置的 `/api/v1/auth/config` 和前端 `/` 均通过，测试镜像、容器及临时目录已清理。

- 构建上下文 `.dockerignore` 与工作区 SHA-256：`ebdad1ffde4c13468dad78d4e7fe575a507ff359992b5c0b9fc786b4b2bb26b4`。
- 镜像内和工作区 `backend/alembic/env.py` SHA-256：`2f49468408f6c0ef12ba0cf70294845cd3676860ff27f91be278effd984b9eed`。
- 镜像内和工作区 `backend/app/services/backups/sqlite_restore.py` SHA-256：`68839655d5a7f62c1eabc17ca6fdc7a06057165608e7f678e97d00d5bd61098c`。

验证只覆盖列出的常见文件名及当前已追踪源码；无扩展名或采用其他命名的用户数据不能靠这些规则自动识别，应移出构建目录或另行配置排除。归档、SQL 和日志扩展被整体排除；将来如需把这些格式作为运行源码，须显式审查并调整规则。未清理共享 BuildKit 缓存，避免影响其他构建；本轮输入均为受控源码或虚构文件。历史双实例从零记录使用旧过滤规则和旧恢复提示，不代替本次镜像验证。
