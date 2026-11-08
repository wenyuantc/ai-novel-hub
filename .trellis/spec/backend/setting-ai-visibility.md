# 设定 AI 可见策略与实际出站门禁

## 1. 范围 / 触发

新增从设定库读数据的写作、分析、向导、Agent 工具、检索或模型发送入口，以及修改设定身份、字段、关联、迁移、卡片、备份或效果恢复时，检查本契约。限制应用提供给模型的结构化设定，不改作者原文、人工档案、历史及授权，不清洗自由文本或撤回已发请求。

## 2. 签名

路由前缀 `/api/v1/projects/{pid}`，设定身份使用 `{kind: character|world|organization|career, id}`：

| 接口 | 合同 |
|---|---|
| `GET /setting-entries/{kind}/{id}/ai-settings` | `EntryAiSettingsOut`：档位、有效追踪开关、大小写、排除词、`ai_hidden_builtin_fields`、`hideable_builtin_fields`、`links: SettingEntry[]`、`ai_visibility_revision` |
| `PATCH /setting-entries/{kind}/{id}/ai-settings` | `expected_ai_visibility_revision` 加选填策略字段及 `links: EntityRef[]`；列表整体替换，省略不变、空数组清空、显式 `null` 拒绝 |
| `GET/POST/PATCH /setting-field-definitions[/id]` | `ai_hidden` 严格布尔，定义级作用于全部绑定；保留字段目录 CAS `expected_fields_revision` |
| `PATCH /setting-entries/{kind}/{id}/legacy-fields/{fid}/ai-visibility` | `{ai_hidden, expected_ai_visibility_revision, expected_timeline_revision}`；返回 `EntryFieldsOut`，不提交未保存的字段正文 |

`0037` 在四实体保存 `ai_mode`、`mention_enabled`、`mention_case_sensitive`、`mention_exclusions`、`ai_hidden_builtin_fields`；保留存量 `ai_mode IS NULL`，之后默认 `detected`。共享定义与旧独立字段分别在 `setting_field_definitions.ai_hidden`、`setting_text_fields.ai_hidden`；共享绑定行不能自定义该开关。`setting_ai_links(project_id, source_handle_id, target_handle_id)` 同项目真实 FK、去重、非自身、端点级联；GET 不创建句柄。项目持久化 `ai_visibility_revision` 与 `ai_history_epoch`，会话保存使用时的 `ai_history_epoch`。

## 3. 契约

- **资格：** `always`／唯一字面命中的 `detected`／作者显式引用先合并为直接根；`manual_only` 不因命中加入但可手动或经一跳加入，`never` 拒绝任何原因。仅从直接根展开作者链接及当前场景原有的有效成员／任职，不能从已展开集合再展开。普通字段引用、人物关系和模型猜 ID 都不增加资格。未配置旧条目在各入口保留既有注入、旧选角和名单；旧名单不当一跳来源。
- **投影：** `setting_ai_context.build_scope(...) -> AiScope`；普通块和规划保护段共享章初 `ChapterSettings`。分析也读取作者指定的 POV／引用；读规划和手动出场前后核对 `planning_revision`，不能混合两次保存状态。原始场景输入使用 [统一 matcher](./mention-tracking.md)，不扫描已渲染的设定、工具回显和向量结果。先按时点解析再用 `field_visible` / `entry_fields` / `ref_visible` 过滤，再做裁剪和拼接。隐藏字段的标签、选项、历史和派生值均不进入模型；例如隐藏 `career.stages` 后也不能从任职摘要发出阶段。身份由条目资格控制，不是字段开关。
- **读写：** 人工可读用 `get_manageable_project`，写需 `projects.edit` 或归属；按权限写屏障 → `lock_visibility_write` → 项目生命周期／CAS 次序。同事务提交资料与版本，等值不递增；配置变化递增 revision，可能缩小资格的变更（含候选竞争）递增 epoch，已知只放宽的不封存。新增限制或隐藏值改绑不能通过旧元数据写、卡片复用或效果恢复放宽。
- **出站：** 构建前读 SQL 标量 `VisibilityStamp(project_id, revision, epoch, last_deleted_at)`。`guard_provider(provider, stamp, sessionmaker)` / `guard_embedder(...)` 保留构建戳，每个真实 HTTP 发送从独立事务验戳，不能给旧负载重签。PostgreSQL 项目级事务共享／独占 advisory lock；SQLite 实体 no-op UPDATE。响应头即释放，异常／取消兜底；新尝试、重定向、fallback 不绕过。无结构化设定的独立试写等不需要策略。更新前排空旧执行器，不混跑版本。
- **场景写作：** `guard_provider(..., verify=callback)`在原真实发送lease内补查SceneContextStamp，不替代visibility检查。该stamp覆盖目标章内容／位置、项目规划和时间线／字段版本；只有可见性不变而目标正文已变也拒绝旧负载。前章普通正文只是构建时背景，不绑定其他章内容版本误伤合法并行。最终采纳／保存再次核验；首轮发送后作者收紧never不是普通续写warning，当前未提交章保留原稿。原始Scene规划只供作者，POV和引用经AiScope后才能进入锁定块。见[场景合同](./scenes.md)。
- **历史：** Agent 在本轮作者输入上取资格并在搜索／分页前过滤；只用投影或最小写回执。收紧后有历史的旧 epoch 会话 `history_sealed`，历史仍能读，不再摘要、重放、确认后继续；无历史的空会话可采用当前代次。构建期冲突时可安全重建的首轮任务重新组装；带旧派生上下文的续跑停止，已有章节及副作用不回滚重做。
- **格式：** 策略最初由v6／0037保存，当前项目备份为v8／0040，两端句柄仍显式重映射，不复制项目代次或Agent会话。卡片仍v6，包内`ai_settings/ai_link_refs`、字段flags和兼容角色的`legacy_always_in_context`随新建项，包外引用需显式映射；复用不改策略，定义复用必须匹配可见性。旧格式不接受夹带会被丢弃的新保护资料，人工文件仍含原值。当前effects v6，visibility兼容阈值保持5，合法v5不能被误判为缺策略。
- **前端：** 四种详情使用 `SettingAiPanel` 和 `api/settingAiVisibility.ts`；与详情外层独立草稿。409 后保留草稿，读取对照并显式采用新版本；列表逐项合并作者真实增删，不能把最新的隐藏字段一起取消。网络失败可重试，撤权不提交旧请求。字段页、旧字段、备份／卡片和 Agent 封存提供各自明确反馈。

