# Research: 设定缩略图 AI 生图的后端复用

- Query: 为四类作品设定缩略图添加 AI 生图入口，核对封面提示词、模型选择、授权与发送/回包门禁、图片标准化、错误和 API；评估系列共享设定。
- Scope: internal
- Date: 2026-11-17

> 研究中的可选方案以最终 `design.md` 为准：本次预览确认沿用既有当前图片版本 CAS，不要求跨实例的一次性 nonce 消费，也不宣称严格防止无图 ABA；若未来需要严格一次性或完整图片历史代次，须另行设计持久状态及迁移。

## Findings

### 文件与现有接口

| 文件 | 职责与证据 |
| --- | --- |
| `backend/app/ai/prompts/cover.py:6` | 封面默认词按作品标题、题材、标签、简介/设定及对当前 actor 可见的风格构建，逐项截断，附无文字要求；固定竖版书封，不能直接作为设定肖像提示词。 |
| `backend/app/api/v1/project_cover.py:13`、`backend/app/schemas/project_cover.py:6` | `/api/v1/projects/{project_id}/cover/prompt` 返回 `{prompt, warning}`，`/cover/generate` 接 `{prompt, config_id?}`，返回 `{image_base64, mime_type, model_source?}`；提示词去空白后 1–2000 字符。 |
| `backend/app/services/cover_service.py:87` | 默认词要求 `ai.use` 并锁项目到返回；生成是预览，无数据库写入。作者和协作者发送路径不同，详见下文。 |
| `backend/app/api/v1/setting_images.py:24` | 单书 GET/PUT/DELETE `/projects/{project_id}/settings/{kind}/{entity_id}/thumbnail`；系列 GET/PUT/DELETE `/series/{sid}/entries/{eid}/thumbnail`。PUT 传 multipart `file`、`expected_version`；系列再传 `expected_revision`。 |
| `backend/app/services/setting_image_service.py:18` | 单书四类主体映射及存在性校验；上传授权、规范化、版本比较、不可变资源与绑定的操作均已集中实现。 |
| `backend/app/services/setting_image_codec.py:16` | 输入校验、解码、WebP 归一化及有界线程池。`validate_standard_image` 可校验导入的标准 WebP，但现有普通 PUT 使用 `normalize_image_async`。 |
| `backend/app/services/project_ai_config.py:53`、`:832` | 服务端 `ResolvedProjectModel` 固定 actor、作品、配置来源、配置指纹、实例键和邀请身份；`resolve_project_model(..., "image", ...)` 统一选择图片模型。 |
| `backend/app/services/collaboration_ai_guard.py:1439`、`backend/app/services/ai_dispatch.py:708` | HTTP actor/模型/可见性戳的受控 provider 及实际 HTTP 发送门禁；已有测试直接覆盖重试、重定向、下载和回包竞争。 |
| `backend/app/services/setting_ai_context.py:35`、`:135` | 设定 AI 资格、字段和引用投影；与人工设定详情分离。 |
| `backend/app/services/series_service.py:107`、`backend/app/models/series.py:9` | 系列有独立 owner、editor 邀请身份与 revision，系列设定目前只有 character/world；系列对象没有与单书对应的 AI 模型授权字段。 |
| `frontend/src/api/projects.ts:30`、`frontend/src/api/settingThumbnails.ts:4` | 封面前端已封装提示词/生成；缩略图 target 区分 project/series，确认保存复用版本化 PUT。`frontend/src/components/entities/SettingThumbnail.tsx:42` 是现有图片旁操作区。 |

### 角色、模型选择与安全边界

