# 规划矩阵、支线交织与 AI 规划上下文

> 章节／细纲与独立场景共用契约。实现入口：`services/planning_{queries,service,lifecycle,context_service,scene_service}.py`、`api/v1/planning.py`、前端 `pages/project/PlanningPanel.tsx`。正文与结构合同见 [独立场景](./scenes.md)。

## 1. 范围 / 触发条件

新增规划来源、设定类型、支线动作，或修改章／细纲／大纲／选角／快照／作品备份／智能体撤销时，都需核对此契约。规划身份独立于章和细纲；删除来源不会删除作者的规划。只读矩阵和 AI 预览不得为了显示虚拟行而创建记录。`matrix/weave` 的全部SQL（包括提及、节点/交汇窗口、最后revision读）均禁用autoflush，不能只保护最初的快照读取。

## 2. 签名

- 迁移 `0034`：`projects.planning_revision`；`planning_units`、`planning_tags`、`planning_unit_tags`、`planning_unit_world`、`planning_unit_organizations`、`planning_unit_careers`、`planning_unit_relationships`、`subplots`、`subplot_nodes`、`subplot_crossings`、`subplot_crossing_participants`。
- `PlanningUnit` 的 `chapter_id`／`plan_id` 各自唯一且 `ON DELETE SET NULL`，`pov_character_id SET NULL`；`binding_epoch` 记录作者改挂代次，`retired` 防止被合并单元复活。
- 以下路径均在 `/api/v1/projects/{project_id}/planning` 下，schema 以 `schemas/planning.py` 为准：

| 方法／路径 | 主要契约 |
|---|---|
| `GET /matrix` | `granularity=chapter|scene`、`column_mode=settings|subplots|custom`，行分页、30列窗口；`column_refs`／`source_refs` 为有界 JSON 数组 |
| `GET /entries`、`GET /tags`、`GET /subplots` | 分页；设定按类型、分类、名称筛选，支线按名称筛选 |
| `PATCH /units/batch` | `expected_revision`、1–100个 `targets`、可选 POV／标签／各类型关联操作 |
| `POST /units/{unit_id}/binding` | `expected_revision`、`target:{kind:chapter|plan|scene,id}`；scene只允许同章同层 |
| `POST /units/merge` | `expected_revision`、双方 unit ID、明确 POV 和重叠支线的 `node_resolutions` |
| `GET /detached` | 失去全部来源的未退役规划 |
| `POST /tags`、`PATCH/DELETE /tags/{id}` | 项目标签及固定 tone 枚举 |
| `POST /subplots`、`PATCH/DELETE /subplots/{id}` | 支线名称、作者概览、tone、sort |
| `PUT/DELETE /subplots/{id}/nodes/{source_kind}/{source_id}` | 一个支线在一个 unit 上最多一个普通推进节点 |
| `GET/POST /crossings`、`PATCH/DELETE /crossings/{id}` | 明确交汇、位置及完整参与集合；PATCH 可移动位置 |
| `GET /weave` | `axis=chapter|plan|scene`、行／支线独立分页、`gap_threshold=1..100`（默认5） |
| `GET /units/context-preview` | `source_kind`、`source_id`、`scope=chapter|selection`、`budget=256..20000` |

写请求都有 `expected_revision`（DELETE 放 query），成功响应都有新 `revision`。创建返回201；DELETE 返回200和 `{revision}`，不能按204丢弃响应体。

## 3. 契约

### 身份、事务与旧入口

- `PlanningSource={kind:chapter|plan|scene|detached,id}`；未建章细纲按卷／章纲／seq 展示，不能把其显示位置当成实际章号。默认章粒度中已建章细纲只展示对应章一行；scene粒度只展示真实活动场景。
- 写入先通过 `get_manageable_project(..., lock=True, permission="projects.edit")` 和项目回收站屏障；持锁核验所有目标及引用，再在同一事务以 `planning_revision == expected_revision` 原子递增。任何目标失败整批回滚。
- 所有旧入口影响来源身份或角色权威时调用 `planning_lifecycle`：创建／修改／建章／删章／删细纲／改纲／重新展开／向导覆盖／导入替换／恢复／设定真删除。辅助函数只 flush，由原有服务拥有 commit。`bump_revision` 必须使用 `no_autoflush`，不能提前提交调用方待写实体，绕过版本冲突与审计的专用flush边界。
- 已建章以 `ChapterCharacter` 为角色关联权威，`ChapterPlan.character_ids` 是镜像；未建章使用 plan 的 `character_ids`。首次建章合并双方角色；以后任何入口修改都同步，不得保留两套独立事实。
- 章层来源消失时保存最后位置和 `detached_character_ids`（空数组也是有效作者选择）；节点和交汇继续跟随 unit。scene层保留本地角色join，不复制章层detached角色副本。双方已有实质规划时先报合并冲突；空单元可自动退役，不能静默覆盖有内容单元。
- 显式改挂／合并递增 `binding_epoch`；正常建章／自动脱离不冒充新的作者意图。普通 API 和旧快照不能复活 retired 单元。
- 快照保存 `planning_bindings:[{unit_id,plan_id,binding_epoch}]`。恢复前在项目锁内捕获当前 `unit→chapter` 锚点，恢复时同时校验 epoch、retired、占用和真实章锚点；不信裸整数 ID 或重建后的 `Chapter.plan_id`。不确定时保持待关联并返回 warning，不回退新节点文本或存活章角色。
- effects 对规划及子记录内容做稳定签名以检测作者后续修改；撤销恢复双方来源前先统一释放受影响单元的 chapter_id／plan_id，避免按 ID 逐行恢复撞唯一约束。所有步骤在保存点内；revision推进必须放在恢复、时间线物化和最终flush都成功之后，以免失败回滚使调用方Project对象过期；revision／binding_epoch 不回退，显式撤销可恢复自动退役空单元并升新代次。

