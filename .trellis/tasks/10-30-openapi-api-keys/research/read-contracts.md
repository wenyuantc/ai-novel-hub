# Research: 创作设定开放读取契约

- Query: 核对人物、大纲（分卷与细纲）、世界观、组织、人物关系与关系类型的字段、归属校验、排序、复用入口，以及更新时间和动态关系注意项。
- Scope: internal。仅只读研究上述实体；不覆盖小说 / 章节基本读取与密钥管理。
- Date: 2026-10-30。

## Findings

### 1. 文件与相关规范

| 文件 | 用途 |
| --- | --- |
| `backend/app/schemas/story.py` | 世界观、人物、基础大纲的请求和响应 |
| `backend/app/schemas/outline.py` | 细纲、带细纲的大纲概览 |
| `backend/app/schemas/organization.py` | 组织、成员、成员计数 |
| `backend/app/schemas/relationship.py` | 关系类型、人物关系、历史、关系图谱 |
| `backend/app/services/story_service.py` | 三类设定的通用归属校验和排序 |
| `backend/app/services/outline_service.py`、`plan_service.py` | 分卷大纲和细纲序列化、已删除角色过滤 |
| `backend/app/services/organization_service.py` | 组织归属、成员查询及批量计数 |
| `backend/app/services/relationship_service.py` | 可见类型、关系归属、视角称呼与批量序列化 |
| `backend/app/models/entities.py`、`base.py` | 实体字段、外键与行时间戳 |
| `.trellis/spec/backend/directory-structure.md` | API → service → ORM，路由不能直接查询 ORM |
| `.trellis/spec/backend/database-guidelines.md` | user_id 归属校验、404、时间戳与项目备份登记 |
| `.trellis/spec/backend/story-entities.md` | 关系方向、预置类型、成员与历史契约 |
| `.trellis/spec/backend/outline.md` | 大纲排序、章号平移、细纲引用规则 |
| `.trellis/spec/backend/relationship-generation.md` | 现有关系类型 / 角色对并发与最新输出语义 |

### 2. 当前字段、排序与可复用入口

| 实体 | 对外可用的领域字段 | 当前入口、顺序和限制 |
| --- | --- | --- |
| 人物 | id、name、role_type、profile、relations_text、aliases、age、gender、appearance、personality、background、current_state、life_status、sort | `story.py:93` 的 `CharacterOut`；`story_service.py:33` 按 `(sort, id)`；`:46` / `:76` 提供 list_owned / get_owned。`always_in_context` 是写作注入配置，对外读取无必要时可省略。 |
| 世界观 | id、title、content、category、sort | `story.py:46`；`story_service.py:32` 按 `(sort, id)`；`:38` 支持 category 过滤。分类为 place / faction / item / rule / history / other（`story.py:7`）。 |
| 大纲 | id、volume_no、volume_title、chapter_no、title、summary、sort | `story.py:132`；`story_service.py:35` 明确顺序 `(volume_no, sort, chapter_no, id)`，不能改成只按章节号。分卷当前是大纲上的两个字段，并无独立 Volume 表。 |
| 细纲 | id、outline_id、seq、title、summary、key_events、character_ids、emotion、goal、est_words、chapter_id、chapter_no、has_content、created_at、updated_at | `outline.py:85` 的 PlanOut；`plan_service.py:61` 按 `(seq, id)`；`:99` 的 list_plans 验证小说与大纲。`:73` 的 plan_outs 过滤已删除角色并批量附章节信息，不能直接 model_validate 后就返回。 |
| 组织 | id、name、org_type、purpose、location、power_level、parent_id、description、sort、member_count | `organization.py:34`；`organization_service.py:114` 按 `(sort, id)`；`:126` 单组织读取；`:52` 批量计数，避免每个组织单独 count。 |
| 组织成员 | id、organization_id、organization_name、character_id、character_name、position、status、note | `organization.py:63`；`organization_service.py:175` 批量联表并按 member.id 排序；`:221` 的 list_members 先验证小说和组织。状态为 active / left / expelled / dead。 |
| 人物关系 | id、a_id、b_id、a_name、b_name、type_id、category、role_a、role_b、intimacy、status、description、created_at、updated_at | `relationship.py:75`；`relationship_service.py:442` 按 id 升序；可传 character_id，此时额外输出 other_id / other_name / other_role / self_role。`:384` 的 to_outs 批量补充双方名字和类型，避免 N+1。 |
| 关系类型 | id、category、role_a、role_b、sort、builtin | `relationship.py:26`；`relationship_service.py:161` 返回全局预置与当前小说自定义类型，预置优先，再按 `(sort, id)`。不能对外列出其他小说的自定义类型。 |

