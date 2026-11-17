# 个人小说模板技术设计

## 状态与依据

整体方案已获批准；按约定在缩略图独立复核后启动实施。需求见 `prd.md`；底层原语、事务和当前投影证据见父任务 `research/template-design-evidence.md`。依赖已完成的笔名与缩略图契约，不复用提示词模板引擎。

## 快照语义和数据范围

模板为保存时的独立当前档案快照。现有“当前值”包含全部有效事件后的投影，也包含当前界面已经体现的 planned 变化；保存预览明确说明这一点。新书以这些值建立 confirmed 初始基线，不回放源剧情，不退回源的旧 baseline。

采用独立 `ainovel.novel-template` v1 严格DTO：显式字段白名单、局部ref和完整图验证。禁止反射复制所有ORM列，禁止将整书备份清空一个正文字段后当模板使用。

| 组 | 包含与处理 |
| --- | --- |
| 常用配置 | 题材、一句话设定、简介、主题、标签、卖点、读者、目标字数、auto_analyze、默认风格内容快照；全部字段／选项、分类／规则配置。新书名单独填写。 |
| 角色／世界观／组织／职业 | 勾选具体条目，显式内置字段、别名、标签、当前自定义字段value、AI可见性及隐藏规则；职业带其阶段。四种主体保持不同局部ref类型。 |
| 关联 | 关系／成员仅两端进入最终选择时保存，带实际保留关系需要的类型定义和有序端点；角色当前职业及阶段为必需依赖。使用当前关联，不携带已结束的旧关系和剧情历史。 |
| 大纲 | 选择章纲并带其细纲、卷信息及顺序；角色用局部ref。使用时按所选卷首次出现的顺序从 1 连续编号，保留卷标题；章纲号、排序及每条章纲的细纲序号连续编号。不创建章节／场景或章节链接。 |
| 图片 | 所选设定当前有效标准图，冻结为模板自身图片资源；共享图也按书内投影复制成独立快照。 |

不包含来源笔名、书名默认沿用、系列绑定、协作者、API Key、模型配置、模型授权、Skill／MCP／提示词覆盖、正文、章节／场景、聊天、笔记、知识库、运行任务、源历史、阅读／向导进度和源修订号。

保存配置组为可选；未选配置但设定值依赖某定义／选项或保留关系依赖关系类型时，必须带入所需最小配置闭包。选配置组则保留完整字段／选项／分类目录，即使零条目；关系类型始终仅保留被选中关系实际引用的定义。新书默认配置用于未提供的字段，不因快照缺失变为无效null。

风格保存完整内容；建书时可复用本人已有同内容风格，否则重建本人风格。预设也按内容比较，不能仅按preset_key使用已经改变的内容。建书后的风格编辑仍沿用现有风格库共享语义，不新增项目专属风格系统；模板本身的内容快照不被这些后续编辑改写。

### 关系类型快照与重建

类型DTO为 `{ref, origin, category, role_a, role_b, sort}`；origin为服务端生成的global／project，category仅允许family／social／hostile／work／other，称呼trim后非空且≤20字符，sort为PostgreSQL Integer范围内的严格整数。不得写入源id／project_id／时间戳。关系使用明确的type_ref和有序a_ref／b_ref；当前没有稳定builtin_key，不能按称呼、反向配对或近似名称猜测身份。

- origin=global且目标库恰有一个全局类型的category／role_a／role_b／sort逐字段完全相同时，才复用该只读类型。否则用冻结定义新建本书类型，在使用摘要说明；不能让当前全局定义改写保存时的语义。
- origin=project始终创建本书可编辑类型，即使某全局类型同名或完整内容相同。局部ref唯一，不做跨ref身份合并。
- 先形成type_ref映射并无提交写入类型，再用显式type_id创建关系；保持a／b语义方向，只重算pair_low／pair_high。不调用按称呼／反向匹配的find_type或现有备份类型合并分支。
- 项目类型唯一键为(project_id,role_a,role_b)，不含category。若全局回退与另一需新建类型撞键，预检返回400 `template_relationship_type_conflict`并整体拒绝，不改名、合并、交换方向或丢分类。全局复用与同称呼项目类型同时存在仍合法。

## 依赖预览与保存一致性

