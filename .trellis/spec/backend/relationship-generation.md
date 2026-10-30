# AI 批量关系生成：跨层契约

## 1. 范围与触发

“人物关系”页选定已有角色，生成他们之间的缺失关系草稿，预览编辑、勾选后保存。涉及新增生成入口、任务 kind、模板、草稿格式、关系来源或并发保存时读取本文。

实现：`services/entity_ai_service.py`、`task_engine/handlers/entities.py`、`api/v1/relationships.py`；前端 `RelationshipsPanel`、`EntityTaskModal`、`RelationsGenerateResultView`、`api/entityTasks.ts`。

## 2. 签名

```text
POST /api/v1/projects/{pid}/relationships/generate
  RelationsGenerateRequest → 202 TaskOut(kind="relations.generate")
GET /api/v1/tasks/{id}/stream
  snapshot / delta / progress / done{task, result} / error
POST /api/v1/projects/{pid}/relationships/batch
  {items: RelationshipCreate[]} → 201 RelationshipOut[]
```

```typescript
relationshipApi.generate(pid, body: RelationsGenerateRequest): Promise<Task>
parseRelationsGenerateResult(result: unknown): RelationsGenerateResult | null
generatedSuggestionsToBatch(list, selectedIds): RelationshipCreate[]
filterExistingGeneratedSuggestions(list, relationships): EditableGeneratedSuggestion[]
```

写入继续经 `create_relation → resolve_type → apply_change`。后两者不提交、不主动回滚；HTTP create/batch 拥有请求事务，分析、导入和智能体的外层事务/SAVEPOINT 保留控制权。

## 3. 请求、结果与数据契约

请求：

```json
{"character_ids":[11,12,13],"count":10,"instruction":"补充同门间的旧恩怨","ai_config_id":null}
```

- `character_ids`：2–20 个不同正整数；拒绝 bool、浮点数、字符串 ID，不静默去重。
- `count`：严格整数 1–50，默认 10；实际目标为 `min(count, 缺失角色对数)`。
- `instruction`：可选，默认空，最多 2000 字；AI 配置可空，沿用现有默认配置解析。
- 创建与运行阶段均校验项目、角色选择、AI 配置与剩余关系对。`input.request.character_ids` 保存原选择，`TaskOut.character_id` 为 null。
- `resource_key=None`，同类生成草稿可并发。关闭弹窗只 detach，任务继续；取消由显式操作触发。创建端点不提供幂等键，前端以 starting/running 防重复点击。

任务结果（不在 TaskOut 直接嵌入，需读订阅或完成快照）：

```text
RelationsGenerateResult {
  character_ids: number[]
  suggestions: (RelationSuggestion & {
    subject_id: number | null
    subject_name: string
  })[]
  warnings: string[]
}
```

`RelationSuggestion` 沿用 target 身份、resolved/reason、existing_relationship_id、self_role/target_role、subject_is_a、type、intimacy、status、description。原始模型输出是 `{"relationships":[{"a_id":11,"b_id":12,"role_a":"师父","role_b":"徒弟","category":"social","intimacy":60,"status":"normal","description":"授业之恩"}]}`。

- 两端只按原选择的 ID 关联，且必须仍在当前项目；名字/别名仅显示，不用名字回退解析身份。
- 双方称呼、类别和状态必须原本是文本，称呼非空且各 ≤20 字；亲密度为 -100～100 的整数，类别/状态属于现有枚举。不能先转成文本或默认值，再把非法原始类型恢复为有效建议。
- 无序角色对去重，已有关系、自指、未选/删除角色或非法字段标为不可保存并给出原因；合法建议不超过有效目标，预览最多 50 行，超出部分有警告。
- JSON 缺少 relationships 数组是任务失败；合法空数组是成功空结果。生成只读，不写角色、关系、类型、历史或 generation_logs。
- 类型匹配只调用 find_type，不调用会创建类型的 resolve_type。匹配反向类型时 subject_is_a=false；编辑称呼后清 type_id，按主体为 role_a 重新匹配。

上下文：身份清单及已有角色对 ID 独立保留，不裁掉后面的身份；每名角色卡片按 800 字、世界观按 4000 字、关系详情按 6000 字、可见类型参考按 3000 字预算裁剪，超限后附省略号。复用角色卡和组织成员；知识库沿用项目总开关与参考优先级，不读全书正文。模板 key 为 `relations.generate`，json_rule/output_format 锁定，与 relations.parse 独立。

确认保存：只提交所选、勾选、有效且去重的关系，source 为 `ai_generate`。`RelationshipHistory.source` 是 String(16)，新增来源无需迁移；前后端可写枚举及显示标签必须一起更新。旧 ai_parse 来源不变。