- `backend/app/services/project_access.py:42`：作品作者、已接受 editor 能使用 AI；viewer 不能；仅凭管理员 `projects.read`/`projects.edit` 即使能人工编辑图片也不能代作者调用 AI。管理员同时为真实已接受 editor 时依 editor 能力处理。生图入口应同时要求有相应图片编辑能力和 `ai.use`；服务端验证，不能只依按钮隐藏。
- `backend/app/services/project_ai_config.py:832`：显式 `config_id` 只能选 actor 自己的图片配置；缺省优先 actor 的默认/最近图片配置，仅 actor 无配置且作者授予 `allow_owner_ai` 才回退作者配置；本人配置失效不得静默换作者。来源只能返回 `actor`/`owner`，不可泄露作者配置 ID、密钥、端点、模型名、原始第三方错误。此规则也适用于管理员兼协作者的真实 actor。
- `cover_service.py:152`：协作者路径固定原认证 actor、邀请轮次 `(invitation_id, revision)`、配置实例和指纹、作品可见性戳；事务回滚后构造 provider，防止长时间持锁；每次实际请求由守卫校验；接到结果后在锁内复验原授权、模型及戳。`project_cover.py:17` 的受限 JSON 响应让授权锁覆盖 ASGI 发送，最长 5 秒；已经发送的字节无法收回。
- `collaboration_ai_guard.py:80`、`:908` 只接受受信工厂与带已验证 hook 的 provider；不能相信第三方仅自称使用 hook。`dispatch_hooks.py:22` 在每一物理 HTTP 请求前获取 lease，收到响应头释放，异常/取消清理；OpenAI 的条件重试和图片 URL 下载/重定向也是独立物理请求（`openai_compat.py:103`、`:122`）。禁止仅在调用 `generate_image` 前检查一次授权。
- **不能直接照搬封面作者分支**：`cover_service.py:132` 对作者只调用 `resolve_config` + 原始 provider；默认封面提示词没有自动拼设定条目。新功能若向外发送结构化设定，作者路径也需要用**构建时** `VisibilityStamp` 绑定 `ai_dispatch.guard_provider`（或设计并证明同等受控路径），不能在准备发送时为旧提示词重签。协作者的 `for_http` 可复用但只绑定作品模型/可见性，尚不验证本次实体来源。需要在每个物理发送与最终回包时同时检查目标仍存在、属于本书且未变为系列共享；若计划使来源文本变化让旧请求失效，还要固定并核验相关来源版本/摘要。
- `EntitySourceSnapshot` 是可参考的来源摘要实现，但它绑定后台 `TaskExecutionIdentity`（`entity_ai_service.py:136`、`:218`），`for_http` 并不接收它且 `_make` 拒绝无 Task 的 entity_sources（`collaboration_ai_guard.py:1407`）。不能把 task-only 证明直接冒充 HTTP 证明；须扩展已有受控门禁或新建有相同物理发送语义的 HTTP 来源绑定。注意既有锁顺序与回包门禁，不应在模型网络期间持业务写事务。

### 自动提示词与设定可见性

- `setting_ai_context.py:135` 可显式选择当前 `(kind, id)`；`manual_only` 允许明确选择，`never` 不可选择（`:19`、`:167`）；`AiScope.require_entry` 对不可用目标返回 `setting_not_available`（`:77`）。应只取当前目标的**经 AI 投影**字段，并按对应 `field_visible`、`entry_fields`、`ref_visible` 过滤内建字段、自定义字段和跨条目引用；不可将人工 DTO、整个实体或全部关联直接拼入提示词。`setting_ai_visibility.py` 的隐藏字段清单覆盖四类（`schemas/setting_ai_visibility.py:17`）。`build_scope` 可能展开作者的一跳关联，若产品只需当前目标，构词时仍须显式限制输出，不能无意把整个 scope 一并拼入。
- 可见性版本是 `VisibilityStamp(project_id, revision, epoch, last_deleted_at)`（`ai_dispatch.py:285`）。在生成提示词前捕获，构建期间及每次发送前核验。**不能仅在 POST 到达时读一个新戳**：GET 默认提示词后、用户确认之前权限/隐藏字段可能收紧，旧文本却仍留在编辑框中。设计需让默认词预览与 POST 关联原始可见性及来源版本，在变更时要求重新生成/核对；不能自动换新版本重放旧文本。
- 用户可以编辑任意文本：服务端无法可靠地从一段自由文字辨认其是否手动复制了隐藏字段。应明确区分系统自动投影的强约束与用户自行输入的文字；若产品要求即使手工输入也不能提交被隐藏内容，单靠版本戳或字符串过滤不够，需要更严格的结构化输入协议及产品决策。提示词、原始设定、provider 请求和配置不应记入日志。
- 可用文本字段是否包括当前未保存草稿、历史时间点、自定义字段和关联内容，代码未给本生图入口现成合同。建议首版默认只使用已保存、当前目标、清晰列举的可见字段；提示词让用户在提交前检查编辑，并限定总长。图片本身不作为设定 AI 上下文（`.trellis/spec/backend/setting-thumbnails.md:7`）。

### 图片 Provider、归一化与保存

