# Research: 设定缩略图 AI 生成的前端接入

- Query: 在现有设定图片旁增加 AI 生成按钮，核对封面预览范式、四类条目及系列的入口、授权、草稿和竞态处理，以及移动端与异常状态；用户已明确支持系列共享图片。
- Scope: internal
- Date: 2026-11-17

## Findings

### 已有入口和状态

| 场景 | 现有行为与证据 | 生图接入含义 |
| --- | --- | --- |
| 四类书内详情 | 角色抽屉 `frontend/src/pages/project/CharactersPanel.tsx:543`、世界观弹窗 `frontend/src/pages/project/WorldPanel.tsx:217`、组织编辑弹窗 `frontend/src/pages/project/OrganizationsPanel.tsx:247`、职业编辑抽屉 `frontend/src/components/careers/CareerEditDrawer.tsx:134` 都使用 `ProjectThumbnailEditor`。世界观分类包含地点。 | 共用图片控件旁接入最一致；无图和已有图都要可见，不必为每类复制上传逻辑。 |
| 无图 / 有图 | `frontend/src/components/entities/SettingThumbnail.tsx:23` 占位和已授权图片均可展示；`:42` 的控件在无图时显示“上传图片”，有图时显示“替换图片”“删除图片”，按钮容器 `Space wrap`；`:62` 新条目提示先保存。 | 推荐已保存条目且可操作时，上传/替换旁增加“AI 生成图片”，无图和有图均可；未保存的新条目先保存，避免孤立图片和身份不稳定。 |
| 列表和只看详情 | 角色卡 `frontend/src/components/entities/CharacterCard.tsx:56`、世界观列表 `frontend/src/pages/project/WorldPanel.tsx:167`、组织树与详情 `frontend/src/pages/project/OrganizationsPanel.tsx:60` / `:345`、职业卡 `frontend/src/pages/project/CareersPanel.tsx:68`、系列列表 `frontend/src/components/series/SeriesLibrary.tsx:94` 都是 `SettingThumbnail` 展示；图片操作位于编辑器。 | 现有列表图片旁没有上传、删除按钮。用户表述“图片旁边”可先沿用既有详情编辑位置，不自动认定列表也要提供直接生成操作。组织独立只读详情需要先进入编辑弹窗才有图片编辑控件。 |
| 单书共享投影 | 角色/世界观传 `editable={canEdit && !seriesManaged}` 和服务端 `thumbnail_editable`（`CharactersPanel.tsx:546`、`WorldPanel.tsx:219`）；通用组件对这两类要求 `thumbnailEditable === true`，缺省也只读（`SettingThumbnail.tsx:70`）。 | 必须沿用两重图片写权限，再叠加 AI 权限。不能仅凭有图、UI origin 或作品编辑权限开启生图，尤其只获得单书访问的协作者。 |
| 系列共享基础 | `frontend/src/components/series/SeriesLibrary.tsx:109` 书内浏览只提供“在本书使用”/查看共享基础；系列管理页可打开 `SeriesEntryEditor`；该编辑器已有相同 `ThumbnailControls`（`SeriesEntryEditor.tsx:114`、`:132`），更新后刷新被采用的作品（`SeriesLibrary.tsx:129`）。系列只有角色和世界观，含地点。 | 已确认系列共享图片也要生图，应在系列编辑器另接入，不能在单书投影里绕过只读。系列操作作用域是 seriesId/entryId，不等同任意一本成员书。 |

### 权限与模型选择

- 作品封面的生成入口同时要求读取、内容编辑和服务端确认的 AI 能力，身份限作者或真实 editor 协作者，管理员只有人工权限不足以生图（`frontend/src/components/project/coverGeneration.ts:8`）；`CoverPanel.tsx:20` 的按钮不因当前是否有图而变化。为书内设定复用该授权语义，同时再要求该实体图片可写；后端是最终授权方（`frontend/src/hooks/useProjectAccess.ts:9`）。
- 封面的 `coverChoice` 在模型列表加载前保守禁用；有本人图片配置时固定并显式发送配置 ID，无本人配置且作品 editor 获作者模型授权时才允许使用作者默认模型；失效选择不悄悄切换（`frontend/src/components/project/coverGeneration.ts:15`）。`frontend/src/components/AIConfigSelect.tsx:19` 已支持 `modelType="image"`、图片模型列表、未配置时跳转 `/settings/ai`、作者来源费用提示。`CoverGenerateModal.tsx:164` / `:213` 区分“可能使用作者模型”与实际 `model_source`，不披露作者配置细节。
- 系列是独立权限域：系列页明确允许系列设定协作者维护共享基础（`frontend/src/pages/SeriesPage.tsx:72`），服务端接受 owner 或已接受的系列 editor，系列图片更换再核对原授权版本（`backend/app/services/series_service.py:107`、`:148`；`backend/app/services/setting_image_service.py:265`）。仅单书权限、管理员人工权限都不能推导系列能力。
- `AIConfigSelect` 的作者回退判断来自当前路由 `:pid`（`frontend/src/hooks/useAIModelSource.ts:9`），系列页面路由是 `:sid`（`frontend/src/pages/SeriesPage.tsx:21`）；系列成员响应的 `allow_owner_ai` 为 `null`，只有作品成员有此授权（`backend/app/services/collaboration_service.py:59` / `:266`）。因此不能直接拿任意成员书的 `ProjectScope` 或作品模型授权给系列生图。按已确认方案，系列设定协作者使用其**本人**图片模型，系列作者使用自己的模型；不能推断可用作者配置或消费作者费用。具体生图能力、授权变化与配置失效时的服务端合同仍需设计及后端复核。