### 设定、矩阵与交织

- `0039`增加`source_level=chapter|scene`、唯一可空`scene_id`及七项`inherit|override`模式和`planning_unit_characters`。scene层不同时绑定chapter/plan；inactive来源保留身份，在待关联中展示，物理失联后不能跨章认领。
- 场景`ScenePlanningOut.raw`保存模式／本地值，`effective`逐项解析本章默认并带`sources`。明确空覆盖不同于继承。场景角色不反写章cast。batch写scene必须带`expected_content_revisions`，同章多目标只递增一次内容版本；完整继承／引用合同见[场景](./scenes.md)。
- 标签被场景本地或结构历史引用时删除返回409`planning_tag_in_use`并列来源；不能删除标签后留下不可导入的结构历史。没有Scene依赖的旧章标签仍可删除。
- 引用为判别联合：`{kind:character|world|organization|career,id}` 或 `{kind:relationship,timeline_id}`。world 覆盖全部现有分类；关系必须使用 `setting_timelines.id` 且 `subject_kind=relationship`，不能用当前 `relationships.id` 替代。隐藏或尚无当前载体的关系仍有稳定引用。
- 集合操作 `add|remove|replace|clear`；关系用 `timeline_ids`，其他用 `ids`。`clear` 不带 ID；每种设定最多一个操作，所有 ID 必须属于同作品。POV 使用 `set + character_id` 或 `clear`。
- 矩阵列有 `selection.kind=setting|subplot`，格子有 `kind=setting|subplot`。设定格区分 `manual`、`pov`、正文确定／歧义次数、`tracking_supported`、`has_content`；未写或没有启用追踪不等于零次出场。目录和统计使用同一 `load_candidates` 快照，角色、各世界观、组织和职业的实际追踪配置决定 `tracking_supported`；关系不追踪。用 `match_entities` 读取每次命中的归属，不能绕过大小写／排除规则。
- 交汇独立于普通 node：2–20条同作品、不重复的支线，即使参与方没有 node 也算推进。相同标题的不同 event ID 不合并；筛掉某列不能删除事件里的其他参与者。
- 已建章轴、计划轴和场景轴分别按完整真实顺序计算；断档在未分页轴上算实际位置数，不以章号相减，也不混合三种轴。`WeaveGap.after_label/before_label` 来自完整轴的真实位置，跨分页仍显示准确间隔；UI过滤后交汇提示未展示参与支线数。`same_position` 只说明同位置推进，剧情交汇必须有作者维护的 crossing。
- 被交汇引用的支线删除返回409。数据库参与表的 subplot FK 使用 `NO ACTION DEFERRABLE INITIALLY DEFERRED`：独立删支线仍受保护，整作品多路径级联能在事务末清空；不能改成立即 RESTRICT／NO ACTION。

### AI 与备份

- `load_planning_context(db, project, source_kind, source_id, *, scope="chapter", budget=4000, text_hint="", legacy_text_hint="", stamp=None, preceding_text=None)` 同时服务预览和实际生成；只读、无副作用，并检查读取前后版本。scene分支返回真实position、manual_character_ids和content_revision；preceding_text=None才读取DB前景，空串是明确无前景，整章暂存稿必须替代而非追加旧前景。
- 目标章的 POV、普通节点、交汇和显式设定进入独立锁定 `planning_context`。排除其他章／未来节点／作者支线概览／未建章其他计划／孤立／退役内容；只有交汇、只有关系引用也必须生效。
- generate／revise／五类局部改写接入；自动续写复用首轮 messages，批量每章重新组装。局部 scope 是背景约束，不能要求当前选区完成整章规划。
- 角色状态、关系、成员、职业阶段按目标章初或scene start解析；不存在关系不能泄漏未来类型。组织简介／职业体系是当前静态档案，必须标明边界。引用两端角色仅作参考，不自动选角、授职、加入组织或新建关系。
- 规划资料与普通上下文去重且计入同一预算，公平确定性裁剪并标记；容纳不了最小语义骨架时明确失败，不静默漏掉某类资料。无规划或仅标签时不增加剧情指令；0039的锁定场景范围块仍独立加入，不承诺整条消息与旧版逐字相同。
- `chapter.generate/revise/rewrite` 默认模板、registry 和 builder 都声明锁定变量；旧覆盖漏变量仍兜底一次。生成日志记录使用的身份、版本、引用及截断信息。预览未建章时说明正式有效状态待建章后确定。
- 规划最初由v3／0034注册11表，0039的v7增加scene来源、本地角色和结构历史引用。旧格式转换不能夹带新结构。全部typed FK、JSON角色与快照ID重映射；保留orphan、retired和binding_epoch，新作品planning_revision从0开始。已建章plan角色仍以ChapterCharacter为准，不修改源库旧镜像。