1. 选择请求只含配置开关、四类条目ID和章纲ID，不接收浏览器拼装的任意快照。
2. 预览在独立一致读快照中读取本人活跃作品：PG在首条SQL前设置只读REPEATABLE READ，SQLite显式BEGIN；最终使用原actor重新验证账号、权限及活跃状态，不在旧读快照中伪装成实时复验。
3. 解析当前值并构建局部ref图；返回选择数量、必要依赖及原因、包含／省略关联计数、体积、警告、`source_stamp`。人工快照保留AI隐藏字段，不能使用给模型的过滤结果代替。
4. 必要依赖包括typed引用、AI links、组织父链、当前职业／阶段、细纲角色、实际保留关系的类型定义及配置规则依赖。循环引用用visited闭包处理，不按名称猜测。作者明确补选实体依赖后重新预览；类型／字段等最小配套定义随所选内容携带且在预览列明，未补全实体时不能保存，不静默置空或拉入全图。
5. `source_stamp` 为规范化快照内容及图片摘要的指纹，包含所有选中内容和依赖、风格、配置、关系类型完整定义与有序关联、大纲、图片版本。不是授权凭据；不只比较Project.updated_at。
6. 保存时取得原认证gate／owner User屏障；先读取并锁定当前来源风格行，再取本人源项目活跃锁并重验风格绑定，在同一事务重建当前图并比对stamp。风格强制删除会通过外键更新作品，因此不得持项目锁后反向等待风格行；无风格时也需在项目锁内验证期间未新绑定。不同返回409 `template_source_changed`，保留选择；相同才写模板和图片，一次提交。不能持项目锁后反向等待系列锁。
7. 来源作品变更、回收或删除只影响后续重新保存；成功快照不再依赖来源ID。普通编辑仅改名称／说明；替换内容重新走预览和stamp，再结合模板revision CAS。

## 持久结构

| 表 | 主要字段与生命周期 |
| --- | --- |
| `novel_templates` | 永不复用的自增id、owner_id→User CASCADE、name≤100、description≤2000、format_version、snapshot JSON、revision≥1、摘要计数、来源标题／保存时间、时间戳。SQLite模型与迁移均显式sqlite_autoincrement=True，PG使用单调分配sequence；禁止应用指定旧ID或重置序列。无来源Project FK；列表不加载大JSON。 |
| `novel_template_images` | id、template_id→模板CASCADE、local_ref、mime、标准图bytes、sha256、size，`UNIQUE(template_id,local_ref)`。模板替换原子替换自己的图片，新书从中复制不共享。 |
| `novel_template_creation_receipts` | owner_id→User CASCADE、intent_key规范UUID、request_version／request_digest、result_project_id→Project SET NULL、created_at；`UNIQUE(owner_id,intent_key)`。只记录已成功创建。 |

模板名称允许相同，以ID区分；新建／改名不会覆盖同名模板。所有数据访问按实际登录owner隔离，管理员和协作者不例外。

删除最后一个模板后新建也必须得到新ID；旧页面携带的ID／revision不能修改、删除或以新意图使用另一模板。旧成功intent仍按收据重放原作品，这不等于访问已删除模板。

模板与图片纯用户级，不加入单书注册表。收据因为引用Project，加入NOT_EXPORTED，理由为用户创建幂等凭证；不进Agent effects、不随单书恢复迁移。整库原生备份保留这三表。

工程上限：最终选择图≤2000个四类实体、≤2000条章纲、≤5000条细纲；规范JSON UTF-8≤10MiB；标准图累计≤50MiB且单张遵守512KiB。遍历与查询在达到限制时提前终止，图片总量按元数据估算后才读取；预览清楚报告超限，不静默截断。前后端共同展示限制，服务器负责最终约束。不新增个人模板个数配额。

## API 契约

| 方法／路径 | 输入与结果 |
| --- | --- |
| GET `/api/v1/novel-templates` | 本人分页列表，page_size≤50，名称查询；只摘要。 |
| GET `/api/v1/novel-templates/{id}` | 当前revision、配置／条目／大纲摘要、图片ref；可按内容分组分页预览，不把全部二进制内嵌普通响应。 |
| GET `/api/v1/novel-templates/{id}/images/{ref}` | 仅本人当前模板标准图，校验当前revision；private,no-cache，无裸资源访问。 |
| POST `/api/v1/projects/{pid}/novel-template-preview` | 配置开关＋所选四类ID＋outline_ids → 依赖、警告、计数、体积、source_stamp。只读，不占作品额度。 |
| POST `/api/v1/novel-templates` | name、description、source_project_id、选择、source_stamp → 201模板摘要。 |
| PATCH `/api/v1/novel-templates/{id}` | name／description＋expected_revision → 新revision。 |
| PUT `/api/v1/novel-templates/{id}/snapshot` | 源作品／选择／source_stamp＋expected_revision → 整体替换内容，新revision；失败原快照完整。 |
| DELETE `/api/v1/novel-templates/{id}?expected_revision=` | 删模板和自身图片，保留已建作品及创建收据。 |
| POST `/api/v1/novel-templates/{id}/projects` | expected_revision、intent_key、新title、可选立项覆盖、本人pen_name_id → 新ProjectOut；同一成功意图重放返回同一作品。 |

