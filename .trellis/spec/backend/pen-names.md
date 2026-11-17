# 作者私人笔名与作品署名

## 场景：私人笔名库、作品署名与分组

### 1. 范围 / 触发条件

作品可选择作者本人一个笔名；旧作品及未设置时以真实作者用户名展示。修改笔名管理、作品创建／投影、备份、EPUB、智能体项目工具或书架筛选时，必须核对本跨层合同。笔名不是登录身份、协作者身份或审计 actor。

### 2. 签名

```text
GET    /api/v1/pen-names
POST   /api/v1/pen-names                 {name}
PATCH  /api/v1/pen-names/{id}            {name, expected_revision}
DELETE /api/v1/pen-names/{id}?expected_revision=N
PUT    /api/v1/projects/{id}/pen-name    {pen_name_id: number|null}
POST   /api/v1/projects                  {..., pen_name_id?: number|null}
ProjectOut.pen_name = null | {id: number|null, name: string}
```

迁移 `0055` 建 `pen_names` 和 `project_pen_names`；后者 `(project_id, owner_id)`、`(pen_name_id, owner_id)` 分别引用作品及笔名的同作者复合键。作品至多一个关联；SQLite 的笔名 ID 不复用。作品关联为 `project_id` 主键，不把复合 `SET NULL` 加在作品父表上。

### 3. 契约

- 笔名名称 trim 后 1–100 字符，同作者精确大小写敏感唯一；修订号从1开始，改名／删除使用 expected_revision。User 写屏障串行化该作者库操作、绑定和建书恢复。
- 独立署名写入先原 actor 认证 gate／User 屏障，再锁作者活跃作品，校验笔名归作者，最后提交；协作者、管理员代管均不能写他人署名。普通 `ProjectUpdate` 显式拒绝 `pen_name`／`pen_name_id`；其他旧的未知字段兼容语义不改变。
- 新建和备份恢复沿原有 User 额度屏障，重验身份后调用 `bind_without_commit`；内部 helper 不 commit、不反向取全局 gate。不得将创建链改成先持 User 再获取 gate，否则与既有账户操作锁序冲突。
- 人工 ProjectOut 在最终授权投影批量 join 作品当前署名：仅作者获得笔名 ID，其他有权读取作品者只获得名称和 `id=null`。列表不得逐书 N+1；最终授权变化不能用先前查得的笔名填入已撤权的作品。
- 私库列表／管理只针对当前认证用户，不接受任意 owner 参数。改名即时反映已有作品；删除通过关联外键解除所有作品（含回收站）的署名，不删除作品。
- 书架按全部／未设置／本人指定笔名筛选；全部保留协作作品，未设置与具体笔名只筛本人作品。私库和筛选状态按 actor／请求代次失效，不写入跨账号共享偏好。概览只合并笔名结果，不覆盖其他未保存表单。
- 备份自 v10 起的顶层 `pen_name: string|null` 只含本书当前文本，无笔名库 ID；v1–v9 无笔名，旧格式不能夹带非空新字段。恢复在导入者库按精确名称复用／创建并同事务绑定。`project_pen_names` 注册为 `NOT_EXPORTED`；用户级 `pen_names` 不放进单书表注册表。
- 站内和开放 EPUB 的默认作者均使用当前署名，否则真实作者用户名；显式提供的 author（含空串）沿既有语义优先。站内导出弹窗未编辑作者时不得发送当前登录用户名或空串覆盖服务端默认；显式编辑（包括清空）才传对应 author，不能覆盖正在编辑的草稿。私人笔名及 ID 不加入 Agent 项目工具的实时结果或历史重放。
- 有笔名或关联数据时拒绝 `0055` 有损 downgrade；整库备份保留数据。

### 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 无有效登录或原 actor 已失效 | 401 |
| 他人笔名／他人作品署名写入或不存在 | 404 |
| 笔名空白、过长、非法 ID、未知字段或普通 PATCH 夹带笔名 | 422 |
| 本人重复名称 | 409 `pen_name_exists` |
| 旧笔名 revision | 409 `stale_revision` |
| 旧备份夹带非空笔名／备份笔名不合法 | 400，不能部分导入 |

### 5. Good / Base / Bad

- Good：作者为新书选择本人笔名，协作者只在该书看到名称；删除笔名后作品保留且回退账号署名。
- Base：旧作品／旧备份不设置笔名，书架与 EPUB 继续显示作者账号名。
- Bad：凭内容编辑权限枚举作者笔名库、通过普通作品 PATCH 改署名，或把库 ID 加入备份与模型工具输出。

### 6. 必需测试

- HTTP：跨账号、协作者、管理员、无效账号、重名、CAS、通用 PATCH 拒绝、创建和清除绑定；最终投影撤权复验与固定查询数量。
- SQLite／真实 PostgreSQL：0054 → 0055 保留作品、同作者外键、带数据降级保护；两事务覆盖笔名删除与绑定／建书／备份恢复，以及作品回收与绑定的双向竞争。
- 备份和双 EPUB：当前署名、同名复用／新建、无署名旧格式、导入失败回滚、显式作者覆盖；Agent 实际工具结果与历史重放不含该字段。
- 前端：账号切换／延迟响应、本人筛选与协作书、表单草稿、桌面和375px实际操作。

### 7. 错误写法 vs 正确写法

```python
# 错误：普通内容编辑接口接受笔名，或只在 UI 隐藏作者笔名库。
project.pen_name_id = request.pen_name_id

# 正确：owner-only 服务在认证和父行屏障内绑定；建书／恢复复用无提交原语。
await pen_name_service.bind_without_commit(db, owner_id, project.id, pen_name_id)
```
