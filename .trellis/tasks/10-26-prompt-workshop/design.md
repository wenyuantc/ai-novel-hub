# 设计：实例内提示词工坊

## 边界

- 新增 `app/models/workshop.py`、`app/schemas/workshop.py`、`app/services/workshop_service.py`、`app/api/v1/workshop.py`，迁移 `0020_prompt_workshop`。
- 复用而不改既有调用语义：`style_service.clean_fields` / `FIELD_LIMITS` / `unique_name`，模板注册表与 `prompt_service.ensure_valid` / `apply_save`。私有辅助函数提为公开函数，`apply_save` 的新增 `force_version` 默认为 false，原保存与文件导入行为不变；工坊显式开启。
- 工坊是用户级资源，不属于项目，不登记进项目备份注册表（`project-transfer.md` 只要求登记项目相关表）。

## 数据模型（迁移 0020）

`workshop_items`（在架内容，只保存最新通过的版本）

| 列 | 说明 |
|---|---|
| id | PK |
| kind | `style` / `prompt` |
| prompt_key | kind=prompt 时的模板 key，否则 NULL；索引 |
| name / description | ≤100 / ≤500 |
| category | 固定枚举，见下 |
| tags | JSON 数组，≤5 个，每个 1–12 字，去重 |
| content | JSON。style：`STYLE_FIELDS` 去掉 name 后的字段；prompt：`{system, user}` |
| author_id | FK users **SET NULL**，可空（D4） |
| is_anonymous / is_official | bool |
| status | `active` / `hidden` |
| hidden_by | `author` / `admin` / NULL |
| like_count / download_count | int，默认 0 |
| published_at | 首次上架时间；`updated_at` 用 TimestampMixin |

`workshop_submissions`（新条目或更新的审核单，也作为作者的提交历史）

| 列 | 说明 |
|---|---|
| id | PK |
| item_id | FK workshop_items **CASCADE**，NULL = 新条目 |
| submitter_id | FK users **CASCADE**（删号时提交一起删，D4） |
| kind / prompt_key / name / description / category / tags / content / is_anonymous | 同条目 |
| status | `pending` / `approved` / `rejected` / `cancelled` |
| review_note | ≤500，驳回必填 |
| reviewer_id | FK users SET NULL |
| reviewed_at | |

部分唯一索引：`item_id` 在 `status='pending'` 时唯一（同一条目最多一份待审核更新；PG / SQLite 都用 `*_where`，参照 `uq_skills_global_slug`）。

`workshop_likes(item_id, user_id)`、`workshop_imports(item_id, user_id)`：复合主键，两列都 CASCADE。

分类常量 `WORKSHOP_CATEGORIES`：`general` 通用、`xuanhuan` 玄幻仙侠、`wuxia` 武侠、`romance` 言情、`scifi` 科幻、`suspense` 悬疑、`history` 历史、`urban` 都市、`other` 其他（文案自写）。

上限：每个用户待审核提交 ≤20；模板每段沿用 `PART_LIMIT`；风格字段沿用 `FIELD_LIMITS`；请求体整体 ≤1MB。

## 关键流程

**提交** `POST /workshop/submissions`：body `{kind, item_id?, name, description, category, tags, is_anonymous, content | prompt_key}`。
- 风格：前端从自己的风格预填 content；服务端只接受内容，按风格字段规则清洗。
- 模板：key 必须在 `TEMPLATES`；`validate()` 有错误 → 400 带错误列表；与系统默认相同 → 400。
- `item_id` 有值：条目必须是本人的（否则 404），kind / prompt_key 必须一致；已有 pending → 409。
- 提交人是管理员：同一事务内直接通过（新条目 `is_official=True`）。

**审核**（`AdminUser`）：`approve {category?, tags?}`：新条目 → 建 item（author=提交人，status=active，published_at=now），回写 `item_id`；更新 → 用提交内容覆盖条目的 name / description / category / tags / content / is_anonymous，计数不动；若条目处于作者下架状态，保持原状态（更新不等于上架）。`reject {note}` 必填原因。只能审 pending，否则 409。

