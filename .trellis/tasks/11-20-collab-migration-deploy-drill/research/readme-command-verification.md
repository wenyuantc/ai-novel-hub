# README 备份命令复验

2026-11-25（北京时间），对 SHA-256 为 `d1f43f9a32258a01acd7c54b20a939707438465d7ef1afedbc347eb44b2d093b` 的 README 执行：

```bash
# c8_dir 指向本任务目录；归档后改为对应的 archive 目录。
c8_dir=.trellis/tasks/11-20-collab-migration-deploy-drill
backend/.venv/bin/python "$c8_dir/research/check_readme_commands.py"
backend/.venv/bin/python "$c8_dir/research/check_readme_postgres_compose.py"
```

第一个脚本抽取 README 的 28 个 Shell 代码块，均通过 `bash -n`。它实际运行 SQLite 快照示例，验证未检查点的 WAL、单文件库、路径空格和引号、拒绝覆盖、拒绝源和目标相同、拒绝不匹配的迁移版本、`0070` 备份及 SHA-256；PostgreSQL 示例另用模拟 Docker 检查正常执行、拒绝覆盖、停服失败、备份失败和空输出。这些失败情况下不会报告生成了最终快照。

第二个脚本抽取 README 的 PostgreSQL 停服及快照命令，在独立 Compose 项目的 PostgreSQL 16 临时文件系统上执行真实 `pg_dump`，再恢复至新建空库；应用容器确已停止，快照为 1,745 字节，恢复后版本为 `0043`，虚构标记完整。镜像 ID 为 `sha256:c54a696c68013b2f8f42f479e00a2b6ad24c5ef10ee31cdfb8972cec3dad7eb7`。脚本结束时删除了该专属 Compose 项目及其资源。

上述命令复验不替代真实旧应用的数据升级演练、独立 README 全流程验收，也不覆盖生产代理、GPG 或跨主机环境。
