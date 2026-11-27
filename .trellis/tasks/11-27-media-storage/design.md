# 媒体文件存储：技术设计

> 需求见同目录 `prd.md`（M1–M9）；决定编号见父任务 `prd.md`（D13、D17、D18）。调研依据：
> `11-27-short-drama-plugin/research/reference-media-infra-and-gap.md` B.7、B.10、C。

## 1. 设计原则

1. **文件在库外，账在库内。** 每个文件对应一行元数据；业务表只引用元数据行的 id，不直接拼路径。
2. **读文件必须过鉴权。** 不提供静态目录；浏览器经带登录态的接口读取（M2）。
3. **切换存储不搬家。** 每个文件记住自己在哪个存储位置；切换只影响新文件的去向，旧位置保留为只读（D17）。
4. **删除两段式。** 业务事务里只标记，文件在事务提交后删除，失败由清理任务兜底（M6）。
5. **核心能力不认识插件。** 存储层只认「归属用户、所属作品、可选的插件标识」，不认识剧或分镜。

## 2. 结构

```
backend/app/media/
  backends.py     # Backend 协议；LocalBackend、S3Backend
  locations.py    # 存储位置的读取、切换、连接测试
  service.py      # 写入、读取、复制、标记删除、配额、清理
  access.py       # 读写鉴权
  signing.py      # 外部访问地址（M4）
backend/app/models/media.py
backend/app/schemas/media.py
backend/app/api/v1/media.py                 # GET /media/{id}（含分段读取）
backend/app/api/v1/admin_media_storage.py   # 管理页接口
backend/alembic/versions/0081_media_storage.py   # 编号以实施时的链尾为准

frontend/src/api/adminMediaStorage.ts
frontend/src/pages/AdminMediaStoragePage.tsx
frontend/src/components/media/{MediaImage,MediaVideo}.tsx   # 统一的图片 / 视频展示组件
```

## 3. 数据模型

### 3.1 `media_storage_locations`（存储位置）

| 列 | 说明 |
| --- | --- |
| `id` | 自增主键 |
| `kind` | `local` / `s3` |
| `configuration` | JSON，非敏感项：本地为空（根目录由环境变量 `MEDIA_DIR` 决定）；S3 为 endpoint、region、bucket、prefix、path_style |
| `secrets_enc` | JSON，敏感项经 `SecretCipher` 加密（access key、secret key）；接口只回显是否已设置 |
| `state` | `active`（新文件写入处，全表至多一行）/ `readonly`（仍可读、可删）/ `retired`（已无文件） |
| `created_at`、`created_by` | |

首次迁移写入一行 `kind=local, state=active`，因此单机部署零配置（M1）。

### 3.2 `media_storage_settings`（单行）

`id=1`、`revision`、`default_quota_bytes`（默认 5 GiB）、`last_operation_id`、`updated_at`。做法与 `backup_settings`（`backend/app/models/backup.py:22`）一致。

### 3.3 `media_objects`（文件元数据）

| 列 | 说明 |
| --- | --- |
| `id` | `String(36)` UUID 主键，也是对外地址的一部分 |
| `location_id` | 外键 → 存储位置，`RESTRICT` |
| `storage_key` | 位置内的相对键：`<两位哈希前缀>/<id><扩展名>`，不含用户输入 |
| `owner_user_id` | 外键 → users，`SET NULL`；为空即待清理 |
| `project_id` | 可空外键 → projects，`SET NULL` |
| `scope` | `project` / `user`：决定鉴权方式；`scope=project` 而 `project_id` 为空即孤儿 |
| `plugin_id` | 可空；非空时读取还须该插件已启用（父任务 D5） |
| `kind` | `image` / `video` |
| `mime`、`size_bytes`、`sha256` | |
| `width`、`height`、`duration_ms` | 可空 |
| `parent_id`、`variant` | 派生文件（`thumb` 缩略图、`poster` 海报帧）指向原文件，随原文件删除 |
| `status` | `pending`（已占位、内容未写完）/ `ready` / `deleting` |
| `created_at`、`deleted_at` | |

索引：`(owner_user_id, status)`（配额统计）、`(project_id)`、`(status, created_at)`（清理扫描）、`(parent_id, variant)` 唯一。

## 4. 后端接口

### 4.1 存储后端协议

```python
class Backend(Protocol):
    async def put(self, key: str, source: AsyncIterator[bytes], *, size_hint: int | None) -> int
    async def open(self, key: str, *, start: int | None, end: int | None) -> AsyncIterator[bytes]
    async def stat(self, key: str) -> int | None        # 字节数；不存在返回 None
    async def delete(self, key: str) -> None             # 幂等
    async def copy(self, src: str, dst: str) -> None
    async def presign_get(self, key: str, *, expires: int) -> str | None   # 不支持时返回 None
    async def check(self) -> None                         # 连接测试：写、读、删一个探针对象
```

