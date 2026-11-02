# 备份管理调研与实施约束

日期：2026-11-02。范围：本任务的代码证据、官方资料及实现边界；产品决定以 `prd.md` 为准。

## 仓库证据

| 证据 | 结论 |
| --- | --- |
| `README.md:483`、`README.md:487` | 作品 JSON 导入会创建新作品，不包含账户和平台凭据，不能充当整库备份。 |
| `README.md:491`、`README.md:502`、`README.md:520` | 已有 PostgreSQL 手工导出/恢复与 SQLite 停服复制指引；原 ENCRYPTION_KEY 仍须独立保管。 |
| `README.md:659`、`backend/app/main.py:72` | 继续遵循单进程部署；备份运行器接入 lifespan，自行恢复遗留执行，不扩展多实例调度。 |
| `backend/app/core/permissions.py:15` | 当前 33 项权限，没有备份动作；新增四项后当前目录为 37 项。 |
| `backend/app/services/authorization_service.py:79`、`backend/app/services/authorization_service.py:141` | initialize 只补缺失内置角色，不刷新现存角色权限；读取又严格校验内置角色与目录相等。必须通过新迁移更新现存 super_admin 元数据，否则升级后授权读取会失败。 |
| `backend/alembic/versions/0029_rbac_quotas.py:59` | 历史迁移冻结旧语义。0030 负责新增语义和安全回退，不修改 0029 的 33 项冻结目录，不向自定义角色自动添加备份权限。 |
| `backend/app/services/system_settings_service.py:33`、`backend/app/services/system_settings_service.py:122` | 可复用版本条件更新、提交确认、发布与 HTTP 取消隔离模式；备份使用自己的配置表和确认状态，保留原六项系统设置白名单。 |
| `backend/app/services/audit_writer.py:52`、`backend/app/core/security.py:47` | 管理变更与成功审计同事务；短凭据复用 SecretCipher，备份文件采用独立文件加密。 |
| `frontend/src/api/projects.ts:76` | 现有 downloadAttachment 整份构造 Blob，整库下载需另建原生流式下载入口，保持现有作品下载契约。 |
| `backend/app/services/project_transfer/exporter.py:35` | 当前 APP_SCHEMA 仍为0024；按既有数据库规范同步为0030，仅更新元数据，作品 JSON 格式仍为v1。 |
| `frontend/src/utils/permissions.ts:27` | 新入口接入统一管理导航、具体动作与授权代次。 |
| `Dockerfile`、`docker-compose.yml`、`backend/pyproject.toml` | 应用镜像尚无 pg_dump/GPG、备份卷和 S3/SMTP 实现；aiosqlite 目前仅是开发依赖。 |
| `.dockerignore` | 已排除.env和工具目录，但尚未排除备份目录；需与Git忽略同时增加锚定运行目录规则，防止构建传入备份且避免误忽略services/backups源码。 |

## 官方资料与选择

- PostgreSQL 使用 `pg_dump -Fc` 获得单个应用数据库的一致快照，恢复使用 `pg_restore`。应用角色表随数据库保存，数据库集群角色和部署环境由运维管理。客户端不能低于来源服务器主版本；默认 Docker 以 PostgreSQL 16 工具配套验证。[pg_dump 16](https://www.postgresql.org/docs/16/app-pgdump.html)
- PostgreSQL 官方 Apt 仓库提供指定主版本客户端；Docker 安装 16 版，不依赖发行版默认主版本。[Debian 安装说明](https://www.postgresql.org/download/linux/debian/)
- SQLite 采用标准库 `Connection.backup`，在独立连接中生成一致快照；不复制正在写入的数据库主文件，也不漏掉 WAL 中已提交事务。[Python 3.12 sqlite3](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup)
- 文件加密采用 GnuPG 对称 AES-256，生成标准 `.gpg` 文件。密码通过专用 stdin/文件描述符传递，批处理使用 loopback，并禁用对称密码缓存；密码不能放入 argv、环境变量或落地密码文件。密码校验拒绝换行，符合该接口只读取一行的约束。[操作模式](https://www.gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html)、[密码输入与缓存选项](https://www.gnupg.org/documentation/manuals/gnupg/GPG-Esoteric-Options.html)
- S3 使用官方 Boto3 管理式分段传输，固定区域、endpoint、path-style、显式凭据和有界超时/重试。通过 TransferConfig 限制并发和缓冲；不自己实现签名或一次性读取整份备份。[文件传输](https://docs.aws.amazon.com/boto3/latest/guide/s3.html)、[客户端配置](https://docs.aws.amazon.com/botocore/latest/reference/config.html)
- SMTP 使用标准库 `smtplib`，仅支持 SSL 或 STARTTLS，校验证书与主机名，TLS 建立后才认证。[Python 3.12 smtplib](https://docs.python.org/3.12/library/smtplib.html)
- 邮件地址采用 Pydantic EmailStr 及其 email-validator 依赖，不在配置保存时执行邮箱可达性查询。[EmailStr](https://docs.pydantic.dev/latest/api/networks/#pydantic.networks.EmailStr)

以上结论经过 Context7 与官方页面核对；锁定具体依赖版本和容器构建证据在实施阶段记录。

## 安全与生命周期重点

1. 系统配置、执行记录、产物三类事实分开；快照按执行冻结，关闭存储或改变加密开关只影响之后受理的执行。
2. 已接受的手动执行和下载授权不代表备份完成或文件已送达；审计与界面必须标明“已受理/已授权下载”。
3. 新备份仅清理各已启用目的地中属于本实例的过量成功产物；不扫描清空整个 bucket，不删除未知文件，不把失败产物计入成功保留份数。
4. 保留旧产物定位和完成访问所需的加密存储资料；相同目的地凭据轮换优先使用最新有效凭据，改变 bucket/endpoint 不把旧记录指向新位置。无有效权限时展示不可访问并记录清理失败，不假装已删除。
5. 外部请求每次发送前检查协议、固定 origin 和所有解析地址；默认禁私网，部署可显式许可私网 S3/SMTP，云元数据/链路本地地址仍禁止。沿用现有 DNS 检查与连接之间存在窗口的事实，以部署出站规则兜底，不宣称实现了 DNS 地址绑定。
6. 原始数据库快照和加密中间文件仅在应用私有临时目录，0700 目录/0600 文件；失败、中断及启动恢复清理只操作经所有权核对的实例目录。
7. 大文件、慢网络和中断时保持实际执行租约，等待底层 I/O 确实结束后释放；不能取消等待线程后就宣称执行已结束。
8. 成功邮件投递的准确含义为 SMTP 服务已受理。发送超时或崩溃不能承诺收件人未收到；未知状态独立显示，不盲目重发。

## 验收环境现状

本机已探测到 uv、npm 和 Docker，Docker daemon 版本 29.8.2；本机没有 pg_dump、pg_restore、gpg、gpgconf。真实 PostgreSQL、GPG、S3 与 TLS SMTP 验收安排在本任务专用的隔离 Docker 环境，不修改宿主机工具或接触真实生产数据。

实现阶段必须提供真实备份/解密/恢复、S3 传输与邮件捕获证据。当前仅完成调研，不声称实现或运行验收已通过。

## 上下文注入整理

自动注入单文件上限32768字节。数据库、错误、组件与类型规范原文件超过上限，已按本任务相关完整段落整理为 research/spec-*.md，附原文件、行号与 SHA-256；JSONL 引用摘录以避免截断。原规范仍是权威来源，源文件变更后须按来源重读并刷新摘录，不修改 Trellis 注入上限。
