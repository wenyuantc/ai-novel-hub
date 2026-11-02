# 真实隔离备份验收

先从仓库根构建本任务运行镜像，再从 `backend/` 执行：

```sh
docker build -t ainovel-backup-acceptance:local .
cd backend
uv run python tests/backup_acceptance.py --scenario all \
  --output ../.trellis/tasks/10-30-admin-backup-management/research/acceptance
```

主机只需要 Docker 和项目 `uv` 环境。PostgreSQL 16 客户端、GPG、GPGCONF 均使用本次应用镜像的真实工具。
每次启动先冻结应用 image ID，即使其他会话同时重建同名 tag，本次全部容器仍使用同一镜像。
可用 `--image` 指定另一个已构建镜像；缺少镜像或服务失败直接以非零退出，不以 skip 代替。

`--scenario` 支持 `all`、`roundtrip`、`storage`、`smtp`、`reliability`。所有选择都会先执行真实 HTTP
权限矩阵、配置默认值/CAS/秘密省略与非法秘密不回显、非超管执行和下载、跨 actor 请求身份冲突。

- `roundtrip`：PG16/SQLite 各执行明文、GPG 加密及密码轮换；鉴权流式下载，先完整解密并核对退出码，
  再恢复到另一个数据库。比较账号、角色、平台设置、作品、封面、引用和凭据密文指纹，验证原 Fernet
  密钥能解密。恢复目标使用独立备份目录，恢复完成后启动应用、用原 owner 登录并通过 HTTP 读取作品、
  章节和模型配置，然后停机。错误密码/损坏加密文件须拒绝，旧产物仍只认原密码和原格式。
- `storage`：仅本地（公共前置）、仅 S3、两者；合成章节含约12MiB不可压缩内容，实际文件超过8MiB，
  服务端 ETag 证明发生真实 multipart。比较本地1份/S3两份，检查真实对象版本精确删除且无 delete marker。
  变更 bucket/清除当前凭据后仍可访问旧位置及加密访问快照。替换本地文件 inode 模拟清理异常：新备份仍
  成功，清理独立失败并真实发信，无关文件不得删除；恢复原 inode 后精确重核，过期副本返回410。
- `smtp`：在本次 Docker 网络内使用自建测试 CA、主机名校验和真实 TLS/SMTP AUTH 捕获器，绝无外部投递。
  验证部分/全部备份失败、连接失败独立状态；模拟正文已受理而250确认丢失，要求 unknown 且不盲重发。
- `reliability`：并发同一 request_id、异意图忙碌、重启后幂等和配置保留；真实杀死已受理执行进程，重启
  后中断状态、执行租约和已知私有明文/GPG home 回收；离线多日只补最近槽、北京时间及重启去重、关闭定时
  仍可手动。离线夹具另外模拟终态清理失败，要求仅回收对应执行的私有明文、保留未知身份文件和原失败历史。

计划到期和终态清理失败通过**应用停机后的隔离夹具**注入持久状态；实际调度、恢复及文件操作仍经过应用启动。
这两项不冒充真实经过三天，也不声称通过 Docker 复现了磁盘硬件故障。未知数据库提交、线程排空等可控屏障
由相应后端回归测试证明；本入口不替代这些单测，也不替代主会话的真实浏览器验收。

资源均使用随机名称与归属标签，专属 bridge 网络；只有应用端口绑定宿主 `127.0.0.1`，其余服务不发布。
Docker Desktop 的随机宿主端口会在重启后变化，驱动每次重新 inspect 并重绑定 HTTP 客户端。
绝不加入已有网络或读写项目现有数据库卷，不读取真实 `.env`。所有凭据随机生成，秘密只在0700临时目录的
0600环境文件或进程 stdin 中传递；CA私钥、数据库和二进制产物均不写入输出目录。退出回收本次拥有的容器/卷/
网络与临时目录；只输出检查名、退出状态、版本、文件校验和及计数，失败也不输出供应商/SQL原始异常。

S3 服务使用[官方 RustFS 容器](https://docs.rustfs.com/en/installation/container/docker)与
[官方 TLS 配置](https://docs.rustfs.com/en/integration/tls-configured)，固定为2026-11-02核对的
`rustfs/rustfs@sha256:ed9210d6411d77ffceb70d94b964a3cb7d225983368bc25bebe01e995293dcc2`。
官方 MinIO DockerHub 镜像已删除且 Quay 匿名拉取401，未换用第三方重打包镜像。
PostgreSQL 使用核对过的官方16镜像 digest，完整值见 `infra.py` 和每轮脱敏结果。

输出 `<scenario>.json` 是该场景最近一轮结果，`history.jsonl` 保留启动记录器后的脱敏历史。
只有 `status=passed`、进程退出0且 `resource_cleanup=passed` 才表示该轮已完成；先前失败不改写为通过。