- `LocalBackend`：根目录 `MEDIA_DIR`（默认 `./media`）；写入先落临时文件再原子改名；键经规范化后必须仍在根目录内。
- `S3Backend`：复用整库备份已有的 `boto3` 用法与网络策略（`backend/app/services/backups/storage.py:347`、`backups/network.py`）；
  阻塞调用放线程池；endpoint 走与备份相同的内网地址防护。
- 不抽出与备份共用的基类：备份存储绑定执行键与实例身份（调研 B.10），语义不同，共用只会互相牵制。

### 4.2 服务层

```python
async def store(db, *, owner_id, project_id, scope, plugin_id, kind, mime, source, max_bytes) -> MediaObject
async def store_derived(db, parent: MediaObject, variant: str, mime, data: bytes) -> MediaObject
async def open_range(obj: MediaObject, start, end) -> AsyncIterator[bytes]
async def copy(db, obj: MediaObject, *, owner_id, project_id, scope, plugin_id) -> MediaObject   # 画布「存回」用
def mark_deleted(db, obj_ids: Iterable[str]) -> None           # 只改状态，随调用方事务提交
async def mark_project_deleted(db, project_id: int) -> None    # 作品彻底删除时调用
async def usage_of(db, user_id: int) -> int
async def sweep(sessionmaker, *, now) -> SweepReport           # 清理任务
```

写入流程：校验配额 → 插入 `pending` 行并提交 → 流式写后端（边写边算大小与 sha256，超过 `max_bytes` 即中止并删除）→
图片用 Pillow 读尺寸并生成 `thumb`（最长边 400、WebP）→ 置 `ready`。内容类型按文件头判定，不信扩展名与客户端声明
（沿用 `backend/app/ai/images.py:8` 的做法，视频另加容器头识别）。

配额：`usage_of` 统计该用户 `pending + ready` 的 `size_bytes` 之和；超出 `default_quota_bytes` 时 `store` 拒绝，
错误码 `media_quota_exceeded`。并发写入可能短暂超额一个文件，接受。

### 4.3 删除与清理（M6）

- 业务删除：业务事务内 `mark_deleted`（置 `deleting`、写 `deleted_at`）。提交后由调用方触发一次即时清理；失败不影响业务结果。
- 作品进回收站：不动媒体。彻底删除：`recycle_bin_service.purge`（`backend/app/services/recycle_bin_service.py:464`）在删除作品前调用
  `mark_project_deleted`。外键 `SET NULL` 是兜底：任何绕过该调用的删除都会产生孤儿行，由清理任务回收。
- 清理任务 `sweep`（应用生命周期内的周期任务，默认每 10 分钟，多实例用数据库行锁避免重复处理）回收四类：
  `deleting` 行；超过 24 小时的 `pending` 行；`scope=project` 且 `project_id` 为空的孤儿；`owner_user_id` 为空的行。
  处理顺序：先删派生文件，再删后端文件，最后删行；后端删除失败则保留行、下轮重试。
- 位置在其下再无任何行时由清理任务置 `retired`。

### 4.4 读取接口

`GET /api/v1/media/{object_id}`，可带 `?variant=thumb|poster`。

鉴权（`access.py`），任一不满足一律 404，不区分「不存在」与「无权」：

1. 已登录。
2. `status == ready`。
3. `scope=project`：对 `project_id` 具备 `content.read`（`backend/app/services/project_access.py:17`）；`scope=user`：本人。
4. `plugin_id` 非空时该插件已启用（`app.plugins.state.is_enabled`）。

响应：支持 `Range`，返回 `206` 与 `Content-Range`、`Accept-Ranges: bytes`（M3）；`Cache-Control: private, max-age=31536000, immutable`
（对象内容不可变，id 即版本）；`Content-Disposition` 默认 inline，带 `?download=1` 时为 attachment。
S3 位置采用服务端流式转发而不是跳转到预签名地址：鉴权与停用语义保持在应用内，代价是流量经过应用。

本地文件在本实例不存在但行为 `ready`（双实例未共享卷）：返回 503，错误码 `media_unavailable_on_instance`，并记一条告警日志（M7）。

### 4.5 外部访问地址（M4）

`signing.external_url(obj, *, expires=3600) -> str | None`：

