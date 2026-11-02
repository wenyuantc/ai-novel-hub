# 真实隔离验收证据

2026-11-02，最终主验收命令退出0，`all.json` 中32项检查全部 `passed`，`resource_cleanup=passed`。
开始/结束：UTC 04:31:23–04:37:48（北京时间12:31:23–12:37:48）。

```sh
cd backend
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run python tests/backup_acceptance.py \
  --scenario all \
  --image sha256:af56b9a43d74cbcb8a4a0b38954d07c9258676bc6ce53bd6fa0ba5218cdf997c \
  --output ../.trellis/tasks/10-30-admin-backup-management/research/acceptance
```

本轮固定应用 image ID 为上述 `af56…997c`，不以之后同名 tag 的内容冒充已验镜像。
PG工具真实版本为16.15，GPG/GPGCONF为2.2.40。PG16与RustFS官方镜像摘要及完整工具版本见 `all.json`。

- PostgreSQL和文件SQLite各执行明文、GPG、密码轮换后GPG，共6次真实下载/解密/恢复。每次恢复到另一个
  隔离数据库，PG比较15张业务表，SQLite另含真实WAL提交标记共16张；账号、角色、平台设置、作品、封面、
  章节与引用指纹一致，schema为0030，原主密钥能解密原模型凭据及备份配置凭据。恢复目标使用独立备份目录，
  真实启动应用、用原owner密码登录并通过HTTP读取作品/章节/原配置，再停机。错误密码和损坏GPG拒绝，旧文件
  不因新密码或关闭加密而改用新格式。
- 两种数据库均生成超过8MiB的真实产物，RustFS TLS服务的真实对象ETag证明multipart。仅本地、仅S3、双存储
  及本地1份/S3两份独立保留通过；实际版本数2、multipart版本数2、delete marker为0。改变目的地/清除当前
  凭据后旧位置与加密访问快照仍可下载。替换本地inode后清理失败独立可见、原无关文件不被删除并真实发信；
  恢复原inode后精确重核清理，旧下载HEAD返回410。
- SMTP使用本次测试CA/主机名校验、真实TLS和AUTH。部分/全部备份失败真实捕获通知；连接失败不改写备份结果；
  正文已捕获而250确认丢失记unknown且不盲重发。邮件无秘密或备份附件。
- 直接HTTP验证未登录/普通/四项分项权限、同会话撤权、非超管配置/执行/下载、跨actor request_id冲突、默认
  北京03:00/各7份/加密开关、CAS与非法秘密不回显。所有备份API和鉴权下载有no-store，下载长度/校验和一致。
- 并发同一意图只产生一个执行，异意图真实忙碌，配置/幂等重启保留。真实杀死已受理应用进程后中断记录、执行
  租约、已知私有明文和独占GPG home恢复回收。离线多日只补最近计划槽，重启不重复；关闭定时仍可手动。
  终态清理失败夹具重启后只回收对应run的私有明文，保留未知身份文件和原失败历史。
- 直接数据库探针证明配置与产物访问凭据均为可用原主密钥解密的密文，终态执行快照不保留不再需要的秘密；
  所有本次容器stdout/stderr均不含合成秘密。退出后的归属标签容器/网络/卷只读审计均为空。

计划到期和终态清理故障使用应用停机后的隔离持久状态夹具，不声称真实等待三天或制造磁盘硬件故障。
未知数据库提交、阻塞线程排空与其他可控边界由后端对应回归测试证明；浏览器由主会话独立验证。
本目录没有生产数据、真实邮件投递、密码、CA私钥、原始数据库或备份二进制。

`roundtrip.json`（614镜像）记录此前新增恢复应用启动闭环的独立成功；`storage.json`、`smtp.json` 和
`history.jsonl` 是对应早期/定向轮次，完整最终结论以本次 `all.json` 为准。此前GPG长home和Docker随机端口
变动导致的失败未被说成通过，修复后才完成本轮。后续若仅改OpenAPI operationId，须另记录小范围smoke及新镜像，
不改写本轮真实验证的image ID。

本轮驱动源文件SHA-256：

| 路径（相对backend） | SHA-256 |
| --- | --- |
| tests/backup_acceptance.py | 97bac7a43783187bf8fdff9887f8fd8229bd5a5b89263d90cc5f7c4dcab5a40a |
| tests/backup_acceptance/__init__.py | 6b2bc091dab79b7f3bb76215cda86c3d79c734913bc975d20defc83ccaddbc52 |
| tests/backup_acceptance/infra.py | f787e1e843c9e3e08d5df93d5502168fc3114cec53ca21024f77b10bfa0b306f |
| tests/backup_acceptance/probe.py | 7173dc5d1faa3a71dfbd101650689c023443dace6f6b777deb0030bae04d1fe9 |
| tests/backup_acceptance/smtp_capture.py | 781d71d11d0347188066ecea62b9378c3fcf49016d292c9f6f6cadec37c4d615 |
