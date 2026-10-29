# 实例内提示词工坊

> 来源任务：`10-26-prompt-workshop`；模板与风格落点见 [prompts-styles.md](./prompts-styles.md)。

## 1. 范围 / 触发条件

修改实例内共享、审核、点赞或导入时适用。工坊是登录用户级资源，与项目无关，不登记项目备份注册表；不调用中央云端。
实现位于 `app/models/workshop.py`、`app/schemas/workshop.py`、`app/services/workshop_service.py`、`app/api/v1/workshop.py`。

## 2. 签名

所有 API 的共同前缀为 `/api/v1/workshop`，身份来自本实例认证依赖。

| 路径 | 方法 | 行为 |
|---|---|---|
| `/items` | GET | 浏览、筛选、排序、分页 |
| `/items/{id}` | GET | 详情与当前用户的点赞、导入状态 |
| `/mine/items` | GET | 我的条目，包含下架条目 |
| `/submissions` | GET / POST | 我的提交历史 / 提交新条目或更新 |
| `/submissions/{id}` | GET | 作者或管理员读取提交快照 |
| `/submissions/{id}/withdraw` | POST | 撤回待审核提交 |
| `/items/{id}/hide`、`/items/{id}/unhide` | POST | 下架 / 恢复上架 |
| `/items/{id}/like` | POST / DELETE | 幂等点赞 / 取消点赞 |
| `/items/{id}/import` | POST | 导入到当前用户 |
| `/admin/submissions` | GET | 管理员审核列表 |
| `/admin/submissions/{id}/approve`、`/reject` | POST | 通过 / 驳回 |
| `/admin/items` | POST | 管理员直接发布官方条目 |
| `/admin/items/{id}` | PATCH / DELETE | 仅改分类、标签 / 删除 |

迁移 `0020_prompt_workshop` 新增四张表：

- `workshop_items`：最新审核通过内容；`author_id → users SET NULL`。
- `workshop_submissions`：提交快照与审核记录；`submitter_id → users CASCADE`、`item_id → workshop_items CASCADE`、审核人外键 SET NULL。
- `workshop_likes`、`workshop_imports`：`(item_id, user_id)` 复合主键，外键 CASCADE。
- `uq_workshop_pending_item`：`item_id` 在 `status='pending'` 时唯一，SQLite / PostgreSQL 都声明条件。

## 3. 请求 / 响应契约

提交为 `{kind, item_id?, prompt_key?, name, description, category, tags, content, is_anonymous}`。
`kind` 为 `style | prompt`；模板按单个 key 分享，正文为 `{system, user}`；风格正文复用风格字段规则。

- 名称非空、≤100 字，说明 ≤500 字；每用户最多 20 个待审核提交，请求体整体 ≤1 MiB。
- 分类：`general | xuanhuan | wuxia | romance | scifi | suspense | history | urban | other`。
- 标签最多 5 个，每个 trim 后 1–12 字并去重；禁止引号、反斜线和换行，筛选时仍需转义 SQL LIKE 通配符。
- 浏览参数：`kind/category/tag/prompt_key/q/sort/page/page_size`；`sort=newest|likes|downloads`，`page_size≤50`；分页统一 `{items, total}`。
- 条目响应不提供真实作者 id；`is_mine` 表示归属。`author_name` 匿名时对其他普通用户为 null；管理员另得 `author_username`，作者本人可见自己署名；账号删除后显示「已注销用户」。
- 管理员改元数据只接受 `category/tags`；驳回请求为 `{note}`，trim 后非空、≤500 字。

审核单状态：`pending | approved | rejected | cancelled`；已发布条目状态：`active | hidden`，`hidden_by=author|admin|null`。
更新只写提交快照，审核通过前旧内容继续在架。通过时替换正文、名称、说明、分类、标签和匿名选择，保留计数及原下架状态。
管理员提交自动通过，新条目标为官方；管理员不能通过审核接口改别人的正文。

导入风格每次都生成当前用户的新副本，重名通过现有命名工具处理。
导入模板先按当前 `TEMPLATES[key]` 重新校验，失败时用户覆盖和导入记录均不改变。
成功时保留导入前当前生效内容的版本；无覆盖或覆盖停用时，此内容为系统默认，停用覆盖自身正文也须保留。
导入后启用覆盖，每次导入都记录带「从工坊导入」备注的版本，即使正文相同也不能省略该版本。

点赞记录增删与 `like_count` 原子更新在同一事务；首次导入记录插入与 `download_count + 1` 同事务。
重复导入仍执行复制或覆盖，计数只按用户加一次。删号级联清理行为记录，但历史计数不回退。

并发锁顺序：提交 / 导入先锁当前用户再锁已有条目；审核已有条目更新时先锁条目再执行提交状态迁移。
删除先锁条目再级联删除提交，所以审核不能反过来先锁提交、再等条目，否则 PostgreSQL 上会形成锁顺序反转。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 未登录 | 401 |
| 普通用户调用管理员接口 | 403 |
| 非管理员读取他人的提交，或他人下架条目 | 404 |
| 对别人的条目提交更新、下架 | 404 |
| 作者恢复管理员下架条目 | 403 |
| 同条目已有 pending 更新，或重复审核 / 撤回已终结提交 | 409 |
| 内置预设风格原样提交，或与系统默认完全相同的模板 | 400 |
| 模板校验失败或 key 已不存在 | 400；校验失败响应保留错误列表 |
| 非法枚举、空驳回原因、schema 元数据字段超长 | 422 |
| 风格 content 字段或模板段超长 | 400（服务层与模板校验） |
| 超过请求体限制 | 400（沿用 `UploadLimitMiddleware` 的上传限制响应） |

## 5. Good / Base / Bad 用例

- Good：A 提交 → 管理员通过 → B 点赞和导入；A 更新待审核时 B 仍读旧内容，通过后内容更新且计数保留。
- Base：B 重复导入风格得到两条副本，下载数仅增加一次；重复点赞仍只有一条记录。
- Bad：先覆盖模板再校验，校验报错后旧内容已经丢失；或把匿名作者 id 放进公共响应。

## 6. 必需的测试

`tests/test_workshop.py` 覆盖跨用户 404、普通用户管理接口 403、匿名不泄露作者、删除作者后保留条目与计数、提交级联删除。
同时覆盖审核旧版保留与 409、管理员官方标记、分类标签调整、下架归属与恢复、中文 / 通配符标签筛选与分页。
点赞 / 取消点赞必须检查记录与计数；重复和并发调用不得重复计数。
导入测试检查独立风格副本、模板启用、当前生效内容可恢复、同文导入版本、模板升级校验失败无写入。
迁移测试检查四张表、外键动作、部分唯一索引及 downgrade 不影响原用户数据。

## 7. 错误写法 vs 正确写法

```python
# 错误：每次请求都加计数，重试会重复累计。
item.download_count += 1

# 正确：只在首次插入用户导入记录时更新，且与导入写入同一事务。
if inserted:
    await db.execute(update(WorkshopItem).where(WorkshopItem.id == item_id).values(
        download_count=WorkshopItem.download_count + 1,
    ))
```

禁止为了分享模板绕过现有沙箱校验，禁止在前端拼装真实作者署名，禁止在审核通过前覆盖已发布正文。