## 4. 校验与错误

| 条件 | 结果 |
|---|---|
| 非法布尔／枚举／字段键、空排除词、超过 20 项或 80 码点、自关联或目标重复／超过 32 项 | 422，无写入；短语按匹配语义稳定去重 |
| 越作品或失效身份；仅可读用户修改 | 404；同项目有读无写为 403 |
| 配置 CAS 过期 | 409 `stale_revision`，`scope=ai_visibility`、`current_revision`；不得悄悄换版重试 |
| 显式 AI 任务要求禁用条目／隐藏主字段 | 400 `setting_not_available`；工具侧不暴露目标详情 |
| 实际发送代次不同／写屏障超时 | 409 `ai_visibility_changed`／`ai_visibility_busy`；旧负载不发送，失败保存无部分副作用 |
| 封存会话试图继续 | 409 `ai_history_sealed`；人工详情仍可读 |
| 卡片 v6 无确认代次／外部映射不全 | 400 `ai_visibility_revision_required`／不允许导入；字段、分类、AI 三版本校验 |
| 恢复会覆盖后加限制／旧格式带无法保留的保护 | 409 整笔撤销冲突／400 格式错误，不能丢弃保护后继续 |

## 5. Good / Base / Bad

- Good：A 被作者选中，A→B、B→C 只带 B；B 另有直接资格才展开 C。B 为 `never` 时不发送 B 名称、普通引用或档案。
- Base：旧记录仍显示旧主角／名单和世界观行为；新建无命中不自动加入。作者页面仍能查被隐藏的 20 章末事件，章初 AI 预览没有它。
- Bad：把人工 `EntryFieldsOut` 或旧 Agent tool JSON 原样拼回消息，或只在建任务时验一次版本。

## 6. 必需测试

- `test_setting_ai_visibility*.py`：两库迁移与级联、合法旧默认、作者 CAS / 权限／越项目原子拒绝、v6 往返和 effects v5 安全撤销。
- `test_setting_ai_context.py`、`test_mentions_matcher.py`：真实消息／向量出站正例和独立负例、章初／章末、九种类别与码点、同名／排除／一跳、收紧中断与安全重建。
- `test_ai_visibility_dispatch.py` 与 `test_agent_visibility.py`：本机三 SDK HTTP 发送、两连接竞态、超时／取消／重试，响应头后流未完仍可保存。去掉发送前钩子，原子保存的负控制必须失败。已记录旧工具值不允许重发。
- 卡片专项覆盖 SQLite 和隔离 PG、三 CAS、同名 ref、包外显式映射、复用不覆盖；前端覆盖独立草稿、只读、迟到响应、版本对照、九类深链和封存提示。真实浏览器与自动化证据分开记录。

## 7. Wrong vs Correct

```python
# Wrong：读取人工档案并在拿到新版本后替旧消息续签
messages = render(full_settings)
await provider.generate(messages, model)

# Correct：从构建前绑定的范围解析、投影和发送，每次传输验同一个戳
scope = await build_scope(db, project_id, texts=(instruction,), explicit=author_refs)
messages = render(projected_settings(scope))
guarded = guard_provider(provider, scope.stamp, visibility_sessionmaker(db))
await guarded.generate(messages, model)
```
