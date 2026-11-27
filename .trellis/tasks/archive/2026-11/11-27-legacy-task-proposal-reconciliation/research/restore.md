# 升级前快照回退对照

2026-11-27，两种数据库各自运行一套配套的真实应用，从 `4501f18` 的 `0043` 旧版生成快照。全程使用虚构账号、专属数据库、原夹具密钥与仅监听本机的假模型。

1. 作者通过旧 API 创建作品和章节，等待章节生成成功。再产生一条创建角色的提案，取消所属任务并等待 cancelled。停掉旧应用后查询任务表，所有任务均为 succeeded 或 cancelled。原提案仍为 proposed。
2. 将快照恢复到空目标，升级至 `ac97d4d` 的 `0079`，使用新应用登录并新建一部作品。停掉该应用，保留新版数据库。
3. 再从同一份旧快照恢复到另一个空目标。确认版本为 `0043`，启用配套旧程序并以原账号登录，比较原章节 ID、标题、正文、状态及归属，复查两项原任务。原提案的整份旧 API 响应与快照前相等。回退库有一部作品，新版库有两部。用原模型配置发起一次独立章节生成，验证原密钥可用。

| 后端 | 快照大小 | 回退后章节与任务 | 回退后提案 | 模型请求 |
| --- | ---: | --- | --- | --- |
| [SQLite](restore-sqlite.jsonl) | 1,576,960 字节 | done，原生成任务 succeeded，智能体任务 cancelled | proposed，原响应一致 | 3 次预期，0 次意外 |
| [PostgreSQL](restore-postgres.jsonl) | 405,342 字节 | 同上 | 同上 | 3 次预期，0 次意外 |

完整快照 SHA-256、原提案公开响应摘要和各阶段 UTC 见对应日志。两条命令均退出 0，PG 专属容器已验证删除。

在仓库根目录可分别运行以下命令。脚本调用 C8 的旧源码导出、迁移、快照、恢复和应用进程管理；执行需要本地 uv、Python 3.12、后端依赖，PG 另需 Docker 和 C8 的 PostgreSQL 镜像。

归档后复跑时，请在仓库根目录的同一终端执行下列命令。第一行指定 C8 控制器的位置，以保留原演练脚本及其校验值。

```sh
export PYTHONPATH="$PWD/.trellis/tasks/archive/2026-11/11-20-collab-migration-deploy-drill/research"
backend/.venv/bin/python .trellis/tasks/archive/2026-11/11-27-legacy-task-proposal-reconciliation/research/restore_control.py sqlite
backend/.venv/bin/python .trellis/tasks/archive/2026-11/11-27-legacy-task-proposal-reconciliation/research/restore_control.py postgres
```

这里验证的是已排空快照的独立恢复，以及保留新版库以便核对差异。未排空旧任务的专门证据见同目录章节演练。再次恢复旧快照仍会保留快照中原本未决的提案。