新请求extra=forbid，私有对象不可访问／不存在统一404，参数错误422；依赖未齐400 `template_dependencies_missing`，快照损坏400 `invalid_novel_template`，超限400 `template_too_large`，源指纹或revision冲突409，quota沿用既有错误契约。错误只返回当前用户有权看的说明。

## 原子建书与幂等

新建与模板维护统一使用原actor gate／User屏障。建书在取User之后先普通读取当前用户intent收据，不能先锁收据再锁结果Project。

- 已成功的相同key：请求摘要不同为409 `creation_intent_conflict`；相同则读取并锁原结果Project，复核owner及活跃状态后返回。先重放，后检查模板／额度，因此成功后模板被删或额度用尽不会导致重复创建。
- 结果软删、删除中或FK已NULL：409 `creation_result_unavailable`，保留收据，绝不回到新建分支。恢复作品后相同key仍返回原作品。
- 无收据：校验本人模板revision／严格DTO／图片／引用图 → 在同User屏障内查quota → 创建完整新书 → 插入成功收据 → 单次提交。
- 请求摘要使用固定request_version规范化模板ID、请求revision、title、覆盖项及笔名选择，不根据重试时模板新内容重算请求身份。同一用户不同key争抢最后额度仍被User锁串行。
- 收据只在新书成功事务内产生，不预提交pending、不设置会重用key的TTL；User最终删除可级联。模板删除不能CASCADE掉收据。
- 系统purge会Project→receipt SET NULL；重放顺序为User→普通读receipt→Project，禁止receipt行锁→Project形成死锁。新建也先Project后receipt。

无提交的落库顺序：Project → 配置目录／选项／分类及关系类型映射 → 四类实体／职业阶段 → 局部引用／父级 → confirmed基线和typed当前值 → 当前关系／成员／职业 → AI links → 大纲／细纲 → 独立标准图和笔名关联 → 图完整性校验 → 成功收据 → commit。

复用 `new_character/new_world/new_org/new_career`、timeline和字段codec等无提交原语；不可直接串联会commit的create_project、import_cards、import_outline或import_backup。必要新创建记录可由既有业务原语生成，这不等于复制源历史；不能删除公共服务审计行为只为让历史行数为零。派生索引按新身份重算，不复制来源索引，不触发模型调用或后台生成任务。

任何异常或取消整体回滚，包括新风格副本、图片、绑定和收据。提交结果未知时用同key查询恢复，不换key自动补建。

## 前端流程

- 个人模板库作为创作资源中的独立懒加载页面；列表显示名称、说明、保存时间、内容计数，提供预览、改名、删除及从作品重新保存。
- 本人作品概览提供“保存为小说模板”，分组选择配置／四类设定／大纲，预览显示必需依赖及省略关联；显式补选后再保存。
- 新建作品弹窗可选择空白创建或个人模板，展示模板revision与摘要、输入新书名和笔名。模板方式成功进入概览，空白方式仍按原行为进入向导。
- 生成一次创建意图UUID并在断网／超时后保留，用同key重试；只有用户明确开始另一次创建才换key。旧版本冲突保留输入，重新确认模板后建立新的意图。
- 请求都走api客户端；actor、请求代次、project scope及弹窗实例共同守卫成功／失败响应，卸载取消本轮请求但不误删已成功作品。不缓存跨账号的模板或预览。

## 迁移与回滚

在笔名和缩略图验收后按真实head新增三表；验证SQLite真实迁移创建AUTOINCREMENT而非仅普通INTEGER PRIMARY KEY，以及PG sequence不复用已删除身份。本功能不再升级作品备份格式（维持v11），模板内部格式v1。更新APP_SCHEMA和registry收据排除规则。

任一模板、图片或成功收据存在时拒绝有损downgrade；尤其不能删收据后让旧请求再次建书。应用回滚保留表，整库恢复同时保留成功收据。新建路径失败依靠事务，不使用先提交再补偿删除。

## 验证重点

按 `implement.md` 覆盖四实体×字段类型、当前planned投影、关系类型／方向／冲突、同名不同ref、引用环、缺依赖、旧源删除、模板替换、删除后身份不复用、风格快照、独立图片、无正文和原子失败，以及真实PG配额／幂等／purge竞争。这里只是设计，尚未执行测试。
