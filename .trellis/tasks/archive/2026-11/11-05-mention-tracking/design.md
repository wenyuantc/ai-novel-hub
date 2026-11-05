# 技术设计

## 边界与选择
- 复用 FastAPI/SQLAlchemy/React/AntD，无新增匹配、编辑器或图表第三方依赖。
- 提及为原文的只读派生索引。首版读取当前权威文本、计算 SHA-256，与词典指纹及 matcher_version 组合复用有界进程内 LRU；不新增需与多个写入口同步的提及副本表。重启后重算，备份只保存原文。
- 按来源批量读取，CPU 匹配移到线程，不一次加载全书所有正文。每页查询的总数必须来自其完整筛选范围，不能把只扫描一页说成全量。来源筛选可缩小扫描范围，热力图只扫描章节。
- 已有最大规模为 3000 章/500 万字拆书，知识库最多 200×300 万字；首次扫描与 hash 仍有 IO 成本，必须测量，不能承诺 O(1)。缓存限制来源数与总 span 数，大来源不缓存仍返回完整分页结果；不能为了缓存上限截断统计。
- 若基准暴露不可接受阻塞，先优化批量读取、扫描器与缓存，必要时回报证据再更新设计；不靠降低验收范围通过。

## 数据与并发
### 笔记
ProjectNote / project_notes：id PK，project_id FK CASCADE/index，title varchar(200)，content Text，revision int 默认1，created_at/updated_at。纯文本最大 200000 码点；标题 trim 后非空。
PATCH/DELETE 需要 expected_revision，更新使用原子 CAS，成功 revision+1；冲突 409 {detail, code:"stale_revision"}。所有写入持有作品生命周期锁，真实 actor；读取与人工管理按 projects.read，写入按 projects.edit。POST 不自动重试。
迁移 0032_project_notes（down_revision 0031）；核心提及迁移 0033_world_aliases（down_revision 0032）。新增笔记原文登记项目备份 RULES；旧备份无笔记合法。无笔记 AI 工具或新增审计动作。

### 身份词
WorldSetting.aliases JSON 默认[]，Create/Update/Out 使用已有 Aliases 清洗；PATCH 省略/null 不改，[] 清空。UI 为 place/item 暴露别名；索引只纳入 character、place、item。世界观其他分类可保留该字段但不参与本期词典。
别名属于全书元数据；接入 setting_progression_service 的 metadata_changed/revision 守卫，不能改成剧情事件。world_settings 的 json_plain、APP_SCHEMA 和旧项目导入默认值同步；现有角色卡无需新增世界观格式。

## 匹配契约
EntityRef = {kind:"character"|"world", id:int, name:str, category:"character"|"place"|"item", aliases:str[]}
MentionSpan = {start:int, end:int, text:str, entities:EntityRef[]}
- 原始 Unicode 码点、半开区间 [start,end)，原文不 normalize/trim，匹配词只 trim/去空/同实体去重。前端 mentionSegments 用 Array.from 按码点分段；原生 Textarea 选区另由既有 textOffset 转换 UTF-16。
- 精确、大小写敏感；所有非空词包含单字。ASCII 字母/数字/下划线在词首/末时检查相邻 ASCII 同类边界，避免 Ann 命中 Anna。
- 从左向右取最早有效起点，同起点取最长词，消费整个范围，不输出重叠 span。同一词对应多个实体时同 span 返回全部候选，按 kind/id 稳定排序。
- 确定计数 = entities 长度1；多个候选只增加各候选 ambiguous_count，不增加确定次数；相同实体的名称和别名同词不重复。
- dictionary_hash 含 matcher_version、实体 kind/id/name/category/规范化词集合，稳定排序后 SHA-256；删条目、改分类、改名/别名必失效。缓存键含 project_id、source_type/source_id、content_hash、dictionary_hash；权限永远在缓存前检查。

## 四类来源
source_type = "chapter" | "chat" | "note" | "knowledge"。
- chapter：Chapter.content，当前 title/chapter_no 在查询时读取，避免缓存旧章号；不扫描摘要/版本/计划。
- chat：当前 actor 拥有的 AgentConversation 中已保存 user/assistant content；不扫描 tool、tool_calls、page_context、usage、Skill 快照或会话摘要。管理员管理他人作品时完全剔除此来源（计数也不返回）。
- note：ProjectNote.content；标题仅作来源标题，不重复扫描标题。
- knowledge：KbDocument.text 解析原文；忽略 inject/向量状态对展示的影响，追踪不触发模型；不加总重叠 KbChunk。
每次查询先验证作品可读及生命周期，再读取当前可见来源。缓存不能提供当前来源集合中已消失的资源。

