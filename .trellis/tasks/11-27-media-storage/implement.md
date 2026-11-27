# 媒体文件存储：实施计划

> 需求 `prd.md`（M1–M9），设计 `design.md`。按顺序执行；每一步先写测试再写实现。
> 中途只跑受影响测试加哨兵，全量门禁留到最后一步。

## 开始前复核

- [x] 迁移链尾：`ls backend/alembic/versions | sort | tail -3`，据此确定本子任务的迁移编号与父修订。
- [x] 权限目录当前数量与所有数量断言的位置：`grep -rn "len(PERMISSION" backend/tests` 及前端 `PermissionCode`。
- [x] 作品彻底删除的实际入口与事务边界：读 `backend/app/services/recycle_bin_service.py::purge` 与 `.trellis/spec/backend/recycle-bin.md`。
- [x] Starlette 版本对 `Range` 的支持情况，决定分段读取用框架能力还是自行实现。

## 步骤

### 1. 存储后端

- [x] `backend/app/media/backends.py`：`Backend` 协议、`LocalBackend`、`S3Backend`。
- [x] 测试 `tests/test_media_backends.py`：两种后端共用同一组契约用例（写、整读、分段读、stat、删除幂等、复制、超限中止不留文件、路径逃逸被拒）。
      S3 用内存假实现或 `botocore` stubber，不连外网。

验证：`cd backend && uv run pytest tests/test_media_backends.py -q`

### 2. 模型与迁移

- [x] `app/models/media.py` 三张表加 `media_volume_reports`；迁移写入默认的本地生效位置与设置行；含降级。
- [x] 迁移往返测试；调整作品备份反射测试（`media_objects` 进 `NOT_EXPORTED`）与整库备份表目录测试的期望。

验证：迁移测试文件加被改动期望的两个既有测试文件。

### 3. 写入、读取与配额

- [x] `app/media/service.py`：`store`、`store_derived`、`open_range`、`copy`、`usage_of`；内容类型按文件头判定；图片生成 `thumb`。
- [x] 测试 `tests/test_media_service.py`：正常写入与派生；伪造扩展名被拒；超过单文件上限；超过配额返回 `media_quota_exceeded`；`pending` 行在失败后不变成 `ready`。

### 4. 鉴权与读取接口

- [x] `app/media/access.py`、`app/api/v1/media.py`（含 `Range`、`variant`、`download`）。
- [x] 测试 `tests/test_media_api.py`：未登录、非归属、查看者可读、协作者按权限、`scope=user` 仅本人、带 `plugin_id` 且插件停用时 404（用子任务 1 的示例插件）、
      `206` 与 `Content-Range` 正确、越界 `416`、缓存头。

### 5. 删除与清理

- [x] `mark_deleted`、`mark_project_deleted`、`sweep`；应用生命周期内注册周期任务；多实例行锁。
- [x] `recycle_bin_service.purge` 调用 `mark_project_deleted`。
- [x] 测试 `tests/test_media_cleanup.py`：业务删除后文件消失；作品彻底删除后其全部文件消失；进回收站与恢复不动文件；四类待清理行都被回收；
      后端删除失败时行保留并在下轮成功；两个清理并发不重复处理。

回滚点：本步改到 `recycle_bin_service.py`，出问题可单独回退。

### 6. 外部访问地址

- [x] `app/media/signing.py`、配置项 `PUBLIC_BASE_URL`、路由 `/media/external/{token}`。
- [x] 测试：有效期内匿名可取；过期、篡改、对象已删除均失败；未配置公网地址时返回 `None`；S3 走预签名。

### 7. 管理接口

- [x] 权限两项；`app/media/locations.py`；`app/api/v1/admin_media_storage.py`；审计动作。
- [x] 测试 `tests/test_media_admin_api.py`：读取投影不含密钥；连接测试成功与失败；切换后新文件进新位置、旧文件仍可读；
      修订过期 409；只读权限改配置 403；连接测试失败时不保存；审计记录不含密钥。
- [x] 卷诊断：`.volume-id` 与 `media_volume_reports`；测试两个进程读到不同值时接口给出不一致标记。

### 8. 前端

- [x] `MediaImage`、`MediaVideo` 组件与测试（地址拼接、不可用提示、封面）。
- [x] `api/adminMediaStorage.ts`、`pages/AdminMediaStoragePage.tsx`、路由、管理导航入口。
- [x] 测试：只读权限下表单禁用；密钥字段留空表示沿用；切换确认文案；409 后重读；离页后旧响应不反写。
- [x] 浏览器走查：切换到本地起的 S3 兼容服务（如 MinIO）再切回；播放一个大于 50MB 的视频并拖动进度；375px 宽度。

### 9. 部署、文档与收口

- [x] `docker-compose.yml`、`docker-compose.multi.yml` 增加媒体卷；`.env.example` 增加 `MEDIA_DIR`、`PUBLIC_BASE_URL`。
- [x] 双实例验证：共享卷下 A 写 B 读；对象存储下 A 写 B 读；未共享卷时 B 返回 `media_unavailable_on_instance` 且管理页给出诊断。
- [x] 规范与 README（见 `design.md` 第 9 节）。
- [x] 确认封面、设定缩略图行为回归通过（M8）。作品备份测试中仅将 app_schema 期望同步到 0081，单书格式仍为 v18。
- [x] 全量门禁：
  - `cd backend && uv run ruff check . && uv run pytest -n 4 --dist loadfile --durations=20`
  - `cd frontend && npm run lint && npm run test && npm run build`

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 删除与写入并发导致文件与行不一致 | 两段式删除 + 清理任务兜底；测试覆盖后端删除失败与并发清理 |
| 彻底删除流程有既有的事务与锁序约束 | 第 5 步先读规范与代码；只在既有事务内加一次标记调用，不新增锁 |
| S3 转发大文件占用应用内存 | 全程流式，分块大小固定；测试断言不整段读入 |
| 切换存储后旧位置密钥被改或失效 | 旧位置配置不可编辑，只能在其无文件后退役；管理页显示各位置的可达性 |
| 配额统计在大量对象下变慢 | 有 `(owner_user_id, status)` 索引；必要时再加汇总表，本期不做 |

## 开始实现前的检查

- [x] 用户已批准本子任务的规划。
- [x] `implement.jsonl` 与 `check.jsonl` 各有至少一条真实的规范条目。
- [x] 工作区干净，或已与用户确认现有改动的归属。