- `LLMProvider.generate_image(prompt, model)`（`backend/app/ai/providers/base.py:66`）返回 `GeneratedImage(data, mime_type)`；不支持的 provider 给中文错误。OpenAI 兼容接口首发 `1024x1536`，只有明确的 400 size 错误再去尺寸重试；返回 base64 或流式下载 URL，后者累计上限 10 MiB、沿用 endpoint/重定向策略（`openai_compat.py:103`、`:122`）。Gemini 走 `generate_content` IMAGE 或 Imagen `generate_images`，后者当前请求 3:4（`gemini.py:199`），部分 SDK/API-key 组合明确不支持 Imagen。**现有 provider 尺寸是封面用途；无法保证正方形，不能在共用 provider 中无条件修改默认值而影响封面。** 如果要控制形状，需要另行定义可兼容可选尺寸参数与 fallback，或接受按比例缩略；现有 codec 不裁方。
- 图片生成路径的 `app/ai/images.py:23` 只做 ≤10 MiB、魔数和 MIME 检查；这不等于可安全显示/存储缩略图。`setting_image_codec.py:47` 使用 Pillow 实际验完整性、单帧、输入最长边 8192、总像素 ≤1600 万，纠正 EXIF 方向后移除元数据，`normalize_image_async` 在线程池限并发 2 下生成最长边 ≤512 的 WebP、≤512 KiB（`:79`、`:131`）。应在返回生成预览前同样归一化，避免把 provider 原始大图/损坏图片发到前端；注意 provider 本身已先限制 **10 MiB 原图**，大于上限不会先缩小再接受。
- 现有 OpenAI URL 下载是流式有界；base64 有编码长度前置检查（`app/ai/images.py:34`），但 SDK 已读取其响应 JSON/字符串；Gemini inline bytes 也是 SDK 返回后再检查（`gemini.py:216`），因此不能笼统声称所有 provider 的接收阶段均流式限内存。
- 生成只返回标准化预览，不立即改当前图；**确认预览不应直接使用普通 PUT**：普通 PUT 在新请求开始时才捕获当前 actor/邀请（`setting_image_service.py:201`），若预览返回后撤权、重新邀请并接受，旧预览可借新邀请保存，不能证明原邀请仍有效。推荐 AI 专用确认 API，把服务端签发的预览证明与待保存标准图一起提交；在单一事务中核验原 actor/token、原邀请、权限/配置/主体/策略等，再复用现有锁、资源写入和图片/系列 CAS。普通 PUT 保持手动上传合同不变。
- 预览证明建议使用**不暴露内部配置细节**的加密认证凭据；单纯签名 JWT 的 payload 客户端可读（`audit_service.py:143` 只是签名示例，不适合承载作者模型配置指纹/身份秘密）。凭据应有严格 purpose、版本、到期、actor 与 token 版本、作用域/主体/生命周期、原邀请轮次、服务端原模型绑定和可见性/来源戳、生成前图片版本及标准 WebP 长度/MIME/SHA-256；不得包含明文密钥或提示词，也不能将其记日志。单书作者模型来源可为 actor，协作者可能为 owner；系列必须为 actor。选择加密令牌时需论证独立域、密钥轮换和到期语义。
- 防篡改与确认：接收文件后限大小，再 `validate_standard_image` 校验真实单帧 WebP 容器和元数据（`setting_image_codec.py:90`），计算实际 bytes 的 SHA-256 与服务端证明匹配，不能信客户端声明的摘要或 MIME；经验证后复用 `set_book_image_without_commit` / `set_series_image_without_commit` 保存，避免普通 PUT 会再次有损编码预览。解码前做授权/凭据预检；正式提交时在正确锁序内重验所有原身份与绑定及当前版本，绑定新资源并提交。并发重放、过期和响应丢失都要有明确定义；严格跨实例一次性需数据库唯一约束/已消费状态，不应只靠进程内标记，本次不作该保证。
- 凭据保护的是本产品的 **AI 预览确认流程**：如果用户后来重新获得人工编辑权，仍可自行保存图片并通过原有上传入口作为普通文件上传。除非改变通用上传产品合同，否则无法阻止有权用户以人工方式上传相同像素，也不应虚称全系统禁止此类上传。
- **无图 ABA**：单书 `_check_version` 仅比较当前资源 UUID 与 `expected_version`（`setting_image_service.py:173`）；无图统一为 `""`，无图→上传→删除后又回到 `""`，旧预览不能发现中间换过图。Agent effects 可恢复旧图片绑定（`agent/effects.py:131`），因此甚至恢复旧 UUID 时也不能只把当前 UUID 当成完整历史。若 AI 确认要求拒绝任何中间图片变更，须在主体存活期保存不会随删除清空或撤销回退的单调代次/墓碑，并在所有有效绑定变化的同一事务中递增；涵盖人工上传/删除、AI 确认、系列提升/移回、卡片与模板、Agent effects 撤销和恢复路径，禁止回滚该代次。可选择更粗但完整且代价可接受的单书作用域代次，也必须逐写路径验证，不能仅加在 HTTP PUT。系列日常写入还带系列 `revision`，普通上传/删除会推进该 revision，能拦住其无图 ABA；系列迁移/恢复等所有实际改绑定路径仍须逐一确认统一推进 revision。既有普通上传/删除的空版本语义保留，不应宣称它们已具备新的严格 ABA 保护。
- `setting_image_service.py:146`、`:201`：上传在解码前捕获实际权限/原邀请，锁内重验权限、主体、系列采用关系后比较版本再写。角色/世界观若已采用系列条目，单书入口会拒绝写共享图，且错误避免向无系列权者泄露系列身份（`series_policy.py:54`）；生成前也应阻止发起不能确认保存的请求，发送/回包时复查该事实。

