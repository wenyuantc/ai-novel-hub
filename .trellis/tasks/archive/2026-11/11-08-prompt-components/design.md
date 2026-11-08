# 设计：提示词可复用组件

## 1. 边界

- 新增：`models/prompt_components.py`、`schemas/prompt_component.py`、`services/prompt_component_service.py`、`api/v1/prompt_components.py`、`ai/components.py`（纯函数：解析与展开）、迁移 `0042_prompt_components.py`。
- 改动：`prompt_service`（保存／导入／预览／回滚／导出／运行时取覆盖）、`skill_service`（保存、导入、导出、快照）、`workshop_service`（分享模板覆盖）、`agent/tools/skills.py`（`load_skill`）。
- 前端：新增组件库页（放在「提示词模板」页旁，独立路由 `/prompt-components`）、`api/promptComponents.ts`、类型；`PromptTemplatesPage` 与 `SkillEditDrawer` 显示「引用的组件」。

## 2. 数据

```text
prompt_components
  id PK
  user_id FK users ON DELETE CASCADE, index
  slug  String(48)   ^[a-z0-9][a-z0-9-]{0,47}$   UNIQUE(user_id, slug)
  name  String(100)
  description String(500) default ''
  content Text  ≤ 20000
  revision Integer default 1   # 每次内容／元数据变更 +1，CAS
  created_at / updated_at
```

不建引用关系表：引用方只有本人 ≤100 个 Skill + ≤31 个覆盖，查询时现扫，避免两份事实不一致。

## 3. 语法与展开（`ai/components.py`）

```python
REF_RE = re.compile(r"\{\{>\s*([a-z0-9][a-z0-9-]{0,47})\s*\}\}")
def find_refs(text) -> list[tuple[str, int]]          # (slug, 行号)
def expand(text, contents: dict[str, str]) -> str     # 未知 slug → ComponentRefError(slug, line)
```

- `{{>` 在 Jinja 中是语法错误，现有覆盖不可能包含它，所以不会误伤旧数据；Skill 正文是纯文本，同理旧正文不含此写法（迁移不需要改数据，测试会扫默认模板与内置 Skill 确认）。
- 组件内容中出现 `REF_RE` 匹配 → 组件保存时 400「组件内不能引用其他组件」。
- 展开为单次文本替换，不递归。

## 4. 模板覆盖

- `prompt_service` 中所有进入 `templating.validate` 的入口（保存、导入、预览、回滚）先 `expand`，再校验展开结果；校验问题的行号映射回原文：展开结果中落在组件区间内的问题，message 前加「组件 {slug}：」，line 取引用所在原行。实现：`expand` 同时返回区间表 `[(start_line, end_line, slug, origin_line)]`。
- 覆盖库内仍存原文（含引用），版本历史、outdated 三方对比都对原文进行。
- 运行时：`get_override` 返回的 `PromptOverride` 改为已展开内容（在同一 DB 会话中读组件），build 函数无需改动；展开失败（理论上被 R5 拦住，仅竞态）→ `TemplateRenderError`「提示词模板「X」引用的组件 y 不存在，请检查」。
- 预览接口响应增加 `components: [{slug, name}]`。

## 5. Skill

- 保存／导入：`find_refs` 校验全部存在且属于 owner；全局 Skill（`owner_user_id is None`）有任何引用 → 400。
- `snapshot_of(skill)` 改为 `async snapshot_of(db, skill)`：展开后截断到 `SNAPSHOT_LIMIT`。所有调用点（generation、batch、rewrite、scene、section、agent forced）随之调整。
- `load_skill`：返回展开后的正文再分页。
- 导出：SKILL.md 正文用展开后文本（D5）。

## 6. 引用完整性（同一事务内）

- 删除组件／修改 slug：先扫引用方，非空 → 409 `code=component_in_use`，`extra.usages=[{kind: skill|override, id/key, name}]`。
- 修改内容：对每个引用它的覆盖，用新内容展开并 `validate`；任何失败 → 400 `code=component_breaks_usages`，`extra.failures=[{key, name, errors}]`。Skill 正文是纯文本，无需重新校验。
- 并发：组件更新带 `revision`，不符 → 409 `stale_revision`（沿用笔记的 CAS 写法）。覆盖保存与组件修改的竞态以运行时兜底错误处理，不加跨表锁。

## 7. 接口

| 方法 路径 | 说明 |
|---|---|
| `GET /prompt-components` | 列表 + `usage_count` |
| `POST /prompt-components` | 新建，201；上限 200 |
| `GET /prompt-components/export` / `POST /prompt-components/import` | `ainovel.prompt-components` v1，≤1MB，逐项 `created/updated/unchanged/invalid` |
| `GET /prompt-components/{id}` | 详情 + `usages` |
| `PUT /prompt-components/{id}` | 带 `revision` |
| `DELETE /prompt-components/{id}` | 204 / 409 |
| `GET /prompt-templates/{key}`、`GET /skills/{id}` | 响应增加 `components: [{id, slug, name}]` |

他人组件一律 404。权限与模板覆盖一致：仅 `CurrentUser` 本人资源，不新增 RBAC 动作（`api/v1/prompt_templates.py` 同样无动作授权）。

## 8. 工坊

`workshop_service` 分享 `kind=prompt` 时，取覆盖内容先展开再写入快照。

## 9. 兼容与回滚

- 迁移只新增表；回滚 drop 表。旧数据无引用，行为不变；parity 测试不变。
- 风险：覆盖保存后组件被改坏 → 由 R5 内容校验阻断；仍有竞态时运行时报可读错误而非静默。
