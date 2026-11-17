# 笔名分组技术设计

## 状态与依据

整体方案已获批准，本子任务已启动。需求以同目录 `prd.md` 为准；代码依据与完整路径见父任务 `research/pen-name-design-evidence.md`。不通过本设计扩大普通内容编辑者的权限。

## 模型与约束

新增 `app/models/pen_names.py`：

| 表 | 列与约束 |
| --- | --- |
| `pen_names` | `id`、`owner_id → users CASCADE`、`name varchar(100)`、`revision >= 1`、时间戳；`UNIQUE(owner_id,name)`、`UNIQUE(id,owner_id)`；SQLite 自增身份不复用。 |
| `project_pen_names` | `project_id PK`、`owner_id`、`pen_name_id`；双复合 FK `(project_id,owner_id) → projects(id,user_id)`、`(pen_name_id,owner_id) → pen_names(id,owner_id)`，均 CASCADE；笔名索引。 |

关联表最多一行表示每书一个笔名；无行表示未设置。复合 FK 强制同作者，无需给旧 projects 表新增可空列或重建该父表。删除笔名只解除关联，不删除作品，包括回收站作品。

名称首尾 trim 后为 1–100 个字符，同作者按精确大小写敏感文本比较唯一；不引入隐式拼音、同音或大小写合并。前后端沿项目既有文本长度约定显示和校验，不以客户端校验代替服务器约束。

## API 契约

所有新请求 schema 拒绝未知字段，返回中文 `detail`。

| 方法／路径 | 请求与结果 |
| --- | --- |
| GET `/api/v1/pen-names` | 本人库 `[{id,name,revision,created_at,updated_at}]`，不接受 owner 参数。 |
| POST `/api/v1/pen-names` | `{name}` → 201 新笔名。 |
| PATCH `/api/v1/pen-names/{id}` | `{name,expected_revision}` → 更新结果。 |
| DELETE `/api/v1/pen-names/{id}?expected_revision=` | 204，解除含回收站在内的关联。 |
| PUT `/api/v1/projects/{id}/pen-name` | `{pen_name_id:int|null}` → `{project_id,pen_name}`，严格 owner-only；重复清除幂等。 |
| POST `/api/v1/projects` | 扩展可选 `pen_name_id`，默认 null；创建及关联同事务。 |

`ProjectOut.pen_name` 为 null 或 `{id: int|null, name: string}`：作者看到有效库 ID，其他有权读该作品的人只看到名称、`id=null`。`owner` 继续为真实账户。笔名摘要在最终权限复核查询中读取，不用之前读到的私库值套上新权限。

私人路由和 owner-only 关联写入对其他用户统一 404，包括管理员代管和协作者；无登录 401；格式错误 422；同作者重名 409 `pen_name_exists`；笔名修订失配 409 `stale_revision`，携带安全的当前修订。失效笔名 ID 不静默清空或改绑。

普通 `ProjectUpdate` 不接受笔名写入，不把新字段塞入现有 `content.edit` 的通用 setattr。作品关联在父锁内串行，沿用现有元数据后提交覆盖语义；笔名名称维护使用 revision CAS。

## 事务与权限

- 库维护：原登录认证 → authorization gate／User 写屏障 → 本人笔名锁及 CAS → 单次提交。
- 现有作品关联：原登录认证 → gate／User → 严格 owner 的活跃父项目锁 → 校验本人笔名 → upsert／清除 → 单次提交。
- 普通创建、恢复和模板创建：沿用外层 User 额度屏障，调用无提交的笔名校验／关联 helper；内部不能反向再取 gate，也不能自行 commit。
- 删除与新建／绑定由同一 owner User 屏障串行；删除先提交则旧显式 ID 失败，绑定先提交则删除级联解除，不能悬空或死锁。
- 改名通过查询时 join 反映，删除由数据库级联解除，不遍历活跃项目服务，以免遗漏回收站或删除中作品。

## 界面

书库提供管理笔名弹窗和全部／未设置／具体笔名筛选。具体笔名与未设置只作用于本人作品，全部仍显示协作书。筛选只保存在当前身份的组件状态，不把私库 ID 写入跨账号共享的 `ainovel.shelf`；通用排序／视图偏好保持原行为。

新建作品及概览提供作者专用选择器；其他访问者显示文字署名。概览成功时只合并 `pen_name`，不重置未保存的基本资料表单。书架卡片及文字封面优先笔名，无笔名回退账号名；协作邀请、管理员归属和审计仍用真实账户。

加载／保存捕获 actor、请求代次和 project scope，账号切换、撤权或离页后旧成功与失败响应都失效。删除正在筛选的笔名后恢复有效筛选。使用统一 API 客户端和 AntD App message，不新增全局缓存。

## 创建、导出与备份

| 路径 | 行为 |
| --- | --- |
| 普通创建 | 可选笔名，与 Project 同事务。 |
| 灵感／拆书 | 原流程创建无笔名作品，随后可在概览设置；不新增这些流程的问答步骤。 |
| 模板 | 由本次建书表单选择，不继承来源笔名。 |
| 单书备份 v10 | 顶层 `pen_name: string|null`，不包含库 ID、owner ID、其他笔名。读取兼容 v1–v9。 |
| 恢复 | 沿用现有按用户隔离的备份预览 token；最终在 User 屏障内按当前用户精确名称复用／新建，再绑定新书；整笔失败回滚。 |
| EPUB | 站内和 `open_api/catalog.py` 共用默认署名解析；显式 author（含已有空串语义）优先，无笔名回退真实作者用户名。 |

`project_pen_names` 登记在 `project_transfer.registry.NOT_EXPORTED`，理由为顶层署名恢复；用户级 `pen_names` 不登记在项目注册表。两表均不加入 Agent effects，智能体内容撤销不能回滚作者的私人署名。

整库备份为数据库原生备份，无需逐表白名单；新增表随原生快照保存。合法删号由 User FK 清笔名，不改变“有作品含回收站则拒绝删号”的规则。

## 迁移与回滚

实施前确认 Alembic 单一 head；调查时已存在 0054，不能按旧 `APP_SCHEMA=0052` 占用 0053。新增两表／索引，模型和迁移约束一致，更新 exporter.APP_SCHEMA。

空新增表可 downgrade；存在任一笔名或关联时拒绝有损 downgrade。部署回退优先退应用入口并保留数据表；v10 备份不能宣称可供只支持 v9 的旧应用导入。当前项目尚未进行部署操作。

## 验证与主要风险

验证项见 `implement.md`。重点为真实 DB 同作者约束、User／项目锁序、协作者私库隔离、最终署名投影、回收站解除关联和双 EPUB 路径。固定查询次数通过测试验证，不能按每本书逐项加载笔名。