**下架 / 上架**：`POST /items/{id}/hide`（作者或管理员，记录 hidden_by）、`POST /items/{id}/unhide`（作者只能恢复 `hidden_by=author` 的，管理员都可以）。`DELETE /items/{id}` 仅管理员。

**管理员改元数据**：`PATCH /workshop/admin/items/{id} {category?, tags?}`——不提供改正文的接口（D3）。管理员直接发布 `POST /workshop/admin/items`，等价于自动通过的提交。

**点赞**（R7，原子）：`POST /items/{id}/like` 插入点赞行，主键冲突视为已赞（不报错）；只有真插入时才 `UPDATE ... SET like_count = like_count + 1`，同一事务。`DELETE /items/{id}/like`：`DELETE` 的 rowcount 为 1 时才减一。并发重复点赞由主键兜底。

**导入** `POST /items/{id}/import`：
- 风格：新建 `WritingStyle(user_id=me)`，名称用 `unique_name(name, taken, "（工坊）")`；返回 `{style_id}`。
- 模板：按当前 `TEMPLATES[key]` 重新 `validate`，失败 400，不写入；key 不存在 400。写入前保存当前生效内容：已有启用覆盖时补齐缺失的当前版本，无覆盖或覆盖停用时保存系统默认；停用覆盖的原正文也须保留。随后保存导入内容并设 `enabled=True`，即使正文相同也追加备注为「从工坊导入：<名称>」的版本，保证每次导入可追溯并能恢复导入前内容。
- 都在同一事务里插入 `workshop_imports`；真插入时 `download_count + 1`。
- 差异预览在前端完成：取 `GET /prompt-templates/{key}` 的当前生效内容和条目内容，交给 `PromptDiffModal`，确认后再调导入接口。

**浏览** `GET /workshop/items?kind&category&tag&prompt_key&q&sort=newest|likes|downloads&page&page_size≤50` → `{items, total}`。非管理员只看 active；`q` 匹配名称 / 说明（`ilike`，转义 `%_`）；`tag` 在 Python 侧难分页，改用 JSON 文本 `LIKE '%"tag"%'`（tags 入库前已规范化）。每项带 `liked`、`imported`（一次批量查询）。

**署名输出**：`author_name` 规则——author_id 为空 →「已注销用户」；匿名且查看者不是管理员也不是作者 → NULL（前端显示「匿名」）；否则用户名。管理员另得 `author_username`。

## 权限矩阵

| 情形 | 结果 |
|---|---|
| 非管理员读取 hidden 条目（不是作者） | 404 `条目不存在` |
| 读取 / 撤回别人的提交 | 404 `提交不存在` |
| 对别人的条目提交更新、下架 | 404 |
| 作者恢复管理员下架的条目 | 403 `该条目已被管理员下架` |
| 非管理员调用 `/workshop/admin/*` 或删除条目 | 403 |
| 审核非 pending 的提交、同条目重复 pending | 409 |

## 兼容与回滚

- 只新增表和接口，不改现有表；回滚 = 迁移 downgrade 删四张表 + 移除路由与前端入口。
- 提成公开的 style / prompt 辅助函数保持原有行为，现有测试作为回归。

## 前端

- `src/api/workshop.ts` + `types.ts` 镜像 schema；`pages/WorkshopPage.tsx`（Tabs：浏览 / 我的分享 / 审核），`components/workshop/`：`WorkshopItemCard`、`WorkshopItemDrawer`（详情、点赞、导入）、`ShareToWorkshopModal`（新提交 / 更新共用）、`ReviewPanel`。
- 路由 `/workshop` 懒加载；`AppLayout` 顶部导航加「提示词工坊」；审核 Tab 用 `user.is_admin` 控制显示，并显示待审核数。
- `StylesPage` 自己的风格、`PromptTemplatesPage` 已覆盖的 key 各加「分享到工坊」，打开 `ShareToWorkshopModal` 预填内容。
- 风格导入成功后刷新 styles store；模板导入走 `PromptDiffModal` 确认。
