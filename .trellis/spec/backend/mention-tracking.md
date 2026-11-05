# 全文提及、来源定位与作品笔记

来源任务：`11-05-mention-tracking` 及两个子任务。跨层合同适用于后端提及服务、笔记、世界观别名及前端编辑／阅读／聊天／资料定位。

## 1. 范围与触发

- 追踪对象仅为角色及 `WorldSetting.category` 为 `place/item` 的条目；名称／别名是当前全书身份资料，不进入按章剧情变化。
- 四类原文：`Chapter.content`、当前会话所有者的已保存 user/assistant `AgentMessage.content`、`ProjectNote.content`、`KbDocument.text`。不扫描 tool JSON、会话摘要、Skill 快照、章节历史版本、分块回显或灵感 notes 数组。
- 提及是字面命中，不推断真实剧情在场。章节热力图只统计已保存正文；草稿、聊天、笔记和资料不能折算为章节出场。
- 无模型调用、无向量依赖、无持久提及副本表；原文与词典是事实源，有界缓存只复用匹配跨度。

## 2. 签名与存储

后端入口：`app/api/v1/mentions.py`；公共类型：`schemas/mentions.py`；实现：`services/mentions/{context,matcher,sources,query,summary,window}.py`。所有下列路径位于 `/api/v1/projects/{pid}`。

| 接口 | 合同 |
|---|---|
| `GET /mentions/catalog` | `{entities, dictionary_hash, available_source_types}` |
| `POST /mentions/preview` | `{text}`（≤500000码点）→ `{spans, content_hash, dictionary_hash}`；只读预览 |
| `GET /mentions` | `entity_kind?,entity_id?,source_type?,chapter_from?,chapter_to?,page=1,page_size=20` → `{items,total,page,page_size,source_counts,dictionary_hash}` |
| `GET /mentions/summary` | 另支持 `category?,q?`，服务端筛选条目后分页 → `{chapters,entries,total,page,page_size}` |
| `GET /mentions/sources/{type}/{id}` | `start=0,end?,content_hash?` → `SourceWindow`，原文窗口≤8000码点 |
| `GET/POST /notes` | 分页搜索 `q/page/page_size`；创建 `{title,content?}` →201完整笔记 |
| `GET/PATCH/DELETE /notes/{id}` | PATCH `{expected_revision,title?,content?}`；DELETE查询参数 `expected_revision` →204 |

分页参数：page≥1，page_size 1–100。`entity_kind = character/world`，`category = character/place/item`，`source_type = chapter/chat/note/knowledge`。

- `EntityRef {kind,id,name,category,aliases}`；`MentionSpan {start,end,text,entities}`。
- `SourceRef {type,id,title,chapter_no|null,conversation_id|null,created_at|null}`；`Occurrence` 在 span 上增加 source、content_hash、context_before/context_after。
- summary 条目有 entity、mention_count、ambiguous_count、chapter_count、last_chapter_no、gap_chapters、稀疏 counts（chapter_id/count/ambiguous_count）；chapters 保留 has_content。
- `SourceWindow {source,content_hash,stale,window_start,text,spans,target_start,target_end}`，spans 与 target 均为全文绝对码点位置。
- 迁移 `0032`：`project_notes(id,project_id,title,content,revision,created_at,updated_at)`，project_id CASCADE，revision默认1。列表仅取180字 excerpt，不取长正文。
- 迁移 `0033`：`world_settings.aliases` JSON默认`[]`；API复用 `Aliases`（去空/去重、≤10项、每项≤50字），PATCH省略/null不改、`[]`清空。

## 3. 关键合同

### 匹配与缓存

- 原文不 trim/normalize；所有位置为 Python Unicode 码点半开区间 `[start,end)`。前端 `utils/mentions.ts::mentionSegments` 用 `Array.from(text)` 按码点分段；只有需要原生Textarea选区时才另用 `utils/textOffset.ts` 转换UTF-16。
- 字面精确、区分大小写；词表去首尾空白并去重，一字词仍参与。ASCII字母/数字/下划线词边检查相邻字符，避免 Ann 命中 Anna。
- 标准库 `re` 转义词表，左起最早有效位置、同起点长词优先，消费后不重叠。长词边界不满足时仍可回退合法短词。
- 同词多条目返回全部候选；只给 `entities.length===1` 累加确定次数；候选分别记 ambiguous_count。名称与别名同词不重复计数。
- 词典指纹包含 matcher版本、实体kind/id/name/category和规范化词集合；缓存键包含 project/type/id、原文SHA-256和词典指纹。缓存不存原文、不存授权、不存旧章号元数据。
- `MentionCache` 默认≤4096来源、总≤200000跨度、单来源≤20000跨度；超限来源完整流式匹配但不缓存，禁止截断后声称完整。零命中也可缓存，仅完整消费来源后写缓存。`tests/conftest.py` 在测试前后清理缓存。
- 来源分批游标：资料每批1份，其余16份；hash/匹配/聚合放 `anyio.to_thread.run_sync`。分页只保留当前页片段，但统计完整筛选范围；读取不 autoflush/commit 调用方未提交业务对象。

### 权限与统计

- 每次查询先调用 `get_manageable_project` 校验人工读取权限和作品生命周期；然后枚举当前可见来源，最后才使用缓存。
- chat 只对真实项目所有者开放，查询再次 JOIN 会话的 project_id/user_id。非owner管理员的 catalog 不含 chat，固定四键 source_counts.chat=0；显式请求 chat 返回404，不泄漏真实数量。
- 章节范围只限定 chapter 来源；统计格子抽屉必须显式 `sourceType="chapter"`，不能只给 chapterFrom/To 后混入聊天。
- summary 的确定次数、最近章节和 gap 全部按同一筛选范围计算；gap为最后确定提及之后的非空章节数，范围内从未确定提及为null。空章has_content=false，显示“未写”；缺失章号不合成章节。
- 世界观改名／别名、正文改写、AI保存、版本恢复、删除、导入、改章号后，重新查询读取当前原文与元数据；知识库按解析全文计数，不加总重叠分块。