上述基础 Out 常含 `project_id`，开放 API 可按统一字段约定映射为 novel_id。人物 / 世界观 / 基础大纲 / 组织 / 成员 / 关系类型的现有 Out 没有时间戳，但 ORM 均混入 TimestampMixin：`entities.py:211`（WorldSetting）、`:223`（Character）、`:248`（Organization）、`:268`（OrganizationMember）、`:285`（RelationshipType）、`:445`（Outline），时间戳定义在 `base.py:19`。若新契约需要 updated_at，应建立新的开放响应，不要依赖旧 Out 恰好包含该字段。

### 3. 建议端点

均为 GET，完整前缀为 `/api/open/v1/novels/{novel_id}`。列表建议统一 `{items, total, page, page_size}`，默认 page_size=20、范围 1–100，在 service 层数据库分页；当前所有内部列表均无分页，直接调用后 Python 切片会读取全量。

| 路径 | 建议行为 |
| --- | --- |
| `/characters`、`/characters/{character_id}` | 人物列表与卡片详情；可增加姓名 / 别名关键词筛选。 |
| `/world`、`/world/{setting_id}` | 世界观列表与详情；category 使用现有枚举。 |
| `/outlines`、`/outlines/{outline_id}` | 按原业务顺序返回大纲及分卷信息；可用 volume_no 筛选。 |
| `/outlines/{outline_id}/plans` | 细纲独立分页，明确 seq 是纲内序号，chapter_no 是已建章节号，可为空；沿用 plan_outs 的引用清理。 |
| `/organizations`、`/organizations/{organization_id}` | 保留 parent_id / member_count；列表不内嵌全体成员。 |
| `/organizations/{organization_id}/members` | 单独成员列表，避免组织页膨胀；允许 status 过滤，默认返回所有状态。 |
| `/relationships`、`/relationships/{relationship_id}` | 关系清单与单关系详情；character_id 过滤沿用视角语义；可提供 status / category 筛选。 |
| `/relationship-types` | 与关系列表配套的可见类型字典；全局预置 + 当前小说自定义。 |

关系响应已经内嵌双方姓名及 role_a / role_b，调用方不必为显示每条关系请求人物详情和类型详情；仍应提供人物列表 / 详情以查看档案，并提供类型列表以枚举没有关联记录的可用类型。关系类型单条详情、关系图谱、角色 memberships 反向查询、历史变更、卡片导出都不是实现本次已确认读取的必需端点，首版可不额外加入。`relationship_service.py:592` 的 graph 可作为后续整图接口复用参考，但它无分页、组织成员也没有 active 状态过滤。

### 4. 必须保留的隔离与一致性

