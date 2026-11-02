# 管理员整库备份跨层契约

## 1. 范围与触发

修改实例整库备份、备份权限/配置、运行器、存储、邮件、下载或后台页面时适用。代码位于 `services/backups/`，传输唯一来源为 `schemas/backup.py`；作品 JSON 导入导出和个人 TaskEngine 沿用原契约。数据库外的 `.env`、原 `ENCRYPTION_KEY` 与恢复密码必须另行保管。只提供下载及命令行恢复说明，没有 HTTP 整库覆盖恢复。

## 2. 接口与持久模型

前缀 `/api/v1/admin/backups`，所有响应含 `Cache-Control: no-store`。

| 方法 / 相对路径 | 权限 | 结果 |
| --- | --- | --- |
| GET 空路径 | backups.read | BackupOverview，安全概览/就绪原因/计划/最近结果 |
| GET /settings | backups.configure | BackupSettingsOut，非秘密连接参数及 configured 标志 |
| PATCH /settings | backups.configure | `{revision, changes}` CAS 后安全配置 |
| GET /runs | backups.read | status/source/page/page_size，`{items,total}` |
| GET /runs/{id} | backups.read | 执行/产物/独立清理和通知状态 |
| POST /runs | backups.run | `{request_id:UUID,settings_revision}`，202 表示受理 |
| HEAD / GET /runs/{id}/download?destination=local或s3 | backups.download | 预检 / 鉴权附件流 |

后三项动作权限均显式 requires `backups.read` 且 dangerous。0030（父0029）创建三个实例表：`BackupSettings` 单行 id=1/CAS/实例身份/计划游标；`BackupRun` 有整数自增ID及不可复用execution_key，request_id和scheduled_date分别唯一，queued/running必须active_slot=1，唯一约束兜底互斥；`BackupArtifact` 每执行/目的地唯一，私有locator/访问密文/S3版本与multipart身份。文件字节数为BigInteger，不能使用PG的32位Integer。实例表不指向projects、不登记作品备份规则；作品JSON仍format v1，APP_SCHEMA元数据同步0030。

`BackupManager(settings,cipher,sessionmaker,notify)` 提供load/update/accept_manual/accept_scheduled/read_settings/overview/list_runs/get_run/authorize_download/close；`BackupRuntime(manager)` 提供start/stop/notify/prepare_download。快照、GPG、SMTP和文件/S3阻塞工作通过 `process.run_blocking` 在线程执行。

0030只更新现存内置super_admin为37项并递增角色与授权revision；其他内置和自定义角色不增权。历史0029固定33项，禁止改写历史常量。初始化只补缺失角色，已有内置角色严格校验，故不能仅改运行时代码而遗漏迁移。存在备份数据、新权限分配或备份审计时禁止有损downgrade。

## 3. 请求、状态与部署契约

默认定时关闭、北京每天03:00、本地开启、S3关闭、两处各7份成功产物、加密开启且密码未设不可执行。时间可改，存储开关独立，定时关闭仍可手动。保留1–1000份，分页默认20/最多100。开启定时要求存储/密码/失败邮件完整；手动可在邮件未配置时执行并如实提示。

秘密字段为backup_password/s3_access_key/s3_secret_key/smtp_username/smtp_password：省略保持、非空替换、null显式清除、空字符串拒绝。SecretStr/repr不带秘密，SecretCipher加密保存，读取只返回`*_configured`。备份密码16–1024字符，不trim，禁止CR/LF/NUL。JSON层extra=forbid，bool/int严格，S3端点HTTPS且无身份/路径/查询，SMTP只SSL/STARTTLS和EmailStr收件人。

```json
{"revision":3,"changes":{"schedule_time":"03:00","local_keep":7,"s3_keep":14,"encryption_enabled":true}}
```

执行状态：queued/running/succeeded/partial/failed/interrupted；产物：pending/ready/failed/unknown/deleting/deleted/missing。通知：not_configured/pending/sending/sent/failed/unknown；清理独立，不把新副本成功改成失败。succeeded且无清理异常、通知not_configured显示“无需失败提醒”；sent仅SMTP受理。下载授权与文件送达、手动受理与执行完成必须区分。

部署变量仅运维管理：BACKUP_DIR、BACKUP_PG_DUMP_PATH、BACKUP_GPG_PATH、BACKUP_GPGCONF_PATH、BACKUP_TIMEOUT_SECONDS（3600）、ALLOW_PRIVATE_BACKUP_ENDPOINTS（false）、BACKUP_CA_BUNDLE。容器有PG16客户端、GnuPG/gpgconf和tzdata。两份忽略文件锚定排除`/backups/`和`/backend/backups/`，禁止无锚规则误忽略services/backups源码。

## 4. 验证、错误与副作用边界

| 情况 | 行为 |
| --- | --- |
| 会话无效 / 缺少当前动作 | 401 / 403 permission_denied；写前复核原token_version |
| 配置旧revision / 活动执行 / 意图冲突 | 409；不覆盖、不新建执行意图 |
| 准入未就绪（含缺工具/运行器待核实） / 不合法输入 | 400 backup_not_ready / 安全422 |
| 记录不存在 / 文件清理或缺失 | 404 / 410 |
| 持久配置非法/不可读；下载暂不可访问 | 安全503；配置故障只暂停备份准入，不更改六项全局设置；已受理存储错误记执行/产物状态 |
| 外部写入/删除已发生但确认丢失 | 保留unknown/deleting意图并精确核实，不盲重放或假报失败/删除 |

