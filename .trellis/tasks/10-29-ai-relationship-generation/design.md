# AI 生成人物关系信息：技术设计

## 架构与边界

新增 `relations.generate`，复用 `EntityHandler`、`entity_ai_service`、后台任务引擎与前端 `EntityTaskModal`。任务只生成草稿；保存继续使用现有 `relationships/batch` 和 `relationship_service.apply_change`。本功能是同一条跨层交付链，保持一个任务。

路由只解析 schema 并调用任务服务；归属、角色选择、剩余角色对及 AI 配置校验放在 service。LLM 调用沿用 `resolve_config` / `build_provider`，不新增 Provider 路径。

## 接口契约

### 创建任务

`POST /api/v1/projects/{project_id}/relationships/generate` → 202 `TaskOut`。

```json
{
  "character_ids": [11, 12, 13],
  "count": 10,
  "instruction": "围绕主角的成长建立亦师亦友的关系",
  "ai_config_id": null
}
```

- `RelationsGenerateRequest` 放在 `schemas/entity_ai.py`：`character_ids: list[int]`（2–20 个不同正 ID）、`count: int = 10`（1–50）、`instruction: str = ""`（≤2000）、`ai_config_id: int | None = None`。
- 不按名称/别名猜测身份，不把重复 ID 静默去重后当作有效请求。
- 先查项目归属，再按项目 ID 限定查询选择集合，任一缺失或不属于该项目返回 404。
- 创建和运行阶段均检查所选角色与剩余关系对。有效期望数为 `min(count, 缺失关系对数)`；没有缺失关系时创建请求返回 400，不调用模型。
- `kind = relations.generate`，标题 `AI 生成关系`，`resource_key = None`（与生成类草稿任务一样允许并发）。`TaskOut.character_id = None`；多选存 `input.request.character_ids`，不借用单角色字段。
- 与同类创建端点一样不提供幂等键，重试创建请求可能生成另一草稿。前端用 starting/running 防重复操作；生成本身不写关系。

### 草稿结果

通过任务 `done.result` 和已完成任务快照读取：

```text
RelationsGenerateResult {
  character_ids: number[]
  suggestions: GeneratedRelationSuggestion[]
  warnings: string[]
}
GeneratedRelationSuggestion = 现有 RelationSuggestion + {
  subject_id: number | null
  subject_name: string
}
```

保留既有 target 身份、`resolved/reason`、`existing_relationship_id`、双方称呼、`subject_is_a`、类型引用、亲密度、状态和描述。有效行的 subject/target 必须同时属于原选择与当前项目且不同；无效行不能勾选。名字由服务端查询或合法快照提供，仅供显示。

前端在 `api/types.ts` 镜像请求/结果，在 `api/entityTasks.ts` 用 unknown 守卫统一收窄；组件不强转原始载荷。

### 保存与历史来源

继续 `POST /api/v1/projects/{pid}/relationships/batch {items}` → 201 `RelationshipOut[]`。条目沿用 `RelationshipCreate`，新增可写来源 `ai_generate`；同时扩展 `RELATION_SOURCES`、`ApiRelationSource`、前端 source 联合与历史标签。

`RelationshipHistory.source` 已是 `String(16)`，新增来源无需迁移。旧 `ai_parse` 语义不变，不把新生成记录标成解析备注。

保存经 `create_relation → resolve_type → apply_change`，不直接改 ORM、不提前创建类型。使用匹配类型时按 `subject_is_a` 决定 a/b；编辑称呼后清 type_id，以主体为 role_a 重新匹配（含反向）或创建类型。

保留单事务语义。预览后出现重复关系返回 409、整批回滚。UI 不自动改为更新或静默部分写入，保留草稿供调整。唯一约束继续兜底；测试确认重复/过期冲突保持中文 409。

实施时的真实并发验收揭示既有缺口：唯一约束可能在 `apply_change` 的 flush 抛出，早于原 `_commit` 的捕获。最小修复使用精确的角色对约束识别：统一写入口只翻译该冲突，不主动回滚；拥有请求事务的 create/batch 与 commit 边界负责回滚，外层分析/导入/智能体 SAVEPOINT 保留事务控制权。无关完整性错误不误报成角色对冲突。此修复落实 R3，不改变产品范围；回归及 TCP 复测见 research/runtime-verification.md。

独立检查进一步发现首次创建同名自定义类型的竞争。`resolve_type` 精确识别类型唯一键，返回独立中文 409“关系类型已被其他请求创建，请重试保存”，仍由请求或既有外层事务回滚。不增加裸 SAVEPOINT（SQLite legacy 模式可能提前释放并提交类型），不引入方言 upsert。不同角色对共享新称呼的竞争，允许一方收到该提示后重试并复用已创建类型；同一角色对保持只保存一份。该语义已通过确定性回归和真实 TCP 双客户端验证。