- S3：后端预签名 GET。
- 本地：`{PUBLIC_BASE_URL}/api/v1/media/external/{token}`，`token` 为对象 id + 过期时间的 HMAC 签名；该路径不需要登录，只校验签名与过期。
  未配置 `PUBLIC_BASE_URL` 时返回 `None`，由调用方（子任务 3）改用内联方式或给出明确错误。
- 新增环境变量 `PUBLIC_BASE_URL`（当前配置中没有类似项，`backend/app/core/config.py`）。

### 4.6 管理接口与页面（D17）

权限目录新增 `media_storage.read`、`media_storage.configure`（后者 `dangerous`、依赖前者），分组「媒体存储」。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/admin/media-storage` | read | 当前生效位置、历史位置（各自文件数与字节数）、默认配额、全站占用、各实例的卷诊断 |
| POST | `/admin/media-storage/test` | configure | 用提交的（或已保存的）配置做连接测试，不保存 |
| PATCH | `/admin/media-storage` | configure | `{revision, default_quota_bytes?, location?}`；带 `location` 时新建位置并设为生效，原生效位置转只读 |

- 写入沿用系统设置的管理写约定：授权写屏障 → 修订号条件写 → 同事务审计（动作 `media_storage.update`，摘要不含密钥）。
- 保存新位置前强制做一次连接测试，失败则不保存。
- 密钥只写不读：响应里只有 `has_access_key: true`；请求里省略密钥字段表示沿用。
- 页面 `admin/media-storage`：表单、测试按钮、切换确认（文案写明「新文件写入新位置，旧文件留在原位置并保持可读」）、占用统计、卷诊断。

### 4.7 双实例诊断（M7）

本地位置在根目录维护一个 `.volume-id` 文件（不存在则生成 UUID）。每个应用进程启动及每次清理周期把自己读到的值写入
`media_volume_reports(worker_key, volume_id, seen_at)`。管理页比较存活进程上报的值：不一致即提示「各实例的媒体目录不是同一个共享卷」。

## 5. 前端

- `MediaImage`、`MediaVideo`：接收对象 id，拼出 `/api/v1/media/{id}`（同源、带 Cookie）；视频用原生 `<video controls preload="metadata">`
  并以 `poster` 派生文件作封面。项目里此前没有音视频播放（调研 B.10），这两个组件是唯一入口。
- 收到 `media_unavailable_on_instance` 时显示「文件暂不可用」而不是破图。

## 6. 与备份的边界（D18）

- 三张新表不属于任何作品的导出范围：`media_objects` 登记到作品备份注册表的 `NOT_EXPORTED`（它有 `project_id`），理由写「媒体文件不随单书备份迁移」；
  另两张与作品无关，做法同 `system_settings`。
- 整库备份的表目录加入三张表（元数据随库走）；文件本身不在备份内。恢复整库后，文件缺失的对象读取返回 404，由清理任务不处理（行仍 `ready`）。
  README 写明需单独备份 `MEDIA_DIR` 或对象存储桶，并且两者要取同一时间点。

## 7. 兼容

- 现有数据库内图片（封面、设定缩略图、模板图）不迁移、不改接口（M8）。
- Compose 文件增加媒体卷：`docker-compose.yml` 挂 `${MEDIA_DIR:-./media}:/app/media`；`docker-compose.multi.yml` 两个应用服务挂同一个卷。

## 8. 权衡记录

| 问题 | 选择 | 放弃的方案与原因 |
| --- | --- | --- |
| S3 读取 | 服务端流式转发 | 跳转预签名地址：省流量，但绕过应用内的鉴权与插件停用判定 |
| 切换存储 | 多位置并存，旧位置只读 | 迁移旧文件到新位置：需要后台迁移任务与中断恢复，本期不做 |
| 配额粒度 | 全站统一默认值 | 并入现有逐用户配额体系：要改配额页与其契约，等有需求再做 |
| 引用计数 | 不做，一个对象只属于一个业务行；需要共用时复制 | 引用计数：删除语义复杂，容易泄漏或误删 |
| 与备份存储共用代码 | 不共用 | 见 4.1 |

## 9. 规范与文档

- 新增 `.trellis/spec/backend/media-storage.md`，登记到后端索引；前端在 `component-guidelines.md` 增补媒体组件用法。
- 更新 `rbac-quotas.md`（权限数量）、`admin-backups.md`（表目录与文件边界）、`recycle-bin.md`（彻底删除的媒体处理）、`project-transfer.md`（`NOT_EXPORTED`）。
- README：部署章节增加媒体存储（本地卷、对象存储、双实例、备份边界、`MEDIA_DIR`、`PUBLIC_BASE_URL`）。