### 预览、草稿与并发

- 现有封面用服务端预制的可编辑提示词，长度最多 2000，加载失败可重试；生成返回 base64 预览，支持重试，用户明确“设为封面”才转 Blob 走上传，取消不修改已有图（`frontend/src/components/project/CoverGenerateModal.tsx:21` / `:88` / `:136`；`frontend/src/utils/projectCover.ts:14`；`.trellis/spec/backend/project-cover.md:20` / `:33`）。建议缩略图沿用“生成预览 → 明确应用”模型：有图时不立即覆盖，取消或生成失败保留原图，无图时保留占位。应清晰注明图片用途及待确认状态；重新生成期间不能同时应用。
- 生成预览与图片写入是不同阶段；应保存与预览绑定的 actor/授权代次、目标 scope/kind/id、图片原始版本和系列 revision。确认时复核当前能力，带生成开始时捕获的 `expected_version`（无图为空串），系列额外带 `expected_revision`，不能取新 props 的版本替旧预览盲目覆盖。现有 `putThumbnail` 已发送这两个 CAS 参数（`frontend/src/api/settingThumbnails.ts:26`），生成图片也应经过相同规范化与最终权限检查，不能绕开上传接口的内容和尺寸校验。
- 现有控件对 403/404/409 或写入结果未知要求先重新读取图片，保留文字草稿，不自动重放（`frontend/src/components/entities/SettingThumbnail.tsx:98` / `:117`、`frontend/src/utils/writeResult.ts:3`）。系列冲突/未知结果进入核对流程并保留草稿（`frontend/src/components/series/SeriesEntryEditor.tsx:64` / `:84` / `:143`）。建议图片写入超时或断网后明确提示“结果未确认，先读取当前图片”，让用户核对后决定；生成请求发生网络异常也不能假定 Provider 未执行、未计费，应提示后由用户决定是否重新生成，禁止静默自动重复生图。
- 图片更新只合并图片元数据，不能让图片写入结果替换编辑表单文字草稿。角色和职业已经在文字请求完成时用 `createThumbnailRevision` 保留较新的图片（`frontend/src/components/entities/thumbnailSync.ts:9`；`CharactersPanel.tsx:273`；`CareerEditDrawer.tsx:113`）；系列更新仅设置 thumbnail 和 base revision（`SeriesEntryEditor.tsx:125`）。需要在新增预览及并发图片保存时继续遵守该模式。
- 作品身份含 actor、登录请求代次、作品权限快照与 `scopeVersion`（`frontend/src/stores/project.ts:9` / `:22` / `:186`）；封面在 scope 改变或卸载时 abort，并在返回/保存前确认授权（`CoverGenerateModal.tsx:58` / `:66`；`coverGeneration.ts:42`）。系列使用 `useSeriesAction` 的 identity、serial、授权 epoch（`frontend/src/components/series/useSeriesAction.ts:11` / `:22`）及 `SeriesAccessBoundary` 撤权卸载（`frontend/src/components/series/SeriesAccessBoundary.tsx:7`）。新流程还需涵盖关闭/重新打开、切换条目、改变模型、同账号重新登录、撤权后重邀、系列与作品切换以及并发上传/删除；过期生成结果与旧预览不可落入新作用域。若已经发出写入且编辑器关闭，同一授权作用域可刷新缓存；旧账号不得刷新（`thumbnailSync.ts:22`）。
- 封面当前重新生成失败时可能仍显示此前预览，提示词或模型改动也不自动清除预览（`CoverGenerateModal.tsx:88` / `:114`）。新 UI 应明确把每个预览关联到实际提示词/模型和请求代次，设计失效或显示规则，避免用户以为当前预览对应刚修改的输入；不能简单复制这些未区分状态。
- 默认提示词不能直接拼接人工详情、未保存表单或系列原始字段：设定可配置 `never`、隐藏字段及关联字段过滤（`.trellis/spec/backend/setting-ai-visibility.md:20`；`backend/app/services/setting_ai_context.py:52`）。建议先以服务端允许的已保存字段生成可编辑默认提示词，不自动发送隐藏字段、私人风格或当前缩略图；与手工输入提示词的规则和用户可见告知要在设计里明确。

### 验收建议