- 现有归属校验统一从 `get_owned_project` 开始，随后用资源 id + project_id 联合过滤：人物 / 世界观 / 基础大纲见 `story_service.py:76`；组织见 `organization_service.py:33`；关系见 `relationship_service.py:434`；细纲所属大纲见 `plan_service.py:44`。开放端必须在此之外先验证 Key 的小说授权，不应以 Key 有效就允许遍历所有资源。
- `story_service.list_items`（`:38`）、`outline_service.list_outlines`（`:17`）、`plan_service.plans_of`（`:61`）、`relationship_service.to_outs`（`:384`）是低层入口，没有完整用户校验，只有在外层验证授权与归属后才可复用。
- `relationships?character_id=` 必须验证角色属于路径中的小说；当前 `relationship_service.py:447` 在角色不存在或属于别的小说时返回 404，不能改成空结果或查出跨小说关系。
- `relationship_service.py:67` 的类型可见性只允许 `project_id IS NULL OR project_id == novel_id`；全局预置并不意味着可枚举其他小说类型。
- 关系称呼语义是“a 是 b 的 role_a，b 是 a 的 role_b”。a 看对方用 role_b；b 看对方用 role_a（`relationship_service.py:413`）。不要把关系当成两条有向边并重复返回，也不能无视 a / b 方向互换称呼。
- 组织的 member_count 当前统计所有状态（`organization_service.py:41`），不是“在职人数”；成员读取也不默认隐藏 left / expelled / dead。文档应准确命名；若增加 active_count，应作为独立派生字段，而非改变现有语义。
- 组织上级删除后 parent_id 由外键 SET NULL（`entities.py:261`）；人物删除会级联组织成员与相关关系（`:276`、`:327`）。客户端应以稳定 id 引用，不能只按名字关联。
- 细纲的 character_ids 是 JSON，删除人物不会修改原 JSON，必须经 `plan_service.py:88` 筛除无效 id；大纲删除后细纲级联删除、章节保留，不能把 outline_id 等同于章节唯一身份。未建章细纲的 chapter_id / chapter_no 允许为空。

### 5. 更新时间和剧情时点

- TimestampMixin（`base.py:19`）只代表该行更新，不包含所有派生字段变化。成员新增 / 删除不一定刷新组织 updated_at；人物改名会改变成员 / 关系输出的 *_name，却不改成员 / 关系行时间；细纲过滤角色或附加 has_content 也可能改变响应而不改细纲时间。
- 因此 updated_after 适合“行更新筛选”，不能承诺完整对象增量同步。若要完整同步，需聚合依赖变更、删除事件或让客户端定期读取全量目录；在未实现这些机制前，文档必须说明边界。
- 关系列表当前返回所有状态 normal / broken / past / complicated，没有默认排除历史关系；组织成员也保留离开等状态。状态筛选属于读取参数，不应悄悄改变默认可见数据。
- 人物 current_state / life_status、组织成员与关系均为最新保存值，可能由后面章节分析更新；没有“按阅读章节重建过去状态”的现有列表能力。关系历史端点 `relationship_service.py:527` 只是变化记录，不能当成准确的章节时点快照。开放文档应说明返回当前设定，可能包含尚未读到的剧情。

### 6. 建议验证点

- 每类列表和详情均测试他人小说、Key 未授权小说、同用户其他小说的资源 id；类型检查预置可见 / 他小说类型不可见。
- 过滤角色跨小说时 404；关系两种视角的 other_role / self_role 正确；不按 ID 大小自动改写 a / b。
- 大纲有序号和章号不一致时仍按 `(volume_no, sort, chapter_no, id)`；细纲无章节、人物被删时仍返回合法契约。
- 组织列表成员计数按约定统计全部状态，members 端点 status 过滤和关联姓名正确。
- 对外分页在查询层执行，所有排序含 id 兜底；原内部裸数组 API 的契约保持兼容。

## External References / Versions

本报告完全基于当前仓库，无新增第三方库、版本选择或外部技术资料；不需要更新依赖。OpenAPI 工具和密钥加密的官方资料由主会话另行核对。

## Caveats / Not Found

- 未发现上述实体的现有数据库分页、删除事件游标、统一增量同步或“按某章恢复当时状态”读取能力。
- 当前内部只有关系列表和历史读取路由，没有 GET `/relationships/{id}`；可组合 get_owned_relationship + to_outs 增加开放单条读取，路由不应自行查询 ORM。
- 现有 outline_tree（`outline_service.py:41`）一次加载整个小说大纲与全部细纲；如果对外给树接口，不能把它当成已有分页能力。推荐平铺大纲 + 单大纲细纲读取以控制响应大小。
- 以上端点与字段为主会话形成最终方案的建议，不代表已经批准实施或已经完成接口。