## HTTP 合同
前缀 /api/v1/projects/{pid}，命名与项目现有 snake_case 保持一致，Pydantic 与 TS 同步。
| 接口 | 输入/输出 |
|---|---|
| GET /notes | q?,page=1,page_size=20(≤100) → {items:NoteSummary[],total,page,page_size}；summary 不含全文，含 excerpt/revision/time |
| POST /notes | {title,content} → 201 Note |
| GET /notes/{id} | → Note {id,project_id,title,content,revision,created_at,updated_at} |
| PATCH /notes/{id} | {expected_revision,title?,content?} → Note |
| DELETE /notes/{id}?expected_revision=N | → 204 |
| GET /mentions/catalog | → {entities:EntityRef[],dictionary_hash,available_source_types:SourceType[]}；权限允许的来源类型，不表达某来源是否已有数据 |
| POST /mentions/preview | {text} → {spans:MentionSpan[],content_hash,dictionary_hash}；≤500000码点，仅预览不持久化 |
| GET /mentions | entity_kind?,entity_id?,source_type?,chapter_from?,chapter_to?,page=1,page_size=20(≤100) → MentionPage |
| GET /mentions/summary | entity_kind?,entity_id?,category?,q?,chapter_from?,chapter_to?,page=1,page_size=20(≤100) → MentionSummary；category为character/place/item，服务端先筛选条目再分页；entity_id必须同时给entity_kind |
| GET /mentions/sources/{source_type}/{source_id} | start=0,end?,content_hash? → SourceWindow |

SourceRef = {type,id,title,chapter_no:int|null,conversation_id:int|null,created_at:str|null}。
Occurrence = {source:SourceRef,start,end,text,content_hash,context_before,context_after,entities:EntityRef[]}。
MentionPage = {items:Occurrence[],total,page,page_size,source_counts:Record<SourceType,int>,dictionary_hash}。计数为筛选实体后的 occurrence 数（歧义保留且标明）；source_counts 固定四个键，无权chat恒为0且客户端依据catalog.available_source_types隐藏聊天入口，不暴露真实私有数量。若 source_type 过滤，仅需要计算该来源计数，其他字段为0；UI 不误认为它是全来源统计。显式请求无权来源仍404。
MentionSummary = {chapters:[{id,chapter_no,title,has_content}],entries:[{entity:EntityRef,mention_count,ambiguous_count,chapter_count,last_chapter_no:int|null,gap_chapters:int|null,counts:[{chapter_id,count,ambiguous_count}]}],total,page,page_size}。
SourceWindow = {source:SourceRef,content_hash,stale:bool,window_start:int,text:str,spans:MentionSpan[],target_start:int|null,target_end:int|null}；text 最多约8000码点上下文，spans 使用全文绝对偏移，客户端减 window_start。调用方原 hash 与当前不同 → stale=true，target=null，显示变化提示及重新检索入口，不盲用旧范围。不存在/跨作品/无权 →404。参数类型与范围非法422；统一 AppError/detail 结构。

## 展示与导航
- 公共 MentionText 渲染 React 文本节点与按钮，绝不用不可信 HTML；同时显示有歧义候选。EntityPreview Drawer 只读展示当前基础资料，提供详情链接。
- 章节编辑/标记视图共用当前 content 状态。编辑器仍为 TextArea，切换不提交；预览请求节流并带作品/身份/文本代次，旧结果丢弃；AI 流期间暂停标记并说明，保存后刷新全书统计。
- Reader 保留段落原始码点起止，提及定位优先于阅读进度恢复；保留 CRLF、空行、段首空白与 emoji 映射。
- 详情增加 Mentions Tab，独立请求状态，不能覆盖既有基础/剧情变化草稿；支持 ?id= 深链。
- 来源导航通过 source ID + start/end/hash（URL 不放正文片段）。chapter → 原章编辑器/阅读器并高亮；chat → ?agent=open&conversation_id=&message_id=，消息卡定位，暂停自动粘底并用原文片段展示命中以避免 Markdown 源映射错误；note → notes?id=；knowledge → 原知识库页面指定 document_id 并打开原文定位窗口，不依赖 chunk ID。
- SourceWindow 公共原文定位视图可用于知识库及聊天精确片段；必须同时选中真正消息/文档，不只弹孤立摘要。
- 作品“提及”页展示时间线/热力图、类型/词条/章节筛选、表格数据替代、20条目分页与最多50列章节可视窗口；零值、歧义、空章分别可读，键盘可点击、主题 token 配色。
- gap_chapters = 在当前筛选章节范围内，按当前章序，在最近确定提及之后的非空章节数；无章节范围筛选时范围为全作品。last_chapter_no与所有计数均使用同一筛选范围；范围内从未确定提及为null/“未提及”。界面标明统计范围；歧义不伪装确定出场。

## 兼容、安全、验证与回退
迁移升级与 SQLite/PG 约定一致；0032/0033 顺序固定，APP_SCHEMA 最终0033。别名兼容旧备份、元数据 CAS 和智能体效果撤销；新增笔记备份往返。权限测试覆盖非owner管理员与真实owner，不让 read-only 预览写原文或调用AI。
只撤销新提及入口/派生缓存即可停止追踪，不影响正文；回退笔记迁移会丢笔记，必须先导出，不自动 downgrade 有用户资料的数据库。
实现阶段执行 backend ruff/pytest、frontend lint/build/test、迁移/备份/权限/Unicode/导航回归及真浏览器；使用合成文本记录冷/热扫描规模与耗时，不凭主观估计报告性能通过。