### 跳转与草稿

- `utils/mentions.ts` 统一生成 source URL，参数为 `mention_start/end/hash`，不把原文片段塞进URL。正文到Reader、聊天到overview指定conversation_id/message_id、笔记到notes?id、资料到knowledge?document_id。
- SourceWindow hash不一致返回stale=true、target=null以及当前文本开头窗口；不能静默高亮旧偏移。位置有效但过大/越界按422拒绝。
- 编辑器与笔记的预览使用当前本地content，切换视图不提交、不清草稿；AI流期间不把未保存流计入索引。Reader使用原文范围，定位优先于阅读进度恢复。
- `MentionText` 只渲染React文本节点与按钮；歧义弹候选，点击打开同页 `EntityPreview`。进入完整设定仍遵守原有离页保护。
- 聊天选择真实会话/消息，用原文定位窗口避免Markdown字符偏移错误；定位期间暂停自动粘底。资料按document ID+原文位置定位，不依赖可重建的chunk ID。
- `useMentionQuery` 的状态key包含作品、用户/授权快照、查询、刷新代次；清理时abort，旧响应不回填。`useEntityDeepLink` 区分undefined（未成功加载）与真实空列表；加载失败不消费?id，重试保留目标。提及列表重试同时重试catalog与结果。
- 热力图最多50章可视窗口，条目服务端分页；提供时间线和逐章数值表、键盘按钮及主题token。确定次数、歧义、零值和未写分别可读。

### 笔记与兼容

- 笔记标题trim后1–200字，正文≤200000码点且可空；PATCH省略字段不变，显式null拒绝，未知字段拒绝。revision是严格正整数。
- 写入先锁作品并校验projects.edit，按project_id+id读取笔记；UPDATE/DELETE用expected_revision数据库CAS，冲突409保留前端草稿；成功更新revision+1。创建请求不自动重试。
- 笔记页支持?id及?new=1；新建成功替换为真实?id，不触发放弃新笔记草稿确认。关闭、跨笔记、跨页及刷新保留现有草稿保护。
- `project_notes` 原文进入项目备份RULES；旧备份无表合法；world_settings.aliases登记json_plain；APP_SCHEMA=0033。导入验证别名约束、笔记标题/正文上限及正revision。
- 名称/别名仍走设定元数据revision守卫，不写剧情变化。旧v2智能体效果缺aliases时，只有当前值仍为迁移默认[]才兼容恢复；作者已新增别名必须报冲突，不静默抹除。
- 本次未增加笔记AI工具、自动注入、收藏/固定/并排工作台或开放API端点。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 无权作品、跨作品条目/来源、已删除来源、非owner请求chat | 404 |
| 只给entity_id不给kind、章范围倒置、无效枚举/分页/位置/hash格式、超限预览 | 422 |
| 原文hash变化 | 200，stale=true、target=null，UI提示重新定位 |
| 笔记expected_revision过期 | 409，code=stale_revision，保留输入 |
| 笔记空白标题、超限文本、显式null或未知字段 | 422 |
| 查询当前没有命中 | 空列表/total=0；不是索引失败 |
| 首次目录/条目加载失败 | 显示重试，保留深链，不误报条目已删除 |

## 5. Good / Base / Bad

- Good：`😀 林远。` 的“林远”范围是码点[2,4)，前端换算后高亮，新增文字后旧hash提示失效。
- Base：同词“阿远”归属两角色，显示候选与歧义次数；不调用模型猜测。
- Bad：把资料分块命中相加；把工具回显算聊天；将管理员作品管理权限当私有聊天授权；改稿后用旧offset直接选区。

## 6. 必需测试

- `test_mentions_matcher.py`：Unicode、大小写/ASCII边界、长词失败后短词回退、一字词、候选去重、指纹及缓存超限完整性。
- `test_mentions.py`：四源全量分页、范围/空章/歧义、源与词典变化、删源/改号、AI保存/恢复、私有聊天、跨作品、非法查询、窗口上限、异步线程与不autoflush。
- `test_mention_compat.py`：旧/新备份、非法新值拒绝、智能体旧v2效果与新增别名冲突、0033迁移；`test_notes.py` / `test_notes_migration.py`：CRUD/CAS并发/权限/回收站/备份/0032。
- `test_mentions_scale.py`：500万码点正文+50万码点资料，HTTP全量计数正确并记录冷/热扫描，不把该合成基准当最大规模保证。
- 前端 `api/{mentions,notes}.test.ts`、`utils/{mentions,mentionStats}.test.ts`、笔记/标记组件测试、世界观表单与只读测试。真实浏览器验证Unicode定位、未保存草稿、歧义候选、四源跳转、旧链接、375px/桌面/主题/键盘，以及刷新长聊天后定位不被粘底抢走。

## 7. Wrong vs Correct

```python
# Wrong：无权或已删除来源也可能从缓存返回旧片段
return cached_hits[project_id]

# Correct：先拿当前可见原文，再用原文和词典身份复用跨度
context = await query_context(db, actor_id, project_id)
# iter_documents 还会对聊天按会话归属过滤；调用方在线程中消费cache迭代器
```

```tsx
// Wrong：网络失败被默认空数组伪装成“条目不存在”并消费深链
useEntityDeepLink(pid, data ?? [], loading, open, canReadNow)
// Correct：保留“尚未成功加载”的状态和重试目标
useEntityDeepLink(pid, data, loading, open, canReadNow)
```
