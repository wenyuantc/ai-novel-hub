# Research: 自动保存的服务端契约与标题冲突保护

- Query: 如何在现有场景／富文档、权限、AI 写作与版本恢复体系中增加可靠的标题保存条件，并处理本地草稿身份、旧客户端和保存结果未知？
- Scope: internal；仅读取当前仓库与执行无文件写入的内存 SQLite 最小实验。
- Date: 2026-11-17
- 范围前提：用户已选择方案 A，断网期间只要求已打开页面继续写作和保留草稿，联网重新打开并鉴权后恢复；本记录不设计离线登录或应用缓存。

## Findings

### 1. 已核实的源文件与职责

| 文件 | 职责与定位 |
| --- | --- |
| `backend/app/schemas/chapter.py:20–29,37–56` | `ChapterUpdate` 只有可选 `expected_content_revision`；`ChapterOut` 继承 brief 并加正文，尚未输出 `created_at`。 |
| `backend/app/api/v1/chapters.py:89–94` | 人工章 PATCH 使用 `ChapterUpdate`，调用 `update_chapter(..., manual=True)`，再按当前人工读权限生成响应。 |
| `backend/app/services/chapter_service.py:111–155,339–435` | 锁后重读章节、保留调用方待写普通字段、鉴权、生成中门禁、章标题与正文更新、审计和版本提交。 |
| `backend/app/services/project_access.py:70–118` | 写路径先取认证／User 屏障，再按需取发送门禁、项目锁并重读成员授权；SQLite 锁繁忙转成受控 409。 |
| `backend/app/services/scene_service.py:115–163,398–411,542–572` | 章级内容版本检查及条件 UPDATE、单一正文投影、人工写入与批量范围门禁、场景元数据／正文历史。 |
| `backend/app/services/rich_document_service.py:295–330` | 富文档整份原子保存、显式标记／区块增删及恢复血缘校验、权限／发送屏障、正文版本与审计同事务提交。 |
| `backend/app/services/chapter_version_service.py:191–240` | 恢复时沿用人工写入门禁、内容版本检查和完整场景结构；产生恢复版本。 |
| `backend/app/services/version_service.py:23–24,109–167` | 10 分钟固定窗口、相同快照跳过、星标／注释／清空／结构边界保护；最多保留 50 个普通版本及全部星标。 |
| `backend/app/services/agent/tools/write.py:559–567,654` | 智能体 `update_chapter_meta` 直接经服务层写章标题，不走 HTTP 路由。 |
| `backend/app/services/agent/tools/base.py:227–274`、`backend/app/services/agent/service.py:897–906` | 智能体预览在写锁内通过保存点获得完整前值指纹；确认时重新预览，不同则 409 并要求重新确认。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:359–377,630–644` | 当前唯一人工标题更新入口与章状态更新入口；标题保存先存场景，再 PATCH 标题。 |
| `frontend/src/components/chapter/ChapterStyleSetting.tsx:31` | 风格绑定也调用章 PATCH，但不改标题。 |

### 2. 现有保护能复用什么

1. `get_manual_chapter(..., "content.edit", for_update=True)` 已在项目／User 写屏障后重读章节，不能另建一个绕过这些屏障的“轻量自动保存”入口。富文档保存还须保留 `visibility_write=True`，避免与模型发送范围竞争。
2. `content_revision` 是章级内容／场景结构版本。`touch_revision` 通过 `WHERE id = ... AND content_revision = expected` 原子推进；不是只在 Python 中检查后无条件写入。
3. 场景标题、摘要及正文变更都会推进这一章的内容版本。因此同章不同场景发生变化仍可能导致旧草稿 409；首版保留该保守规则，不擅自改成场景级版本。
4. 章标题目前不推进 `content_revision`，只在请求字段含 `title` 时推进项目规划版本。只给标题请求加 `expected_content_revision` 仍无法发现另一端只改了章标题。
5. 富文档无变化时，仍先检查 `expected_content_revision`，再返回同一文档；旧 revision 即便正文恰好相等也不会直接成功。这是标题契约应保持的先校验、后 no-op 顺序。
6. 正文仍由 Scene 及富文档作为权威；不得用章级纯文本 PATCH 恢复多场景或富格式草稿。章 PATCH 的旧正文入口仅用于其现有安全桥接。

### 3. 标题冲突方案比较

| 方案 | 优点 | 代价／不足 | 结论 |
| --- | --- | --- | --- |
| 只锁内精确比较 `expected_title` 原值 | 不新增数据库列，可发现当前不同标题 | 不能解决未知请求迟到的 ABA：重试 A→B 成功，再改回 A，原请求仍可能满足全部旧条件并再次写 B | 不可单独作为可重试保存协议；保留该字段仅作明确基线比较。 |
| 新增 `title_revision` | 单调版本可识别 A→B→A；协议容易描述 | 涉及迁移、Out schema、全部章标题写入／Agent effects 撤销／导入与备份路径，并须证明每个写入口推进；本期成本明显更高 | 本期不采用。 |
| 实际章标题变更也推进现有 `content_revision` | 无 DDL；每次实际写入不可逆地消耗旧条件，已能复用原子 CAS；Agent 版本排除／撤销设施现成 | 需覆盖所有标题写入及纯标题撤销；带来更多保守场景冲突，须同步刷新本实例基线 | 最终推荐，与 `expected_title` 和稳定场景锚点一起使用。 |
| 新增保存幂等收据 | 可为同一个 request ID 返回持久结果 | 需要表／清理／身份与 payload 指纹协议；尚未执行的请求如何撤销仍需持久取消或版本屏障 | 本期不采用，成本高于扩展现有修订号语义。 |

推荐标题请求同时携带：

```json
{
  "title": "作者希望保存的新标题",
  "expected_title": "作者开始编辑时读取的服务端原始标题",
  "expected_scene_id": 123,
  "expected_content_revision": 17
}
```

- `expected_title` 是字符串，允许空串，最大长度与章标题一致；严格比较服务端原值，不能先 trim 基线或用最新 GET 值替换旧基线。
- `title` 继续沿用现有 `.strip()` 入库行为。前端识别已同步时比较这一服务端规范化结果；原输入与显示如何处理由编辑协调器保持一致。
- `expected_scene_id` 是该标题草稿创建时绑定的真实场景 ID，正整数；不是场景序号。锁内要求它仍属于此章且活动，防止已删章节的整数 ID 被重新使用后旧标题草稿串写。
- `expected_content_revision` 严格非负整数。标题和正文一起脏时，标题阶段使用本轮正文已确认回执的 revision；不能通过随手 GET 最新 revision 后继续写来绕过另一端变化。
- **复核修正**：实际章标题变化也必须在同事务中经 `scene_service.touch_revision` 推进该章内容版本。只比较标题值而不推进单调版本不安全；标题-only 成功预期为旧 revision + 1。标题与正文同一次旧 PATCH 混合提交时先检查原前置条件，若正文路径已推进版本则不额外重复推进，至少消耗一次原版本。
- 标题-only 修改也带内容版本：另一端 AI 写作、恢复或结构变化后暂停并核对，保持本任务的保守冲突规则。
- 锁内顺序：原权限／生成中门禁 → 活动场景归属与身份 → 内容 revision → 原标题 → 真正修改及规划版本／审计。所有条件必须在写入副作用前核对。
- 标题不匹配返回 `409 code=stale_chapter_title`，不把服务端内容直接塞入未经当前读权限校验的错误详情；客户端另行 GET 已授权版本。场景失效沿用 404／明确目标失效，保留本地草稿并提供复制／导出。
- 即使服务端标题已经等于 `title`，只要 `expected_title` 不符仍返回冲突。由前端重新读取后确认“该快照已同步”，不能后端先以期望结果相等绕过条件。
- 若所有条件相符且规范化标题已经相同，允许 no-op 返回；标题-only 路径不推进规划版本、不产生历史或无意义审计变化。
- 新 guard 字段必须从 `audit.track` 的字段集合排除。现有实现只排除了 `expected_content_revision`（`chapter_service.py:357`），照抄会错误地把协议字段当模型字段。

### 4. 第一方入口与兼容方式

人工标题目前只有 `ChapterEditorPage.save` 一处；状态修改和 `ChapterStyleSetting` 不应被强制要求标题令牌。智能体标题工具是独立的内部执行路径，已有锁内完整预览比较。

推荐将人工 HTTP 请求模型与内部服务参数分开：

1. HTTP 章 PATCH 只要 `title` 非 null，就必须提供上述三个条件；缺失或类型无效返回 422。空标题仍是一次标题写入，不能因 falsy 检查跳过保护。`title=null` 沿用原不修改语义。
2. 内部 `ChapterUpdate` 可保留条件为可选字段，供现有 Agent 预览／执行器使用；它本来就依赖当前事务中的完整提案前值比较。不得让模型编造 `expected_title` 或客户端场景令牌，也不得将 `manual=True` 一概等同 HTTP 来源，因为协作者 Agent 同样走人工内容权限。
3. 原 HTTP 路由应使用专用请求 schema 或显式边界验证，而不是只在服务函数检查 `manual`。内部 Agent 和 HTTP 的鉴权仍都由原服务层完成；新增请求模型只区分协议前置条件。
4. 新前端所有人工标题同步带原基线；首次请求、重试、恢复草稿、冲突确认后重试都一致。审查所有 `chapterApi.update` 用例，状态／风格继续原有接口。

部署与回滚有两个可执行选择：

- **更稳妥的选择：新增受保护的 `PATCH /projects/{pid}/chapters/{cid}/editor-title` 路由。** 请求严格只含四项标题保存字段，响应沿用 `ChapterOut`，服务实现仍复用章更新／锁／审计原语。新前端只走此路由；原章 PATCH 同时拒绝缺少条件的标题写入，防缓存旧页面绕过。旧后端没有新路由，返回 404，不能把新 guard 当未知字段忽略后无条件写入。因此能够安全拒绝页面已打开后的后端回滚。
- **更小 API 表面：继续只使用现有章 PATCH。** 后端先上线强制 guard，再上线前端；缓存旧页面标题保存得到 422，须刷新。可给 `ChapterOut` 增加固定 `editor_save_contract=1` 让新前端识别支持版本，但它只检查加载时能力，不能防止页面打开后热回滚到会忽略未知字段的旧后端。若采用此选择，回滚必须保留新后端保护，只回滚前端，不能承诺任意旧后端回滚安全。

两者均无需数据库迁移。父任务已选择新增受保护的 `/editor-title` 路由，并让原章 PATCH 的标题写入同样要求三个条件；内部 Agent 保持原锁内预览契约。新前端不需要 capability marker，也不得在 404／失败时回退到无条件章 PATCH。替代方案仅保留为取舍依据，不是待实现分支。

### 4.1 修订号推进必须包含 Agent 执行与撤销

- 共用 `chapter_service.update_chapter` 在实际标题变化时推进版本，覆盖 HTTP 与 `agent/tools/write.py:563–566` 的执行；原模型参数无需新增客户端令牌。
- `agent/effects.py:221–222` 从 effects 快照排除 `content_revision`；`:1205–1208` 从恢复写值剔除它，普通 UPDATE 撤销不会直接回灌旧修订号，现成机制可保留。
- 但纯章标题 undo 当前只通过 `_restore_rows:1294–1301` 恢复 title，`_restore_scene_projections:1386–1407` 仅收集重建章、场景及场景规划修改，**不会推进纯标题恢复的修订号**。必须新增对 `before.title != after.title` 且前后都存在的章进行推进。
- 同一 undo 中已由 `chapter_text`、场景／规划投影或重新插回旧章路径推进的章应去重；纯标题路径仅调用 `touch_revision` 即可，不需要凭空生成正文历史。旧章节重建仍使用已有 revision floor 后继续递增，不能退回更低版本。
- Agent 预览仍在保存点中执行并回滚，试算的 revision 不应泄漏成持久变更；实际执行与撤销才分别消耗 revision。需定向验证预览、执行、撤销、再次执行都保持相应语义。
- 既有章标题赋值在本次检索中集中于共用 service 及 effects 通用恢复；建章、拆书／备份导入创建新身份，不是修改既有标题。整库回档边界仍不纳入保证。
- 标题改名原本已推进规划版本，`scene_ai_context.py:64–80` 的 AI 快照也已包含章标题／规划版本。新增内容版本推进会使普通场景编辑更加保守地冲突，但不取消原 AI 保护；本实例须把自身标题确认的新 revision 同步到场景基线／目录，不能误报为外部修改。

### 5. 草稿目标身份与 `created_at`

- `Chapter` 已继承 `TimestampMixin`（`backend/app/models/entities.py:585`），`created_at` 定义在 `backend/app/models/base.py:19–23`。只在 `ChapterOut` 与前端 `Chapter` 添加该字段无需 DDL；`manual_chapter_views` 使用 `model_validate` 从 ORM 投影（`chapter_service.py:202–210`），现有读取可直接提供。
- 本期无需新增 `ChapterOut.created_at`，也不扩展 `ChapterBrief`。调查确认该字段可以无 DDL 输出，但它只可能辅助核对，不能当作不可复用 token；实际方案直接使用已有场景稳定锚点。
- Chapter 表无 `sqlite_autoincrement`（`entities.py:585–601`）；Scene 表有该配置（`models/scenes.py:9–21`），迁移 `0039_independent_scenes.py:134` 同样设置。
- 已执行不写文件的内存 SQLite 最小实验（系统 SQLite 3.43.2）：建 `id INTEGER PRIMARY KEY, created_at DATETIME DEFAULT CURRENT_TIMESTAMP`，立即插入、删除、再插入，得到相同的 `(id, created_at)`。因此章 ID + created_at 并不能严格防同秒复用。
- 推荐本地草稿记录 actor / project 身份及其已有 `created_at`、章 ID、稳定 scene ID。即使只有章标题脏，也绑定当时场景。同步时必须由服务端在锁内证明该场景仍在目标章；场景已退场则要求显式核对或导出，不能自动改绑第一个活动场景。
- 不新增全局实例 UUID；整库恢复到旧快照或管理员篡改身份属于另一类恢复边界，本期不可声称覆盖一切跨数据库实例重用情形。

### 6. 保存结果未知与多阶段确认

现有保存顺序并非一个事务：富文档 PUT → 可能的场景标题／摘要 PATCH → 章标题 PATCH。每一请求内部原子，但跨请求必须分别确认。

| 重读事实 | 处理 |
| --- | --- |
| 身份正确，服务端完整目标恰好等于已发送快照 | 将该发送快照确认已同步；保留发送之后的新输入和其它标签页草稿。若同章版本还出现额外变化，仍保持新写入暂停并核对，不据此自动换全部基线。 |
| 身份正确，服务端仍是原基线和原 revision | 允许明确重试原快照；写入仍携带相同前置条件，重读与重试间的竞争由服务端拒绝。 |
| 服务端既不同于发送快照，也不同于基线，或同章 revision 意外推进 | 进入冲突，不猜测是否由自己写入，不自动回放。 |
| 认证／授权失败、章或原场景失效 | 停止同步，保留本地数据按当前权限决定能否显示，不跳转到同号其他资源。 |

- 对富文档比较完整 `RichDocument`，不能只比较纯文本；同文不同区块、标记、格式仍是不同版本。
- 某阶段已确认成功后持久化该阶段的新基线，再进入下一阶段。第二阶段失败不能重发第一阶段旧 payload；新草稿不能被旧阶段回执清理。
- 服务端响应投影前还可能撤权：HTTP 最终返回 403／404 不等于此前事务一定没提交。当前章 PATCH 就在提交后再次走 `manual_chapter_views`，必须保持未知结果核对语义。
- 当远端仍为 B 而原 S 可能迟到，不能执行“采用服务器并完成／舍弃未决 attempt”来假装撤销。先保留／核对，或明确重试同一 S；以已确认的单调 revision 消耗旧条件后，才能允许新的选择。导出、继续本地编辑和显式离页可用，但 unknown 记录仍须保留。
- 本期不必新增服务端草稿表或保存幂等收据。前置条件、目标等值核对和分阶段回执足以防重复覆盖；不能将“等值”宣称为已证明由这一次请求完成。

### 7. 版本历史与富文档约束

- `scene_service.capture_snapshot:286–303` 的富文档历史特意排除嵌套 `document.content`，正文只放场景 `content`；`version_service.same_structure:133–140` 又排除这一正文，因此普通正文仍可按窗口合并。
- 格式、标记映射、区块及摘要／场景标题改变属于真实结构边界，不应为了减少自动保存历史而把它们从比较中删除。
- 富文档保存的 `ensure_baseline`、显式增删／恢复证据、身份血缘、`record_manual`、原子审计必须完整保留。持久化草稿需能重建合法证据；缺证据时进入核对，不能改用纯文本接口绕过。
- 章标题本身不在正文版本模型中（`entities.py:770–788`）；本期标题草稿恢复不等于新增“章标题历史恢复”。章标题仍由审计记录，正文历史和本地草稿承担各自原职责。

### 8. 当前可执行的验证资产

本次未运行产品测试，只核对了当前文件、配置和测试名称。

- `README.md:889–895` 明确默认 SQLite `-n 4 --dist loadfile`，并说明自动化测试不再验证 PostgreSQL。
- `.github/workflows/python-package.yml:31–37` 安装冻结开发依赖、Ruff、SQLite 四进程测试。
- `backend/tests/conftest.py:100–123` 以隔离 `tmp_path` 建应用／库并启停真实任务引擎；`make_client:147` 提供独立 Cookie 客户端。
- 当前 `backend/tests` 没有 PostgreSQL 套件／连接 fixtures；检索仅剩解释性注释。旧 spec 中 `*_postgresql.py` 文件及“配置连接串再运行”指令不能直接作为本任务现存门禁。

建议新增 `backend/tests/test_chapter_title_save.py`（拟新增名），重点覆盖：

1. 仅标题 A→B 后 revision 推进；另一端带原条件写 A→C 得到 409；B 不变。补充 ABA：阻塞的原 A→B 请求、成功重试、后续 B→A，再释放原请求，旧请求必须 409。
2. 当前标题已经等于请求新值但旧 `expected_title` 不符，仍 409；读取后由客户端核对，不后端隐式放行。
3. 标题前值相同，但另一端正文／结构／恢复使 revision 改变，返回 409 且不改标题。
4. 标题条件缺失／null／非法类型 422；空字符串前值合法；status-only、style-only 请求不受影响。
5. 原场景退场／跨章／跨项目、章节 ID 被复用，不能把旧标题写入新目标；加入明确同秒 created_at 复用 fixture。
6. 只读、撤权、管理员权限边界复用原 403／404 行为；标题冲突不产生正文版本／成功审计或推进规划版本。
7. 使用两个独立请求与受控屏障验证检查、写入的真实竞争；不可用固定 sleep 假定已取锁。
8. Agent `update_chapter_meta` 原预览／执行／撤销流程仍有效，旧预览在人工编辑后冲突；实际标题执行和纯标题撤销分别推进 revision，预览回滚不持久推进；同批正文／场景 undo 不重复推进；不让新增 HTTP 模型破坏内部服务调用。

现有相关文件：`test_chapters.py`、`test_scenes.py`、`test_scene_versions.py`、`test_scene_review_regressions.py`、`test_rich_document_api.py`、`test_rich_document_history.py`、`test_chapter_versions.py`、`test_collaboration_content_access.py`、`test_agent_api.py`、`test_agent_tools.py`、`test_agent_tools_e4ic.py`、`test_agent_effects.py`、`test_scene_agent.py`。

实施阶段定向命令（新增文件存在后执行）：

```bash
cd backend
uv run pytest -n 0 tests/test_chapter_title_save.py tests/test_chapters.py tests/test_scenes.py tests/test_scene_versions.py tests/test_scene_review_regressions.py tests/test_rich_document_api.py tests/test_rich_document_history.py tests/test_chapter_versions.py tests/test_collaboration_content_access.py tests/test_agent_api.py tests/test_agent_tools.py tests/test_agent_tools_e4ic.py tests/test_agent_effects.py tests/test_scene_agent.py -q
```

最终当前仓库门禁：

```bash
cd backend
uv run ruff check .
uv run pytest -n 4 --dist loadfile --durations=20
```

SQLite 用例证明接口、CAS 与已有屏障的行为，不宣称完成 PostgreSQL 行锁实测；本任务不自行重建已被移除的 PostgreSQL 测试体系。

## Related Specs

- `.trellis/spec/backend/scenes.md`：Scene 权威、内容 CAS、结构与历史、AI 任务／草稿。
- `.trellis/spec/backend/rich-documents.md`：完整文档、显式证据、身份／血缘和写入门禁。
- `.trellis/spec/backend/database-guidelines.md`：原事务、权限和项目边界。
- `.trellis/spec/backend/error-handling.md`：AppError 与 409／403／404 错误语义。
- `.trellis/spec/backend/quality-guidelines.md`：隔离测试、README 与质量要求；其中 PostgreSQL 旧内容以当前代码／README 为准，实施收尾应修正受影响说明而非按旧路径执行不存在测试。
- `.trellis/spec/frontend/type-safety.md`：后端输入输出与 TypeScript 同步。

## External References

本次结论来自当前仓库源码和本机纯内存 SQLite 实验；未使用外部资料，也未调用真实数据库、模型或外部服务。

## Caveats / Not Found

- 本文件是规划证据，不代表任何修复已实施或测试通过。
- 父任务已选择新受保护标题路由、原路径标题前置条件和内部 Agent 协议分离；不新增数据库列或 Chapter 时间戳输出。
- 复核发现标题值比较的 ABA 需要不可逆版本屏障；本报告已修正为实际标题写入／恢复同事务推进现有内容修订号，不能再使用早期“不改变标题-only 修订号语义”的方案。最终 `design.md:137,141,151,189` 与 `implement.md:21,24,64,111` 已同步该修正，包含 Agent 纯标题撤销、混合正文去重、自身 ACK 基线推进、未知请求结束条件及相关测试范围；见 `backend-plan-review.md` 的最终复核记录。
- `created_at` 不是强身份；标题草稿的场景锚点及锁内归属验证不能省略。
- 旧 HTTP 客户端无条件标题写入应明确拒绝，属于有意兼容性变化，需要部署说明；不能只给新 UI 加 guard 却让旧客户端仍可覆盖。
- 内存与当前序列化快照间的确认、输入期间证据重建及多标签草稿存储由前端设计承担，不能只靠后端锁推断已经解决。