### 前端

- `api/planning.ts` 统一请求；`types.ts` 镜像联合类型，页面不得直接 fetch。写入触发规划和相关章／细纲／提及数据刷新。
- 批量表单明确受影响数量与操作，不默认 replace；409保留用户草稿，先展示最新资料供核对，再显式刷新版本／重试，不自动覆盖。
- `planning_merge_conflict` 在矩阵重连和旧建章入口都提供合并操作。成功后用户重试建章，避免隐式连续写入。
- 作品／actor／授权代次变化时使旧请求失效；只读身份能查看矩阵、交织和预览，但不能编辑。
- 使用主题 token，375px容器内滚动矩阵，SVG轨道有等价数据表；键盘可达且焦点可见。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 非法判别类型、重复目标／参与方、空白节点、超长／超量参数 | 422 |
| 不可见作品／失效来源／不可用矩阵列 | 404 |
| 批量指定不存在或跨作品设定ID | 400，整批拒绝 |
| 可见但无写权限、回收站写屏障 | 沿用项目403／生命周期错误 |
| 旧项目版本 | 409 `stale_planning_revision`，带 `current_revision` |
| 来源已被重新关联 | 409 `planning_source_changed` |
| 两个来源都有独立规划 | 409 `planning_merge_conflict`，带双方 ID／版本／节点冲突信息 |
| 支线仍被交汇引用 | 409，列出阻止删除的事件信息 |
| 预览读取中规划变更 | 409 `planning_context_changed` |
| 规划最小语义内容超过预算 | 明确预算不足错误，不能返回伪完整块 |
| 快照身份不可证明 | 恢复其余数据，规划保留待关联并给 warning |

## 5. Good / Base / Bad Cases

- Good：计划中三条支线通过一个事件交汇；无普通节点也显示推进；建章后unit ID不变，生成只带当前章事件且包含完整三方。
- Base：旧作品未写规划，矩阵返回虚拟行；读取不新增unit；旧生成 messages 保持原样。
- Bad：删除旧章后整数ID被新章复用，再恢复旧快照；不能凭ID把旧规划挂到新章。

## 6. 必需测试

- `test_planning.py`：类型引用、CAS、批量原子性、矩阵双列类型、交汇和权限；`test_planning_migration.py`：升级／约束。
- `test_planning_lifecycle.py`：建章和冲突、全旧写入口、cast权威、ID复用／epoch／retired、快照、v3完整往返、effects作者编辑与恢复顺序。
- `test_planning_context.py`：捕获真实FakeLLM messages，覆盖未来隔离、全部引用、关系历史、只有事件、五类局部、批量／续写、旧覆盖、预算。
- `test_planning_readonly.py` 以SQLAlchemy before_flush屏障核验所有只读入口；`test_planning_access.py`覆盖22条路由的跨作品隔离和只读权限。
- 前端 `api/planning.test.ts`／`utils/planning.test.ts`：请求编码、DELETE版本响应、冲突提取、引用与位置语义。
- 真实PostgreSQL验证fresh迁移、同版本并发只成功一次、整作品交汇级联、快照恢复序列不回退；SQLite不能代替这些断言。
- 浏览器验证矩阵→批量→建章→AI预览、两页冲突草稿、交汇管理、双轴／文本表、只读／键盘／375px／明暗主题。

## 7. Wrong vs Correct

错误：恢复后读 `chapter.plan_id == snapshot.plan_id` 就恢复规划；或逐条恢复双方unit外键。

正确（见 `planning_lifecycle.chapter_anchors/restore_bindings`、`agent/effects.py`）：

```python
anchors = await planning_lifecycle.chapter_anchors(db, project_id)  # 删除来源前，在项目锁内取证
await planning_lifecycle.detach_outlines(db, project_id)
# 恢复来源后，结合 epoch、retired 与 anchors 判定身份；不猜测裸 ID。
await planning_lifecycle.restore_bindings(db, project_id, bindings, plans, anchors)
```

撤销合并／建章时，先核验effects签名，再在同一保存点内释放双方来源并恢复完整状态；作者后续编辑时应拒绝撤销。