### 错误矩阵

| 场景 | 行为 |
|---|---|
| 未登录 | 沿用 401 会话处理 |
| 项目或所选角色不可访问 | 404，统一中文说明 |
| 角色数、重复 ID、count 或 instruction 不合法 | 422，沿用安全校验响应 |
| 已无缺失关系 | 创建时 400；排队期间发生则任务失败，不调用模型 |
| 无 AI 配置或端点不可用 | 沿用现有创建校验与中文提示 |
| 模型调用/整体 JSON 结构失败 | 任务 failed，不写设定 |
| 合法空关系列表 | succeeded，空预览与明确提示 |
| 同批重复、自指、未选/删除角色、已有关系 | 不可保存行或汇总警告 |
| 保存时冲突 | 409，整批回滚，保留草稿 |

## 上下文与提示词

新增 entity 分类模板 `relations.generate`、system/user 两个 j2 文件、`build_relations_generate_messages` 与格式常量。与 `relations.parse` 独立。

变量包含作品信息、世界观、所选角色资料、已有关系、可见类型参考、有效期望条数、作者要求、知识库参考，以及锁定的 json_rule / output_format。

- 身份清单包含全部所选 ID/姓名/别名，不裁身份；详细卡片逐名预算（建议每名最多 800 字）。优先复用 `context_builder.format_card` 与组织成员查询。
- 世界观沿用 4000 字预算；已有关系用 `relations_among` / `describe_relation`。保留全部已占用角色对的 ID，先裁描述再裁辅助资料。
- 知识库沿用 `knowledge_for_project`，查询加入作者要求、所选名字和作品信息；为空时不输出空段，与设定冲突时以设定为准。
- 不加载全书正文，不使用旧备注解析的名字匹配作为身份依据。
- 原始输出固定为 `relationships` 数组，每项有 `a_id` / `b_id`、`role_a` / `role_b`、category、intimacy、status、description。只允许给出的所选 ID，禁止自指、已有关系和重复对，不要求完整角色对笛卡尔积。
- 规范化依据原选择和当前库再次核对身份、无序对、类型和字段。合法建议数不超有效期望；无效/重复项给出原因或汇总警告。非法整体结构不能伪装成合法空结果。
- 新 kind 显式加入 job/result 分支，防止落入现有 organization/relations.parse 默认分支。

注册后模板数量 30 → 31。新 key 没有 legacy 对照，补独立内容、锁定变量和覆盖测试；在 `test_every_legacy_key_is_covered` 中仅排除这个精确 key，不删除或放宽旧 parity 用例。

## 前端交互与复用

1. 关系页工具栏加“AI 生成关系”，不足两名角色禁用。扩展 `EntityTaskModal` 提供多选、条数、要求与配置；运行时禁用冲突输入，显示进度、输出和取消。
2. 弹窗按 `project + kind + active` 重接最新活动任务；关闭只断订阅。任务标签/跳转增加 `relations.generate → /relationships`。
3. 新 `RelationsGenerateResultView` 经 `TASK_RESULT_VIEWERS` lazy 注册，弹窗和任务面板使用同一预览。
4. 按主体分组复用 `RelationSuggestionList`，呈现双方、称呼、类别、状态、亲密度和描述。保留已有编辑/选择逻辑，必要时兼容补充只读状态显示，不复制整表。
5. 复用 `suggestionTypeRef` / `isSavable` / `suggestionsToBatch`；来源显式传 ai_generate，旧调用默认仍为 ai_parse。最终仅提交勾选有效条目并再次按无序对去重。
6. 预览加载/重开时查询现有关系，屏蔽已存在建议；服务端仍作最终校验。未保存编辑仅本地保存，不新增跨会话编辑持久化。
7. 成功后锁定提交按钮，调用 `invalidateProject(pid, ['relationships'])` 与可选 onSaved；相关 loader 已订阅此资源。任务面板无 onSaved 时也可刷新。

沿用 AntD 中文、主题 token、App.useApp 与响应式布局，表格局部滚动，375px 不发生页面级溢出。图谱库和新结果组件保持 lazy，不加依赖或全局 store。

## 兼容性与回滚

新增接口、task kind、prompt key 和来源，保留旧请求/结果结构。手动编辑、角色生成建议与旧备注解析必须回归；项目备份/导出沿用来源字符串读取，不丢新来源。

无迁移。回滚前先让在途新 kind 完成或取消，保留已确认关系与 ai_generate 标签读取兼容性；以功能提交为回滚点，不删除用户数据。
