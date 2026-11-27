# 媒体文件存储

## 1. 适用范围

短剧等功能产生的库外图片、视频、PCM WAV 参考音频及图片缩略图、视频海报使用媒体服务。业务记录保存对象 UUID。作品封面、设定缩略图和小说模板图继续走各自现有的数据库接口。

## 2. 写入与读取

`app.media.service.store(db, *, owner_id, scope, kind, mime, source, max_bytes, project_id=None, plugin_id=None, origin="upload")` 接收异步字节流。作品媒体使用 `scope="project"` 并传所属作品，个人媒体使用 `scope="user"`。调用方先检查业务写权限，给媒体服务提供独立会话。服务会提交配额预留与文件发布。原件和图片缩略图均计入占用。服务用短事务锁定用户及设置行，确认预留后释放锁，再写文件。独立会话成功返回后，如果调用方的业务记录保存失败，应调用 `mark_deleted` 标记本次产生的对象。

`store_derived(db, parent, variant, mime, data)` 支持视频的 `poster`。图片写入时自动生成最长边 400 像素的 WebP `thumb`。需要独立归属的副本时使用 `copy`，让新对象在当前生效位置获得独立存储键。

`GET /api/v1/media/{object_id}` 接受 `variant=thumb|poster` 和 `download=1`。普通读取检查当前登录态、个人归属或作品的 `content.read`，以及对象所属插件的启用状态。派生 UUID 也受原件约束。响应发送 `private, no-store`，后续请求重新检查权限。

## 3. 管理、部署与备份

`GET /api/v1/admin/media-storage` 返回位置列表、空间占用、卷报告和设置修订号。`POST /test` 在此前缀下测试读写删。`PATCH` 提交当前 `revision`，可带 `default_quota_bytes` 和 `location`。保存新位置需要探针成功，旧位置的已有文件按原地址读取。S3 密钥加密保存。相同端点省略密钥可沿用，切换端点须重填。

默认本地卷使用 `MEDIA_DIR`，默认每用户 5 GiB。迁移 `0081` 包含存储位置、单行设置、文件元数据、卷报告四表。本地文件记录写入卷的标识。簇内各实例读写同一目录（含 `.volume-id`）或同一对象桶。单书备份排除 `media_objects`；整库恢复需要同一时间点的媒体文件和数据库。

`signing.external_url(db, obj, *, expires=3600)` 签发单文件外链，有效期 1 秒至 7 天。本地模式使用 `PUBLIC_BASE_URL` 和带到期时间的 HMAC，缺少公网地址时返回 `None`。S3 使用预签名 GET，签发后由对象存储校验到期时间；应用关闭插件不会提前撤销已签发的 S3 链接。业务调用方在签发前核实使用者能访问该素材。

## 4. 验证与错误

| 情况 | 可观察结果 |
| --- | --- |
| 未登录、无作品访问权、插件关闭、对象未就绪 | 普通读取返回 404 |
| 有效单段字节请求 | 206，带 Content-Range、Content-Length、Accept-Ranges |
| 不可满足的字节范围 | 416，Content-Range 给出对象总长度 |
| 本地卷标识与写入卷不符 | 503，错误码 media_unavailable_on_instance |
| 配额不足 | 拒绝写入，错误码 media_quota_exceeded |
| 管理员以旧修订号保存 | 409，重新读取设置后处理草稿 |
| 签名错误或已过期 | 本地外链返回 404 |

## 5. 文件生命周期

作品进回收站时保留原件和派生文件。业务删除在业务事务内标记，提交后可立即触发清理。定期清理还检查过期的 pending 预留、用户已删除的文件、所属作品已不存在的作品媒体。清理者在短事务中领取十分钟租约，在事务外删除底层文件，再核对领取令牌并删除元数据。底层删除失败时保留记录供重试。卷不匹配的进程不能替原卷确认删除成功。

## 6. 验证入口

后端的 `tests/test_media_backends.py` 共用本地与 S3 契约，覆盖整读、范围读取、复制、超限中止和幂等删除。`test_media_service.py`、`test_media_cleanup.py` 检查元数据与实际文件。`test_media_api.py` 检查权限、外链及范围响应。`test_media_admin_api.py` 验证设置切换、密钥投影、并发修订与审计。`test_media_storage_migration.py` 检查迁移与整库恢复。

## 7. 业务接入示例

已有文件须按其 `location_id` 读取。若按全站当前设置拼接磁盘路径，管理员切换到 S3 后，原本在磁盘上的文件便无法打开。读取既有文件时使用媒体 UUID，界面使用 `MediaImage` 或 `MediaVideo`；存回一份独立素材时使用 `service.copy`。