### 系列共享条目：已确认纳入，使用各自本人图片模型

- 系列独立于单书，`series_service.py:107` 只允许系列 owner 或 accepted editor；管理员身份不自动取得系列权。`setting_image_service.py:265` 在解码前固定系列授权版本，编辑时重验原身份、系列 revision，并按系列→排序作品锁传播投影。一个共享条目可能没有任何采用它的作品，因此不能随机选 `project_id` 作为系列 AI 权限证明；反过来，只有单书编辑权也不能替系列改图。
- 用户已确认系列库也要支持 AI 生成；按 parent 转达的方案，系列 owner 和已接受的系列 editor 都只调用**当前 actor 自己**的图片配置，缺少本人配置时报错并提示配置，不使用作品 `allow_owner_ai`、不回退系列作者模型。显示 `model_source` 可统一标 `actor`，不得公开任何他人配置；单纯管理员和单书协作者不可因人工访问而生图。
- 系列条目 `SeriesEntry.data` 与 `ai_settings` 本身是权威来源（`models/series.py:42`）；`schemas/series.py:49` 列出系列角色的 name/aliases/profile/appearance/personality/background/gender 和世界观的 title/aliases/category/content。系列 `ai_settings` 含 `ai_mode`、`ai_hidden_builtin_fields`（`:79`），应针对系列本体显式选择并过滤：`never` 拒绝，`manual_only` 可由本次显式生图选择，其余允许；各隐藏字段不能自动进入提示词。系列角色和世界观的共享资料不能拼接任一采用作品独有字段/本地自定义字段，也不能把人工 `SeriesEntryOut` 原样交给模型。系列未被任何作品采用时同样可生成。系列条目更新同事务推进 `series.revision`（`series_service.py:740`）；该 revision 是可使用的保守来源/策略版本，不等于单书 `VisibilityStamp`。
- 系列 AI 需要新的受控身份/发送原语；不能伪造项目绑定。开始时由认证 session 固定原始 `AuthenticatedActor`/token 版本、series ID/owner、目标 entry ID/kind、系列 revision、accepted editor 的原始 `(invitation_id, revision)`（owner 无 grant）、本人配置 ID/实例键/指纹。`series_authorization.authorization_version` 仅为客户端代次，**不是**授权凭据（`series_authorization.py:1`）。配置应仅从 actor 本人按现有默认/显式类型规则选取，最终和每个真实请求重验原配置，绝不换配置续发。可抽取配置指纹/受信 provider 与 `DispatchHooks` 的可复用机制，不能将 private 项目 guard 塞入伪造 project ID；`collaboration_ai_guard.for_http`/`ai_dispatch.guard_provider` 的既有租约都绑定项目。
- 每个真正的模型请求、尺寸 fallback、图片 URL 下载/每次跳转必须在**独立短事务**完成认证、系列身份、原邀请轮次、目标及原 revision、配置指纹的复验；租约持到外部响应头，异常和取消释放。`collaboration_service.py:194` 的系列撤权/邀请写路径先认证用户再锁系列父行，`series_service.py:147` 的系列内容编辑也先认证再锁系列父行；设计可用相同父行屏障（PostgreSQL 共享行锁、SQLite 有界 no-op 写屏障）把真实出站与撤权/系列修订串行化，同时按现有顺序取配置锁（`ai_config_service.py:43`）。必须对现有所有系列写入/删除/授权路径逐一证明锁冲突与无死锁；`DispatchHooks` 只是请求钩子，自身不提供这一系列事务证明。收到响应头释放网络发送锁，不持事务等待整个响应体。
- 生成结果标准化后重新在系列父行/配置锁内核验原 actor、邀请、系列 revision、条目和模型；响应持有有界授权锁到 ASGI 完成（可借鉴封面的 5 秒上限），确保撤权后不交付旧预览。最终通过 AI 专用确认路径核验原预览证明，复用 `put_series_image` 的系列授权、锁序和 CAS 语义及其底层资源写入；不能以当前新邀请替代预览原邀请。如果系列 revision 已变化（包括条目内容或 AI 可见性修改），保留用户提示词草稿，停止旧发送/回包/提交，重新获取并显式核对，不静默以新 revision 重发。
- 同一系列 editor 可无权访问部分采用作品；默认提示词只投影系列本体，系列校验错误不暴露采用该条目的书籍身份。最终共享图落库仍交由既有系列 PUT 传播锁与隐私错误语义负责。