前端：结果 lazy 注册到 TASK_RESULT_VIEWERS，按主体复用建议表，显示类别/状态/ID，允许编辑称呼、亲密度、描述。重开结果查已有关系并屏蔽；保存 409 后刷新已存关系并保留编辑。成功锁定按钮并由结果组件自身广播 `invalidateProject(pid, ['relationships'])`，不能只依赖 onSaved（任务面板不传该回调）。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 项目/选择角色/配置/任务不可访问 | 404，沿用当前归属规则 |
| 少于2或多于20、重复/非正整数ID、非法count或要求超限 | 422，安全中文响应，不回显完整输入 |
| 所选角色间已无缺失关系 | 创建时400；排队后发生则任务失败，均不调用模型 |
| 模型调用失败或整体结构非法 | failed，不写设定 |
| 合法空数组、仅不可保存建议 | 成功空/无有效项预览，不允许写入 |
| 同一角色对在预览后已建立或竞争唯一键 | 409 `这两个角色之间已有关系`，整批回滚 |
| 首次创建相同自定义称呼竞争类型唯一键 | 409 `关系类型已被其他请求创建，请重试保存`，整批回滚；重试可复用已提交类型 |
| 无关外键/其他完整性错误 | 保留原异常，不能误报成角色对或类型冲突 |

角色对冲突与类型冲突分别精确识别：SQLite 为 UNIQUE 错误码和完整列名；PostgreSQL/asyncpg 为 sqlstate=23505 加约束名（分别 `uq_relationships_pair_low`、`uq_relationship_types_project_id`，对应 0007）。适配器异常及其 cause 都需考虑。

## 5. 正例、基准与反例

- 正例：选择 A/B/C，3 条合法草稿取消1条、编辑1条，确认只写2条和历史；师父/徒弟按视角显示正确。
- 基准：relationships=[]，显示暂无可保存建议，计数不变；角色卡很长时20名身份ID仍全部存在。
- 反例：模型给未选 ID、A–A、A–B/B–A 重复或非文本状态，不可因默认化变成可保存条目。
- 并发：同一缺失角色对（预置或首次自定义类型）双请求为201/409，只写一份。不同角色对同时创建同一称呼时可为201/409，失败方重试后201，共用一个类型。
- 原子性：批次前几条创建了新关系/类型，后面冲突，前面的关系、类型、历史必须一起回滚。

## 6. 必需测试与证据

- `tests/test_relations_generate.py`：HTTP生成、草稿所有计数不变、同名按ID、完整身份预算、字段边界、模型原始类型、删除/已有/重复对、空/格式失败、取消重试、项目/角色/配置/任务权限、模板覆盖/锁定、真实知识库启用/关闭。
- `tests/test_relationships.py`：双请求 barrier 确保双方通过预查后再竞争；覆盖预置类型、自定义类型、相同/不同角色对、重试复用、混合批次回滚；pair/type冲突后外层SAVEPOINT仍可提交其他编辑；精确SQLite/PG约束识别与无关错误不误报。
- 前端 entityTasks/relationships/tasks 的 Vitest：unknown守卫、非法原始类型、方向、称呼编辑清type_id、勾选与所选范围过滤、反向去重、重开过滤、来源和API路径/任务跳转。
- 旧角色生成、旧备注解析、导入、向导、分析和智能体调用方回归；不放宽旧模板 parity，新 key 只精确排除 legacy 对照并补独立模板测试。
- 真正TCP/SSE验证可确认断开重接与并发HTTP结果，不能代替浏览器交互/主题/375px验证。能力不可用时明确记录未验证，不绕过设置。

## 7. 错误写法与正确写法

```python
# 错：只包 commit 会漏掉前置 flush；同时把外键错误也误报为角色对冲突。
try:
    await db.commit()
except IntegrityError:
    raise ConflictError(PAIR_CONFLICT)

# 对：在真正 flush 处精确翻译；低层不抢占外层事务的回滚所有权。
async with _pair_conflict_guard():
    await db.flush()
# resolve_type 对类型唯一冲突使用 _is_type_conflict，给独立的重试提示。
```

请求级 create/batch 捕获 ConflictError 后回滚并原样抛出；外层 SAVEPOINT/TransactionSession 则由既有所有者收口。不要为了恢复类型竞争随意添加裸 begin_nested：SQLite legacy 模式下最早物理写入若是 SAVEPOINT，RELEASE 可能使类型早于外层批次提交，破坏整批回滚。

```typescript
// 错：任务面板不传 onSaved，页面不会随保存更新。
onSaved?.()
// 对：结果自己广播，回调仅用于调用方的额外处理。
invalidateProject(projectId, ['relationships'])
onSaved?.()
```