锁序：manager锁→共享AuthorizationRuntime→授权DB gate→actor User→备份资源。配置和受理成功审计与业务同事务，commit事实确认后发布/唤醒；调用者取消不能拆断shield写任务。未知提交重读，已存在成功不补相反失败。局部审计`backup.settings.update`、`backup.run.accept`、`backup.download.authorize`的backup_change仅受限字段名/版本/执行UUID/目的地/加密标志，不存连接参数、密码长度、原异常或SQL。

受理冻结配置；之后关闭目的地、改密码/份数只影响新受理。长I/O不持SQL事务/授权锁。取消/关停必须等底层线程和进程组结束再终态释放active_slot；shield等待者自身取消也不得提前返回。定时持久next_scheduled_at，Asia/Shanghai日期键每日去重，离线只补最近到期一次，忙碌保留意图。重启将遗留活动执行记interrupted，按已知locator/大小/hash核对可用产物，不自动重放外部副作用。

PostgreSQL用pg_dump -Fc，无shell，密码仅0600临时PGPASSFILE，实际schema_revision查询数据库。SQLite用Connection.backup包含已提交WAL，并quick_check，禁止内存库冒充持久备份。GPG对称AES256/loopback/passphrase-fd/no-symkey-cache，密码不进argv/env/密码文件；原生后缀.dump/.sqlite3，加密加.gpg。恢复先完整解密且验证成功，再导入隔离/已核对目标。明文0700/0600并及时清理。

GPG2.2代理套接字受Unix路径长度限制，GNUPGHOME不能放在长备份工作目录下；使用短私有路径与实例/执行marker、uid/nosymlink校验，正常finally和重启只回收该执行自己的agent/home，不扫描或kill其他GPG代理。终态cleanup_status为failed/unknown/pending时，重启也按已知实例/执行身份精确清理.work目录，明文和加密模式均适用；GPG home与work目录独立尝试，回收成功不改写原保留清理失败历史。

本地原子发布且不覆盖身份未知文件；根/实例/执行身份和无symlink校验。S3显式凭据、禁隐式代理/默认凭据链、有界managed multipart；每次请求校验HTTPS/fixed origin/全部DNS，跨origin不继续。私网仅部署显式许可，metadata/link-local始终禁；仍有DNS检查到连接的窗口，由部署出站策略兜底。旧对象保存原位置和加密访问快照，同位置优先最新凭据；清理指定version_id，不能仅造delete marker或清整个bucket。两处各自新成功后按该次冻结keep清成功副本；失败不挤占名额。

下载先授权、打开资源、再复核原会话并原子记录GET授权，附件no-store/nosniff/Content-Length。响应整个ASGI生命周期finally关闭资源：Starlette先发送headers再迭代正文，不能仅靠生成器finally或BackgroundTask，否则首块之前断开会漏关。main响应头中间件采用纯ASGI包装，不引入BaseHTTPMiddleware子任务破坏原RBAC并发语义。

## 5. 正常、基线与异常案例

- 正常：已配置密码、本地+S3开启，两个ready为succeeded；各按独立keep清理，旧加密文件仍用原密码。
- 基线：首次定时关闭、密码未设，概览显示未就绪；不自动降为明文。
- 异常：仅S3失败则partial，本地仍可下载；SMTP超时为unknown且不盲重发，不把部分成功显示为完整成功。
- 异常：另一管理员保存后本地PATCH409，保留草稿并核实重载；不自动覆盖。

## 6. 必须验证的断言

后端contracts/migrations/review覆盖旧0029与0030、37权限直接HTTP矩阵/撤权、CAS/空值秘密、原子审计、确认丢失/调用者取消和首块前关闭；adapters/runtime/storage覆盖WAL、真实进程回收、线程排空、冻结配置、计划去重、独立保留和未知结果恢复。精确测试文件以`backend/tests/test_backup*.py`为准。

`backend/tests/backup_acceptance.py --scenario all --output DIR`是真实隔离入口：PG16/SQLite×明文/GPG，恢复另一目标比对业务/原密钥；TLS S3 multipart及版本化清理、SMTP捕获与250丢失、并发/重启/中断和日志无秘密。随机秘密、CA私钥、数据库二进制仅仓库外私有目录；退出仅回收自己带身份标签的资源。不能以skip/mock/小文件替代真实验收。

前端按backend schema镜像，通过api模块传输；controller守卫actor+authorizationKey/请求代次/StrictMode/离页，秘密不进store持久化，配置撤权清草稿，保存不自动重试。稳定request_id在结果未知时复核原意图；普通HTTP缺crypto.randomUUID时以getRandomValues生成v4。原生下载HEAD预检并再次校验身份，不能整库Blob。HEAD403没有JSON正文，统一客户端按原请求method刷新当代次授权，不重发业务，旧会话响应不得刷新新会话。next计划显式北京时间；桌面表格、375px卡片、主题与真实交互另做浏览器验收。

## 7. 错误与正确写法

错误：保存后先发布配置再commit；取消to_thread等待后立刻释放active_slot；GPG home直接放深层备份目录；用新版目录改写0029；下载只在generator finally关流。

正确：持久确认后发布，未知事实暂停本模块并重读；发stop并排空底层后释放；短路径且可核验归属的GPG home；0030迁移存量目录而历史固定；整个响应生命周期finally关资源。全量门禁仍是后端ruff/pytest和前端lint/build/test，审计扫描的原有框架告警单独如实记录。
