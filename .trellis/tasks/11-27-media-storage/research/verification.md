# 媒体存储验收

2026-11-27，使用本地临时目录、隔离数据库和真实 HTTPS RustFS。测试仅使用为此次验收创建的账号和文件。

| 范围 | 可复核结果 |
| --- | --- |
| 前端 | `npm run lint`、`npm test`（288 个文件、1,988 项通过）和 `npm run build` 通过。管理页覆盖只读权限、空密钥、切换确认、修订冲突及离页后旧响应。 |
| 媒体、封面与设定缩略图 | `uv run pytest -q tests/test_media*.py tests/test_project_cover.py tests/test_setting_thumbnails.py`，63 项通过，2 项 PostgreSQL 条件用例另行执行。后端 Ruff 全库通过。 |
| 数据库迁移与备份 | SQLite 与 PostgreSQL 16 均通过 0080 → 0081 → 0080 往返及有数据拒绝降级，合计 4 项。另验证 SQLite 整库快照恢复媒体元数据，单书导出排除库外媒体。 |
| 并发与清理 | PostgreSQL 16 两项并发用例通过。并发上传预留配额，删除正在上传的对象会阻止最后发布；阻塞的存储删除不占据数据库写锁，过期租约可接管。回收站和媒体哨兵 33 项通过。 |
| 浏览器 | 真实 Chrome 将 64,154,296 字节的视频拖动到 31.82 秒并继续播放，接收约 1.48 MB。375px 宽度通过管理页切换到 S3 再切回本地。[详细记录](browser-verification.md) |
| 双实例与部署 | 在三个同机应用进程、PostgreSQL 16、独立 TLS RustFS 上验证共享卷、独立卷及 S3。A 写入 56 MiB 文件，B 分段读取成功；挂错卷的 C 得到可诊断的 503，切换 S3 后 C 立即可读。两份 Compose 配置渲染验证通过。[详细记录](deployment-verification.md) |

本地后端在 POSIX 上通过子进程强制退出后的临时文件回收测试；Windows 真机和跨物理主机的共享卷未在本次环境覆盖。

最终后端全量回归执行 `LC_ALL=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20`。7,769 项通过，140 项条件跳过，19 项预期失败，21 条警告，耗时 40 分 38 秒。