- 组件与交互：四类已保存条目分别验证有图、无图、生成预览、重新生成、取消、确认；生成/上传/删除互斥；新条目保存后入口出现；列表及只读详情仍正确展示。角色、世界观服务端 `thumbnail_editable` true/false/缺失均覆盖；管理员、viewer、editor、作者以及系列 editor 与仅单书协作者逐项验证。现有权限 UI 示例 `frontend/src/components/entities/SettingThumbnail.test.ts:16`，封面模型与会话测试 `frontend/src/components/project/coverGeneration.test.ts:25`。
- 竞态：模拟图片在生成期间被另一窗口替换或删除、系列 revision 改变、403/404/409、无图版本竞争、超时后实际提交成功、授权代次改变、同账号重新登录、切换条目、关闭弹窗与迟到响应；确认不自动覆盖、不重复扣费、能重新读取且文字草稿保留。现有元数据合并样例见 `frontend/src/components/entities/thumbnailSync.test.ts:13`。
- 模型与可见性：无配置/加载中/无效模型、本人优先、作品内授权回退与费用提示、系列仅本人模型方案、提示词不能包括隐藏字段或未授权私人信息，检查错误不会泄露他人模型配置；模拟格式非法和图片过大，原图保持。
- 桌面和 375px 真实浏览器检查：图片与上传、AI 生成、删除可换行；长条目名称、模型配置提示、提示词计数、预览、底部确认按钮不截断、不横向溢出；键盘焦点、输入标签、图片 alt、加载与错误可读。现有控件虽使用 `Space wrap`（`SettingThumbnail.tsx:44`），仍需验证增加第三个操作后的实际布局；`.trellis/spec/backend/project-cover.md:47` 要求计数在正常文档流，避免压住 footer。

### 已确认方案与待细化的产品选择

- 已确认：覆盖系列共享图片；系列作者及已接受的系列 editor 使用各自本人图片模型。作品内已采用的系列共享图保持只读，不能从该投影绕过系列编辑入口。
- 入口：推荐四类书内编辑详情与系列共享库编辑器均提供；列表保持展示及进入详情，若希望列表“一键生成”需明确，这将新增多处动作及选择/草稿状态。
- 提示词来源：推荐从**已保存、AI 可见**的设定安全生成可编辑提示词，明确提示未保存文字不参与；`never` / 所有字段隐藏是否允许仅填写手工提示词，要由设计明确，不能绕过现行 AI 可见性合同。
- 应用方式与风格：推荐单张预览、可重新生成、用户明确应用替换当前图；如需多候选、比例或风格预设，需要扩展产品范围及验证。生成期间模型可能收费，应在必要的模型来源或错误状态中说明。

## Files found

- `frontend/src/components/entities/SettingThumbnail.tsx` — 通用展示、操作、权限投影及冲突恢复。
- `frontend/src/pages/project/CharactersPanel.tsx`、`WorldPanel.tsx`、`OrganizationsPanel.tsx`、`frontend/src/components/careers/CareerEditDrawer.tsx` — 四类已保存实体图片编辑入口。
- `frontend/src/components/series/SeriesEntryEditor.tsx`、`SeriesLibrary.tsx` — 系列图片操作、版本、共享列表和刷新。
- `frontend/src/components/project/CoverGenerateModal.tsx`、`coverGeneration.ts`、`CoverPanel.tsx` — 模型选择、预览确认、权限作用域与防旧响应范式。
- `frontend/src/components/AIConfigSelect.tsx`、`frontend/src/hooks/useAIModelSource.ts` — 图片模型选择及仅作品路由的作者回退判断。
- `frontend/src/api/settingThumbnails.ts`、`frontend/src/components/entities/thumbnailSync.ts` — 当前图片 CAS、刷新及文本/图片并发合并。
- `frontend/src/stores/project.ts`、`frontend/src/components/series/useSeriesAction.ts`、`SeriesAccessBoundary.tsx` — 作品及系列身份、请求代次和失权清理。
- `backend/app/services/series_service.py`、`setting_image_service.py`、`collaboration_service.py` — 系列协作编辑及无系列作者模型授权字段的代码证据。

## Related specs

- `.trellis/spec/frontend/index.md` — 前端 API 客户端与跨层规范索引。
- `.trellis/spec/backend/setting-thumbnails.md` — 四类图片、权限、版本、系列 revision、草稿与 375px 验收。
- `.trellis/spec/backend/project-cover.md` — 生图提示词、配置来源、预览确认、协作重新鉴权、错误与移动端要求。
- `.trellis/spec/backend/setting-ai-visibility.md` — `never`、字段隐藏、AI 出站和版本戳。
- `.trellis/spec/backend/series-settings.md` — 共享设定和单书投影；其“校验 owner”表述需以当前系列协作代码为准，不应据此否认系列 editor 能编辑。

## External references

- 本次只做本地代码与规范研究，未调查第三方接口或版本；不代表任何图片 Provider 对目标比例、格式或费用的保证。

## Caveats / Not Found

- 本次仅静态阅读，未运行测试或在真实浏览器验证；375px 是验收建议，不能宣称已通过。
- 当前代码尚未发现设定缩略图的专用 AI 生成 API。系列模型授权、提示词生成/可见性、最终生图发送的服务端权限与竞态机制需要独立设计，不能只靠前端禁用按钮保证。
- 系列当前 `SeriesEntryEditor` 可独立于作品打开；未发现可安全选择成员作品作为系列 AI 上下文的现成语义。需保持系列与作品授权边界。
