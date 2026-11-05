# Research: 提及追踪的前端展示与精确导航

- Query: 正文、角色/世界观、聊天及现有资料的展示与导航能复用什么；哪些缺口会影响第 2 项的验收。
- Scope: internal；只读产品代码，未运行应用或测试。
- Date: 2026-11-05
- 任务状态：planning；下列方案是供范围收敛使用的建议，尚非用户批准的产品决策。
- 范围更新：用户在本轮调研期间回复「全部做」，已确认正文、聊天、独立新笔记、知识库资料四类均纳入；具体交互方案仍待最终评审。

## Findings

### 1. 正文编辑器与阅读器

| 文件 | 现有能力与限制 |
|---|---|
| `frontend/src/pages/project/ChapterEditorPage.tsx:245` | `displayContent` 在 AI 流期间改为流式文本，其余时间为本地草稿；标记来源必须区分草稿、已保存正文和生成中的临时文本。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:254` | 服务端正文通过 `applyServerChapter` 同步标题和正文；`:261` 在生成、采纳、回滚后把选区记录移到结尾。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:269` | `useProjectRefresh` 收到章节等变更后重新读取；有草稿、锁定或保存中时只记录外部版本，不能由提及刷新覆盖当前草稿。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:279` | `syncSelection` 读原生 `selectionStart/End` 并保存 UTF-16 范围，可供定位复用。当前正文 TextArea 没有供外部定位使用的 ref。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:334` | 未保存或局部改写待采纳时启用 `beforeunload`；`:345` 的路由 blocker 仅在 pathname 改变时拦截。新跳转不可绕过它。仅查询串变化不会触发该保护。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:640` | 已支持 `?panel=analysis` 打开分析抽屉，关闭时只删除自己的查询参数。可借鉴查询参数的消费方式。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:678` | “阅读模式”切换到 `/projects/:pid/read/:cid`，通过 `location.state.from` 记录编辑器返回地址。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:912` | 正文外层容器自行滚动，里面是自动增高的 `Input.TextArea`；单靠设置浏览器窗口滚动无法定位编辑区正文。 |
| `frontend/src/pages/ReaderPage.tsx:161` | 阅读器读取已保存章节，`:163` 通过 `splitParagraphs` 生成段落，`:343` 将纯字符串放入 `<p>`；天然适合用 React 文本节点插入标记。 |
| `frontend/src/pages/ReaderPage.tsx:208` | 载入章节后默认按阅读进度滚动；提及跳转应优先于该恢复逻辑，避免刚定位又被进度恢复覆盖。 |
| `frontend/src/router.tsx:26` | 阅读器是 AppLayout/ProjectLayout 外的独立全屏路由；不能假定它拥有 ProjectLayout 的智能体停靠面板和项目 store 加载行为。 |

**偏移契约的关键事实：**

- `frontend/src/utils/textOffset.ts:1` 明确 TextArea 使用 UTF-16，Python 使用码点；已有 `utf16ToCodePoint`（`:13`）、`codePointToUtf16`（`:24`）和 `codePointLength`（`:36`）。`components/chapter/RewritePanel.tsx:98` 已在向后端传递选区时使用该换算。
- `frontend/src/utils/reader.ts:151` 的 `splitParagraphs` 依次按换行分段、`trim`、滤掉空行。**其返回值不保留原始正文偏移**，不能把段落索引或拼接后的文本位置当作后端位置。需保留各段在原文中的范围，或直接从原文范围渲染，并兼容 CRLF、空行、段首空白、emoji/CJK 扩展字符。
- 现有编辑器和 Reader 均无提及范围 URL 解析、过期原文校验、对应标记 DOM 锚点。导航必须绑定来源 ID、范围及足以判断原文是否变动的凭据；过期时不能盲选新正文的同一偏移。凭据/API 细节由后端设计统一。

### 2. 角色与世界观详情：没有现成的条目深链

- `frontend/src/pages/project/CharactersPanel.tsx:79` 使用本地 `editing`、`drawerOpen`、`tab` 状态；`:194` 的 `openEdit` 打开详情。当前路由仅为 `/projects/:pid/characters`（`router.tsx:109`），未读取条目 ID 查询参数。
- `frontend/src/pages/project/WorldPanel.tsx:45` 使用本地 tab 和 `useWorldBaselineEditor`；`:55` 的 `openEdit` 每次回到基础页。当前路由为 `/projects/:pid/world`（`router.tsx:108`），未读取条目 ID 查询参数。
- 角色详情已有多 Tab（`CharactersPanel.tsx:554`），适合增加按需加载的“提及”页；世界观 Modal 已有基础/剧情变化 Tab（`WorldPanel.tsx:172`、`:249`），可沿用同类入口。
- 现有深链范例是 `frontend/src/pages/project/ForeshadowsPanel.tsx:100`：`?id=` 读取指定伏笔，消费参数后发起详情请求，检查组件存活与 `canReadNow()`，打开抽屉。可复用这种结构，不能直接把角色/世界观目前的列表选中状态称作深链。
- 设定编辑器已有额外的草稿与请求隔离：`CharactersPanel.tsx:95` 使用 request gate、draft cache；`:107` 在作品/身份变化时关闭并清理。`components/progressions/useWorldBaselineEditor.ts:20` 同样将身份、作品纳入请求作用域。新提及 Tab/深链不得重建编辑器从而丢失未保存的基础资料。

### 3. 聊天：可打开面板，不能直接选中会话或消息

- `frontend/src/pages/project/ProjectLayout.tsx:21` 仅消费 `?agent=open`；`:23` 以 `location.key` 支持本次打开后手动关闭。`:44` 仅 `canUseAI` 时挂载 AgentDock，管理员人工内容管理不能由新深链强行显示他人的私有会话。
- `frontend/src/components/agent/AgentPanel.tsx:14` 的会话选中是组件本地状态；`:18` 找不到选中项时默认取第一项。`:51` 以会话 ID 为 key 挂载详情。需要新增显式会话定位输入，并区分“请求目标不存在”与“没有选择目标”，否则失效链接会误导到另一会话。
- `frontend/src/components/agent/AgentDock.tsx:19` 只在打开时挂载面板；关闭后重新打开会重新初始化当前选择。定位参数应能穿过懒加载和面板重开。
- `frontend/src/components/agent/AgentTimeline.tsx:23` 维护时间线滚动容器；`:24` 默认粘底，`:27` 在消息变化时滚到底。精确消息定位需要消息锚点、展开完成后的滚动，并暂时暂停粘底，避免新流输出抢走位置。
- `frontend/src/utils/agent.ts:60` 的 `buildAgentTimeline` 合并历史消息、工具步骤和提案；消息已有稳定 key `message-${message.id}`（`:79`），适合生成 DOM 锚点，但当前只是 React key，不是可导航的 DOM id。
- 普通用户消息按 `whiteSpace: pre-wrap` 展示，助手消息经过 `AgentMarkdown`（`AgentTimeline.tsx:43`）。`AgentMarkdown.tsx:16` 分代码块，`:20` 按行渲染，`:5` 又将粗体、代码、链接转换成节点。因此原始 Markdown 范围与可见文本范围不恒等；若要求字符级聊天高亮，需要保留源映射，不能直接拿原始偏移去切渲染结果。
- 工具载荷在 `utils/agent.ts:68` 转成可折叠操作记录（`AgentTimeline.tsx:45`），提案另有卡片。**是否把工具 JSON、提案前后值、助手代码块算作“聊天提及”是来源语义，不能简单扫描所有消息字段再全部标作自然对话。**
- 渲染约束：`AgentMarkdown.tsx:4` 将原始 HTML 当文本，只允许 http(s) 链接。提及标记应继续用 React 节点，不能为了插入高亮改用不受信任 HTML。

### 4. 知识库资料与“笔记”并非同一个现成功能

- `frontend/src/pages/project/KnowledgePanel.tsx:43` 的知识库是文件上传、分块和检索调试。`:56` 用本地 `viewing` 控制文档抽屉；`:197` 点击“查看分块”，没有文档 ID/偏移深链。
- `frontend/src/components/kb/KbChunksDrawer.tsx:30` 每次从第 1 页加载，20 块/页；`:53` 展示 `start_offset/end_offset`，`:56` 默认折叠为 6 行。接入提及至少需要定位目标文档与页/块、展开命中内容并滚动；不能只打开第一页。
- `frontend/src/pages/project/KnowledgePanel.tsx:202` 已明确重建索引会删除现有分块。若选择知识库资料，**chunk ID 不能单独充当长期稳定导航身份**；应以文档与原文范围为主，重建后再解析当前位置。
- `frontend/src/components/kb/KbSearchDebug.tsx:82` 的命中仅显示文件名、标题路径、评分与可展开内容，未提供到文档的跳转。
- 未发现作品内独立自由笔记页面/路由。存在灵感会话“补充想法”：`frontend/src/components/inspiration/DraftSidebar.tsx:81`，通过 `session.state.notes` 的数组索引删除（`:88`、`:99`）；API 为 `/inspiration/sessions/{id}/notes`（`frontend/src/api/inspiration.ts:22`、`:34`、`:37`）。
- `frontend/src/api/types.ts:2527` 的灵感会话 `project_id` 和 `converted_project_id` 都可空；这些想法属于灵感会话，且数组索引随删除变化，不能直接充当作品笔记及稳定笔记 ID。是否纳入需产品范围确认。
- 名为 `note` 的其他字段主要是版本备注、设定变化备注、职业/关系备注，不代表完整笔记模块；不能以它们覆盖 `TODOS.md` 第 9 项的语义。

### 5. 建议的 UI 接入与取舍（待用户选择）

1. **标记与写作**：首版可在 Reader 提供“显示提及”开关，并在编辑器同页提供“提及预览/本章提及”入口，保持本地编辑草稿常驻。普通 TextArea 当前只接收一个字符串值；要在可编辑文字上直接插入可点击标记，需要镜像覆盖层或更换编辑技术，需额外验证输入法、选区、滚动、换行和无障碍。不能把它默认为免费的样式修改，也不能只做 Reader 却宣称完成编辑中标记。
2. **正文到设定**：可点击标记打开同页只读设定预览，提供“查看完整设定”深链；同页预览便于保留章节草稿。若直接跨页打开现有详情，必须走现有离页保护。具体默认体验由主会话确认。
3. **设定反向提及**：角色/世界观详情新增共用“提及”Tab，按已批准的来源筛选，列表显示来源标题、章节号或时间、上下文片段；每条点击准确来源。聊天提及频次与正文出场频次需分开，避免把创作讨论算作正文出场。
4. **作品级热力图**：适合新增按需加载的项目页，横轴章节、纵轴条目，格子可展示计数并点击筛选命中片段；结合来源筛选、条目类型筛选和未出场排序。热力图的章节跨度、分页与零值展示需在设计中明确，不能为未写章节默认为“长期缺席”。
5. **可复用图表形态**：`frontend/src/components/foreshadows/ForeshadowTimeline.tsx:25` 已将派生计算放纯函数，并在`:50` 提供局部横向滚动、使用主题 palette。可复用布局原则；此组件不是热力图组件，也不能原样复制其仅点击 SVG 文本的交互。`frontend/src/components/dashboard/DashboardHistory.tsx:38`、`:77` 使用有标题/说明的 SVG，`:143` 提供数据表，适合作为热力图数值和无障碍替代视图的参考。
6. **异步与刷新**：`frontend/src/hooks/useLoader.ts:19` 只有 effect 生命周期 `alive`，不自动清空旧 data；新定位/筛选要明确作品、身份、目标和请求代次。可以复用 `useProjectAccess.ts:14` 的当前权限检查，以及现有设定 request gate；提及刷新还要订阅章节、角色、世界观等真实资源变化，不能只在面板首次打开取一次。

### 6. 四类来源全部纳入后的最小笔记与定位建议

- **入口与 CRUD**：新增作品内独立“笔记”入口及懒加载页面（建议路由 `/projects/:pid/notes`），以稳定笔记 ID 关联当前作品。列表显示标题、更新时间和内容摘要；创建/编辑维护标题与纯文本正文，删除有确认。列表与加载/错误/空态可沿用 `KnowledgePanel.tsx:51` 的页面结构，编辑器可借鉴 `WorldPanel.tsx:38` 的本地表单与弹窗，但笔记是独立资源，不复用世界观数据或灵感 notes 数组。
- **编辑与预览**：正文采用 TextArea，标记预览与编辑视图共享同一份本地草稿。可切换预览，但切换不提交、不清空草稿；跨笔记或离页要保留/确认未保存修改。只读用户仍能看正文与提及，不能提交或删除。是否进一步增加收藏、置顶、与章节并排查看属于第 9 项完整能力，需主会话写清本次边界。
- **精确来源展示**：笔记正文以保留原始空白的文本节点渲染，提及用码点范围切分，再转换 UTF-16；不要仿照 Reader 先 trim 再定位。深链应携带笔记 ID 与提及位置身份，先取当前笔记核验原文，再高亮和滚动。修改或删除后，旧链接明确提示变化或不存在，不转到列表第一篇。
- **四类来源统一外壳**：可以共享“来源标签＋标题＋位置说明＋命中片段＋跳转按钮”的列表行，目标解析分别处理章节、会话/消息、笔记、文档原文。知识库需处理分块跨页和重建，聊天需处理 Markdown 映射及非自然对话载荷，不能用同一段 `scrollIntoView` 代码掩盖来源差异。
- **支持主会话的交互建议**：保留章节 TextArea，增加可切换的提及标记视图，Reader 同样支持标记，原页抽屉显示只读设定，能避免为查看设定离开未保存草稿。关键前提是编辑器的标记视图针对当前 `content`；若只能取得已保存正文的索引，必须显示“已保存版本”，不能将旧范围直接标在新草稿上。AI 流式 `displayContent` 同理，需明确临时文本是否标记，不能照搬保存后的偏移。

### 7. 可复用测试与验证边界

- `frontend/src/utils/textOffset.test.ts`：BMP、emoji/CJK 扩展字符、往返换算、代理对边界和越界；扩展原文范围切分及高亮测试。
- `frontend/src/utils/reader.test.ts:53`、`:103`：进度恢复、分段；新增定位优先级、保留原文范围、空行/CRLF 测试。
- `frontend/src/utils/agent.test.ts`：历史/实时合并与稳定消息身份；`frontend/src/components/agent/agentUI.test.ts:64`：安全 Markdown；新增目标会话不存在、指定消息高亮、定位期间不被粘底覆盖等契约测试。
- `frontend/src/api/kb.test.ts:49`：资料 API 路径与方法；若纳入资料，增加跨页命中、重建索引后重新定位、删除目标兜底。
- `frontend/src/components/entities/characterForm.test.ts`、`components/progressions/worldForm.test.ts`、`worldSnapshot.test.ts`：别名表单/基线兼容的扩展落点；不能破坏已有剧情变化草稿与 revision 保护。
- `frontend/src/theme/noHardcodedColors.test.ts`：颜色只能集中在 palette 或来自 token。所有图形需要深色主题、键盘可达、明确焦点与数值说明。
- `frontend/package.json:6` 的现有门禁是 `npm run lint`、`npm run build`、`npm test`；本轮未运行，因为没有实现变更。
- 真浏览器应验证：标记点击、精确滚动与选区、未保存正文返回、输入法、AI 流进行中的定位、跨作品/身份切换、375px/桌面窄内容区与深色主题。纯 Node/SSR 测试不能证明这些交互。

## Related Specs

- `.trellis/spec/frontend/component-guidelines.md`：章节编辑、知识库、主题/响应式、Reader、项目智能体、设定随剧情演变。
- `.trellis/spec/frontend/type-safety.md`：API 镜像类型与偏移契约；API 统一走 `src/api/`。
- `.trellis/spec/frontend/hook-guidelines.md`、`state-management.md`：请求生命周期、SSE、项目资源刷新与状态归属。
- `.trellis/spec/frontend/quality-guidelines.md`：测试、入口包边界、路由级懒加载、编辑器不变式。
- `.trellis/spec/backend/reader.md`、`agent.md`、`retrieval.md`：各来源的接口与权限契约。
- `.trellis/spec/backend/setting-progressions.md`、`rbac-quotas.md`、`admin-projects.md`：设定基线与剧情值、当前权限检查、管理员人工内容与私有数据的边界。

## External References / Versions

- 本轮未使用外部资料；结论来自仓库。依赖版本仅记录 `frontend/package.json` 当前声明：React `^18.3.1`、React Router `^6.30.6`、Ant Design `^5.29.3`、Vitest `^5.0.5`，不代表重新核验过的上游最新版本。

## Caveats / Not Found

- 用户已确认四类来源全部纳入；知识库、灵感想法、独立笔记仍不可互相替代，独立笔记应新增真实资源。灵感想法没有因此自动纳入。
- 未发现正文内既有可点击提及层、统一条目深链、会话/消息深链、知识库原文位置深链或可直接复用的热力图。
- 尚需产品确认：正文标记是否要求可编辑状态直接点击；同名候选显示与统计方式；正文“提及”是否可作为“出场”近似及如何呈现该限制；未出场/集中阈值与参考章节范围。
- 此报告只覆盖前端证据。索引一致性、来源 hash、后端身份过滤、匹配算法与迁移方案由后端研究及后续 design 统一。
