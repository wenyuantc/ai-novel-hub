# 媒体存储部署验收

2026-11-27 在同一台开发机上以独立 Uvicorn 进程运行应用。PostgreSQL 16 和 RustFS 使用一次性 Docker 容器，发布端口仅绑定回环地址。用临时 CA 启用并校验 RustFS 的 HTTPS，管理员、密钥、数据库、本地目录与 S3 桶均为此次测试单独创建。测试覆盖同机部署；跨物理主机的挂载与网络仍需在目标部署验证。

| 验证 | 操作 | 结果 |
| --- | --- | --- |
| 单实例和双实例 Compose 配置 | 分别用独立环境变量执行 `docker compose --env-file /dev/null -f <compose文件> config --format json`；检查 app、app1 和 app2 | 三个服务均将指定宿主机目录读写挂载到 `/app/media`，容器内 `MEDIA_DIR=/app/media`，收到相同的 `PUBLIC_BASE_URL` |
| 真实后端读写 | 分别使用共享目录和 RustFS。进程 A 调用存储后端写入 56 MiB（58,720,256 字节），进程 B 读取、复制和删除 | 两种存储均通过。整读 SHA-256 一致，中段 30 字节与原始数据一致，复制大小一致，删除后 stat 返回空。RustFS 预签名链接可匿名读取指定范围 |
| 共享本地卷 | 从空 PostgreSQL 数据库迁移到 0081，启动 A、B 两个应用并登录，由 A 的业务会话调用媒体服务写入 56 MiB 合成 MP4；用登录态直接访问 B 的媒体接口 | 中段读取返回 HTTP 206，`Content-Range` 起止位置和总大小正确，响应字节与原文一致。管理接口返回两份卷上报，`volume_mismatch=false` |
| 未共享本地卷 | 启动第三个应用 C，共用该数据库，指定独立的空媒体目录，读取 A 创建的同一对象 | C 返回 HTTP 503 和 `media_unavailable_on_instance`。管理接口展示三个进程的卷上报及 `volume_mismatch=true` |
| S3 跨实例 | 管理员在 A 通过 `POST /api/v1/admin/media-storage/test` 测试真实 RustFS 并通过 `PATCH /api/v1/admin/media-storage` 切换位置；A 写入另一份 56 MiB 合成 MP4，C 随即读取 | 两个管理请求均返回 200。即使本地目录不同，C 仍返回 HTTP 206，响应范围和内容正确 |

首轮 S3 表单测试发现校验器调用错误导致 500。修复后从另一个空数据库重新迁移、重启三个应用，并完整重跑上述 HTTP 场景。既有启动回归测试 `cd backend && uv run pytest -q tests/test_c7_container_startup.py` 通过 16 项。