### 建议 API 与验证

以下是待设计方案，不是现存接口：在现有单书和系列缩略图路径下各新增 `GET .../thumbnail/prompt`、`POST .../thumbnail/generate` 和 `POST .../thumbnail/confirm`；第一步返回可编辑提示词及与原投影状态绑定的凭据，第二步验证凭据并调用图片模型，返回 `{image_base64, mime_type, model_source?, preview_token}`，第三步提交标准图和预览证明，验证同一原身份/结果并原子写入。不得将客户端版本、actor 或邀请声明当作授权凭据。系列和单书虽然 URL 对称，内部必须使用各自权限、模型与物理发送门禁。具体凭据和防重放方案需与“任意编辑提示词”的隐私边界一起论证。

应覆盖：四类及地点、已保存/删除/跨作品主体、`never`/`manual_only`/隐藏字段及策略在默认词与发送之间变更；viewer、owner、editor、纯管理员、管理员兼 editor；本人配置优先、作者回退授权与错误脱敏；默认词取得后重邀、撤权、变更模型/策略/主体或转系列；图片发送、条件重试、独立下载、回包和 ASGI 卡住/取消的竞争；错误格式、伪造 MIME、损坏、动画、尺寸/10 MiB/标准 512 KiB 上限；预览后撤权重邀再确认、令牌跨 actor/条目/作品/系列使用、图片 bytes 摘要篡改、过期/并发重放；无图→上传→删除和旧图恢复的 ABA；失败/取消不改图片；旧版本/系列旧 revision 冲突；旧人工 PUT 合同不变。已有参考：`backend/tests/test_collaboration_cover_ai.py:185`、`:374`、`:415`、`:601`、`:759`，`test_collaboration_cover_ai_postgresql.py:158`、`:246`，`test_setting_image_codec.py:17`、`:59` 和 `test_setting_thumbnails.py:23`、`:61`；并发门禁需真实 PostgreSQL 加两连接/真实 HTTP，而非只依 SQLite mock。

### 相关规范与外部资料

- `.trellis/workflow.md:5`：研究和决策持久化，复杂任务先计划。
- `.trellis/spec/backend/project-cover.md:23`：封面模型、协作、回包、安全错误、尺寸和预览合同。
- `.trellis/spec/backend/setting-ai-visibility.md:20`：`never`、字段投影、初始可见性戳和每次真实出站规则。
- `.trellis/spec/backend/setting-thumbnails.md:19`：缩略图处理、权限、系列、版本和快照合同。
- `.trellis/spec/backend/series-settings.md`：系列权限及共享设定合同。
- 无外部资料：本次研究针对当前仓库源码及项目规范，未断言第三方 SDK 的仓库以外最新能力。

## Caveats / Not Found

- 本次只读源码及规范并记录研究，未执行测试，也未验证线上模型、SDK/服务商接受的尺寸、真实性能或实际收费。
- 当前任务 PRD 已选择自动生成可编辑提示词及先预览；用户已确认系列库纳入，系列作者和 editor 各自使用本人图片模型。图像形状、默认字段/未保存草稿，以及自由编辑和隐藏字段的边界仍须设计或由产品决定；产品选择不改变既有的单书/系列权限和可见性合同。
- 系列独立发送/回包守卫尚不存在；上述锁法是待实现和跨数据库验证的方案，不能描述为现有保护。
