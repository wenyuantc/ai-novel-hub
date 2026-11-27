# Research: 参考项目媒体生成与文件基础设施，及 aiNovel 差距

- **Query**: 只读调研短剧工坊的媒体生成与文件基础设施（A），核实 aiNovel 现有能力（B），列出 生图 → 生视频 → 拼接导出 的事实性差距（C）
- **Scope**: mixed（两个本地仓库，无外部检索）
- **Date**: 2026-11-27

路径约定：`REF/` = `/Users/wenyuantc/IdeaProjects/yty/ai-hub/ai-drama-workshop/`；`NOVEL/` = `/Users/wenyuantc/IdeaProjects/my/aiNovel/`。
未标注的结论均为「已从代码确认」；凡属推断会写明「推断」。TS 后端（`REF/backend`）与 Go 后端（`REF/backend-go`）行为一致处只引一侧。

---

## A. 参考项目侧

### A.1 图片生成

**生图位置（短剧流水线）**

| 对象 | 入口 | 尺寸 | 参考图 |
|---|---|---|---|
| 角色造型定妆照 | `REF/backend/src/services/look-generation.ts:10-33` | `1920x1080`（`look-image.ts:2`） | 非默认造型以默认造型定妆照作参考并追加「同一张脸」提示（`look-image.ts:24-33`） |
| 场景 | `REF/backend/src/routes/scenes.ts:67-98` | 未传，落到默认 `1920x1080`（`generation.ts:96`） | 无 |
| 道具 | `REF/backend/src/routes/props.ts:104-126` | `1024x1024`（`props.ts:13`） | 无 |
| 分镜首帧/尾帧/合成图 | 通用 `POST /tasks` 带 `storyboard_id` + `frame_type`（`routes/tasks.ts:169-181`），回写见 `generation.ts:519-525` | 请求传入 | 请求传入 `reference_images` |

- 分镜帧图只在后端保留能力：在 `REF/frontend/app` 中检索 `frame_type` / `type: 'image'` 无短剧工作台调用点。当前工作台生视频走「多模态参考」，参考图直接取分镜绑定的造型/场景/道具图（`REF/frontend/app/views/drama/episode.vue:3723-3737`）。
- 风格不是独立参数，而是拼进提示词：项目风格词前置（`look-image.ts:10-20`、`scenes.ts:79-86`、`props.ts:69-80`），优先使用 Agent 生成的 `final_prompt`。

**请求参数与厂商差异**（统一入参 `prompt / model / size / referenceImages / frameType / configId`，`generation.ts:35-49`）

- OpenAI 兼容：无参考图走 `/v1/images/generations`，有参考图走 `/v1/images/edits` 的 multipart `image[]`（`adapters/openai-image.ts:37-52, 93-129`）；按模型归一化尺寸（`:27-35`）。Seedream 模型有参考图时仍走 generations 的 JSON `image[]`（`:39-47`）。
- Gemini：参考图转 `inline_data`，尺寸换算成 `aspectRatio` + `imageSize`（`adapters/gemini-image.ts:49-97, 178-194`），结果只有 base64（`:152-176`）。
- 火山：`/api/v3/images/generations`，参考图放 `image` 字段，尺寸拆成 `width/height`（`adapters/volcengine-image.ts:19-51`）。
- 参考图归一化：本地 `static/` 路径或远程 URL 一律压成 ≤768px、JPEG q68 的 data URL，去重后最多 6 张（`generation.ts:564-607`）。

**返回处理**

- 同步返回 URL → 下载；同步返回 base64 → 直接落盘；返回任务号 → 进入轮询（`generation.ts:337-360`）。
- 落盘到 `static/images/<uuid><ext>`（`utils/storage.ts:13-35, 75-88`），随后生成同目录 `<uuid>_thumb.webp`（宽 400、q78，失败不阻断，`storage.ts:91-112`）。
- 完成后把相对路径回写业务表：分镜帧 / 造型 / 场景 / 道具（`generation.ts:517-536`）。
- 图片请求在传输中被断开时自动重发最多 2 次，每次都会重新生成并计费（`generation.ts:26-27, 329-334`；`services/request-retry.ts:11-24`）。

### A.2 视频生成

**调用模式：四家全部是「提交任务 → 服务端轮询 → 下载落盘」，没有回调（webhook）。**

| 适配器 | 提交 | 轮询 | 证据 |
|---|---|---|---|
| 火山 Seedance 2.x | `POST /api/v3/contents/generations/tasks` → `{id}` | `GET .../tasks/{id}`，`succeeded/failed` | `adapters/volcengine-video.ts:86, 96-106, 108-136` |
| MiniMax H3 | `POST /v2/video_generation` → `{task_id}` | `GET /v2/query/video_generation/{id}` | `adapters/minimax-video.ts:101, 111-120, 122-151` |
| 阿里万相 Wan 3.0 | `POST .../video-synthesis`，头 `X-DashScope-Async: enable` | `GET /api/v1/tasks/{id}`，任务查询有效期 24 小时 | `adapters/aliyun-wan-video.ts:148-155, 165-205` |
| OpenAI 兼容视频 | multipart `POST /v1/videos` | `GET /v1/videos/{id}`；无公网地址时带鉴权下载 `/content` | `adapters/openai-video.ts:75, 87-100` |

- 适配器接口本身就是 构建请求 / 解析提交响应 / 构建轮询 / 解析轮询 四个方法（`adapters/types.ts:48-63`）。
- 轮询节奏：视频每 10 秒一次、最多 3000 次（约 8.3 小时），无总时长上限；图片每 5 秒、最多 120 次且总时长 10 分钟（`generation.ts:29-33`；Go `backend-go/internal/service/generation/engine.go:37-41`）。提交请求单次超时 600 秒（`generation.ts:323`）。
- 单次耗时量级：代码里没有视频耗时的实测值。可确认的旁证是前端单任务最多轮询 15 分钟（`REF/frontend/app/composables/usePollTasks.ts:3-4`），以及注释提到某图片模型出图要 3～4 分钟（`request-retry.ts:6`）。「视频为分钟级」属推断。
- 重试：视频提交**不重试**（重复提交会产生重复任务，`generation.ts:329` 注释）；轮询请求非 2xx 或抛错则下一轮继续（`generation.ts:433, 474-482`）。Go 版对已生成视频的下载另有重试：单次 30 分钟、最多 3 次（`engine.go:141-145`、`poll.go:123-131`）。
- 失败处理：上游明确失败（含内容审核拦截）立即置 `failed` 并写 `error_msg`，不等超时（`generation.ts:469-473`）；网络类错误翻译为中文可读文案（`services/task-errors.ts:23-33`）。
- 并发控制：**没有**。建任务后立即启动后台处理，无队列、无信号量、无按用户/全局的上限（`generation.ts:212`；Go `engine.go:162-173, 342-348`；在两处生成目录检索 semaphore/并发 无命中）。

**输入形态**（统一入参见 `generation.ts:51-74`）

- 火山 Seedance：文本 + 参考图 ≤9 / 参考视频 ≤3 / 参考音频 ≤3，仅「多模态参考」模式；时长 4–15 秒，分辨率 480p/720p/1080p，强制生成音频（`volcengine-video.ts:26, 62-83, 142-147`）。
- MiniMax H3：文本必填，可带首帧/尾帧/参考图/参考视频/参考音频；时长 4–15 秒，分辨率 768P/2K（`minimax-video.ts:74-98, 158-170`）。
- 万相：首尾帧模式与参考模式互斥，另支持文件/网页链接；时长 2–30 秒（`aliyun-wan-video.ts:211-245`）。
- OpenAI 兼容：仅单张首帧参考图（H3 模型经中转站时例外，`openai-video.ts:28-43`）。

**参考资源传给厂商的三种方式**

1. 图片：压成 data URL（base64）内联在 JSON 里（`generation.ts:609-627`）。
2. 图片（OpenAI 兼容视频）：data URL 解码后作为 multipart 文件 `input_reference` 上传（`openai-video.ts:70-74`）。
3. 参考视频/音频/文件：体积大，不内联，必须是公网 URL；本地 `static/` 路径用环境变量 `PUBLIC_BASE_URL` 拼成公网地址，未配置直接报中文错误（`generation.ts:656-673`）。万相适配器明确拒绝非 HTTP(S)/OSS 的视频音频地址（`aliyun-wan-video.ts:61-75`）。
4. （仅 Go）Seedance 可引用厂商素材库 `asset://<ID>`，其余模型发本地副本（`backend-go/internal/service/assetbind/store.go:52-55`）。

### A.3 任务生命周期（`sys_task`）

- 表结构：`type`（image/video）、业务外键（storyboard/drama/scene/character/prop/look）、`provider`、`prompt`、`model`、`params`（JSON，存全部生成参数）、`task_id`（上游任务号）、`result_url`、`local_path`、`status`、`error_msg`、时间戳（`REF/backend/src/db/schema.ts:253-276`）。
- 状态机只有三个值：建任务即 `processing`（`generation.ts:202`）→ `completed`（`:493, 507, 543`）或 `failed`（`:386-393`）。没有 queued / cancelled / paused。适配器内部的 `pending/processing` 只用于判断是否继续轮询，不落库（`adapters/types.ts:128-145`）。
- 进度上报：**没有推送，也没有百分比**。前端轮询 `GET /tasks/:id`（`routes/tasks.ts:217-222`）：单任务每 3 秒、上限 15 分钟（`usePollTasks.ts:3-4, 11-37`）；工作台另有每 4 秒刷新任务列表（`episode.vue:2708`）。
- 取消：**没有取消接口**。只有 `DELETE /tasks/:id` 删除记录（`routes/tasks.ts:240-244`）；后台轮询循环不检查记录是否还在（`generation.ts:402-485`）。推断：删除记录后上游任务仍在跑，完成时文件仍会下载落盘，但不再有记录指向它。
- 恢复：**不恢复**。轮询只存在于进程内存，重启后启动流程把所有 `processing` 记录统一置 `failed`，文案「服务重启，生成任务中断，请重试」（`REF/backend/src/index.ts:97-106`；Go `backend-go/internal/server/server.go:34, 75-85`）。上游任务号虽已落库（`generation.ts:379-384`），但没有代码用它续轮询。
- 拼接任务不走 `sys_task`，另有 `video_merges` 表（`schema.ts:278-294`），前端每 3 秒轮询最新一条（`episode.vue:3814-3825`）。

### A.4 文件存储

- 根目录：`DATA_ROOT` 默认仓库根 `data/`，`STORAGE_ROOT` 默认 `data/static`，可用 `SRW_DATA_DIR` / `STORAGE_PATH` 覆盖（`REF/backend/src/utils/paths.ts:18-23`）。SQLite 库文件与媒体文件同在数据根下，Docker 部署用一个命名卷承载（`REF/docker-compose.yml:8, 32-33`）。
- 目录布局（短剧流水线相关）：`static/images`（生成图）、`static/videos`（生成视频）、`static/merged`（成片）、`static/uploads`（用户上传）、`static/temp`（拼接清单）。占用统计也按这五个桶划分（`utils/dirsize.ts:12-21, 65-71`）。Go 版另有 `static/mv`、`static/ugc`、`static/tools`、`static/asset-hub` 等，属其他模块。
- 命名：一律 `<uuid v4><扩展名>`，扩展名取自 URL 路径或 MIME（`utils/storage.ts:17-18, 52-59, 185-194`）。派生文件靠命名约定而非数据库字段：图片缩略图 `<名>_thumb.webp`（`storage.ts:91-93`），视频海报帧 `<名>_poster.jpg`（取 0.5 秒处、宽 640，`utils/video-poster.ts:9-41`）。
- 数据库里存的是相对路径 `static/<子目录>/<文件名>`（`storage.ts:33-34`），不存二进制。
- 下载方式：整个响应读进内存再写盘，无流式、无大小上限（`storage.ts:30-31`；Go `media/store.go:83-89`）。
- 上传限制：图片无大小与类型校验（`routes/upload.ts:8-21`）；视频 ≤50MB、音频 ≤20MB，按扩展名与 MIME 白名单校验（`upload.ts:23-29, 36-61`）。
- 清理策略：短剧流水线**没有自动清理**。重新生成会产生新 uuid 文件并覆盖业务表里的路径，旧文件留在磁盘（推断，依据：`generation.ts` 与 `ffmpeg-merge.ts` 中除拼接临时清单 `ffmpeg-merge.ts:127` 外无任何删除调用）。成片记录是软删除（`schema.ts:293`、`routes/merge.ts:53`）。设置页只提供占用统计与剩余空间展示（`routes/storage.ts:41-66`）。
- 对外提供：`/static/*` 由应用进程直接托管数据根目录，成功响应带 `Cache-Control: public, max-age=31536000, immutable`（`index.ts:80-86`；Go `server/server.go:51-52`、`server/static.go:52-64`）。
- 访问控制：**没有**。中间件只有日志与错误处理（`REF/backend/src/middleware/` 仅 `logger.ts`；Go `internal/httpx/` 仅 cors/logger/recover），任何能访问端口的人凭 URL 即可读取文件；CORS 白名单只含本机两个端口（`index.ts:41-44`）。访问面靠 `HOST=127.0.0.1` 之类的部署手段收窄（`REF/README.md:98`）。

### A.5 FFmpeg

**短剧流水线实际用到的只有三项能力：**

1. 拼接并统一转码：用 concat 分离器读取文件清单，重新编码为 H.264（libx264、preset medium、crf 23）+ AAC（48kHz、192k），并加 `+faststart`（`REF/backend/src/services/ffmpeg-merge.ts:90-97, 105-124`；Go 等价命令 `backend-go/internal/media/ffmpeg.go:101-105`）。拼接是硬切，按镜号顺序，允许只拼已生成或用户勾选的镜头（`ffmpeg-merge.ts:26-41`）。
2. 时长探测：ffprobe 读成片时长写回记录（`ffmpeg-merge.ts:129-130, 151-158`）。
3. 抽帧：视频 0.5 秒处抽一帧作海报（`utils/video-poster.ts:17-41`；Go `media/ffmpeg.go:96-99`）。

- **短剧流水线里没有字幕、配音合成、背景音乐、转场。** 拼接服务只做上述 concat（`ffmpeg-merge.ts` 全文 158 行）。
- 字幕烧录（ass 滤镜）、响度统一（loudnorm）、BGM 混音（amix）、叠化转场（xfade）、逐段标准化（scale/crop/fps）确实存在，但属于 Go 独有的 UGC 与 MV 模块（`backend-go/internal/service/ugc/exportrender.go:25, 145-160, 194, 254`；`backend-go/internal/service/mv/exportgraph.go:21-36`、`mv/exportrender.go:206`），不在「小说 → 短剧」这条线上。
- 执行方式：拼接是进程内的后台协程，建 `video_merges` 记录后立即返回 `merge_id`（`ffmpeg-merge.ts:62-85`；Go `service/merge/merge.go:96-104`），无队列、无并发上限。
- 拼接前校验：先探测 ffmpeg/ffprobe 是否可执行，再检查每个镜头文件是否仍在磁盘上，缺失时给出可读报错（`ffmpeg-merge.ts:43-56`）。

**运行环境要求**

- 需要 `ffmpeg` 与 `ffprobe` 两个可执行文件。查找顺序：环境变量 `FFMPEG_BIN`/`FFPROBE_BIN` → 与后端二进制同目录 → 系统 `PATH`（`backend-go/internal/media/ffmpeg.go:39-54`）；TS 版用 npm 的 ffmpeg-static（`REF/backend/src/utils/ffmpeg.ts:49-54`）。
- Docker 镜像通过 `apt-get install ffmpeg fontconfig` 安装，并内置字体目录（字体只供 MV 字幕用）（`REF/Dockerfile:53-57, 65-67`）。
- 需要本地可写磁盘：拼接读本地绝对路径、写 `static/temp` 与 `static/merged`（`ffmpeg-merge.ts:90-103`）。所有输入片段必须在同一台机器的文件系统上。
- 重编码是 CPU 密集操作，在应用进程所在机器上同步占用 CPU；代码中没有资源限制或超时设置。

### A.6 模型配置（`ai_service_configs`）

- 字段：`id`、`service_type`、`provider`、`name`、`base_url`、`api_key`、`model`（JSON 数组，第一个为默认模型）、`endpoint`、`query_endpoint`、`priority`、`is_default`、`is_active`、`settings`（JSON，目前只解析 `temperature`）、时间戳（`REF/backend/src/db/schema.ts:184-201`；`services/ai.ts:22-30, 78, 94`）。`api_key` 明文列，配置是全局的，没有归属用户。
- 类别用 `service_type` 一列区分，每类有固定的可用厂商白名单：
  - TS 版三类：`text`（openai/gemini/volcengine）、`image`（openai/gemini/volcengine）、`video`（volcengine/minimax/aliyun/openai_video）（`services/ai.ts:10, 32-36`）。
  - Go 版多两类：`speech`（openai 兼容的文本转语音）、`music`（openai 兼容与本地 mlx-serve）（`backend-go/internal/service/aiconfig/aiconfig.go:14-20`）。这两类只服务 UGC/MV 模块。
- 选择规则：某类型下取 `is_active` 且厂商在白名单内的配置，按 `priority` 降序取第一条（`services/ai.ts:65-97`）。
- 剧集级锁定：每集记录 `imageConfigId` / `videoConfigId`；请求显式带 `config_id` 时优先，其次用剧集锁定值，锁定的配置失效则回退到当前启用配置（`routes/tasks.ts:128-142`、`generation.ts:77-83, 120-126`）。
- 没有用量或费用记录：`sys_task` 无 token/费用字段（`schema.ts:253-276`），生成流程中也没有计量调用。

---

## B. aiNovel 侧

### B.7 封面与设定缩略图的存储

**存在哪里：两者都是数据库里的二进制列，不是磁盘文件。**

| 项 | 项目封面 | 设定缩略图 |
|---|---|---|
| 表与列 | `project_covers.data`（`LargeBinary`），主键即 `project_id`，一书一张（`NOVEL/backend/app/models/entities.py:299-304`） | `setting_image_resources.data`（`LargeBinary`）+ 独立的绑定表 `setting_thumbnail_bindings`，资源不可变、换图只改绑定（`NOVEL/backend/app/models/setting_images.py:22-40, 43-75`） |
| 格式 | JPEG/PNG/WebP，按文件头魔数识别，不信扩展名与客户端 MIME（`NOVEL/backend/app/ai/images.py:8, 12-31`） | 输入同三种格式且必须能实际解码为单帧图；**统一转存为 WebP**（`NOVEL/backend/app/services/setting_image_codec.py:22, 83`） |
| 大小 | 原图 ≤10MB，**按原字节入库，不缩放、不生成缩略图**（`images.py:6`；`NOVEL/backend/app/services/cover_service.py:59-68`） | 输入 ≤10MiB、边长 ≤8192、像素 ≤1600 万；入库的标准图最长边 ≤512、≤512KiB（`setting_image_codec.py:16-20`） |
| 上传拦截 | 中间件在 multipart 解析前按路由拦截超大请求体（`NOVEL/backend/app/main.py:224-227`），路由再读 `MAX+1` 字节兜底（`NOVEL/backend/app/api/v1/project_cover.py:42`） | 同一中间件的另一条规则（`main.py:217-223`） |
| 对外提供 | 鉴权接口 `GET /api/v1/projects/{id}/cover`，用 `Response` 一次性返回全部字节（`project_cover.py:25-32`） | 鉴权接口 `GET .../settings/{kind}/{id}/thumbnail` 与系列版（`NOVEL/backend/app/api/v1/setting_images.py:47-51, 68`） |
| 缓存 | 带版本参数 `v` 时 `private, max-age=31536000, immutable`，否则 `private, no-cache`（`project_cover.py:31`） | `private, no-cache` + ETag；查询参数不构成授权（`NOVEL/.trellis/spec/backend/setting-thumbnails.md:23`） |
| 鉴权 | 读需 `content.read`，写需 `content.edit` 且锁父项目行（`cover_service.py:47-52, 55-56`）；无权或不存在一律 404 | 读写都按当前主体与绑定解析，并核对协作邀请身份（`setting-thumbnails.md:23-24`） |

- 没有静态目录托管用户内容：应用只把前端构建产物挂在 `/assets`（`NOVEL/backend/app/main.py:92`）。规范明确写明封面「不引入独立图片存储目录或数据卷」（`NOVEL/.trellis/spec/backend/project-cover.md:42`）。
- 是否进备份：
  - 整库备份：PostgreSQL 用 `pg_dump -Fc`、SQLite 用 `Connection.backup`（`NOVEL/.trellis/spec/backend/admin-backups.md:60`）。图片是库内行，因此随整库备份走（推断，依据为存储位置 + 备份方式，未逐表核对 dump 内容）。
  - 单书备份：两张图片表都登记为「不走通用行导出」，封面改为备份顶层 `cover` 字段的 base64，缩略图导出到顶层 `thumbnails`（`NOVEL/backend/app/services/project_transfer/registry.py:112, 122`）；单书备份总上限 100MB（`NOVEL/backend/app/services/project_transfer/thumbnails.py:14`）。

### B.8 `generate_image` 的参数与图生图能力

- 签名只有两个参数：`generate_image(self, prompt: str, model: str) -> GeneratedImage`，基类默认抛「不支持图片生成」（`NOVEL/backend/app/ai/providers/base.py:68-71`）。返回值是 `GeneratedImage(data: bytes, mime_type: str)`，即单张图的字节。
- **没有** 尺寸、宽高比、张数、参考图、风格、种子等任何可选参数；**没有图生图**。
- OpenAI 兼容实现：尺寸写死先请求 `1024x1536`，仅在明确的 400 且报错含 size 时去掉尺寸重试一次；固定 `n=1`；只调 `images.generate`，没有用 `images.edit`（`NOVEL/backend/app/ai/providers/openai_compat.py:173-198, 200-212`）。结果优先取 base64，否则流式下载 URL 并限 10MB、拒绝重定向（`openai_compat.py:160-171`）。
- Gemini 实现：Imagen 模型写死 `aspect_ratio="3:4"`、一张；原生模型用 `generate_content` 且 `contents` 只传提示词文本，不带任何图片输入（`NOVEL/backend/app/ai/providers/gemini.py:247-296`）。
- Anthropic 沿用基类的不支持错误（`project-cover.md:41`）。
- 都是同步调用：一次请求内等到图片返回，总超时沿用 `AI_REQUEST_TIMEOUT`，默认 300 秒（`NOVEL/backend/app/core/config.py:46`）。没有「提交后轮询」的图片路径。
- 调用链外层的封装同样只透传这两个参数：调度层（`NOVEL/backend/app/services/ai_dispatch.py:715-716`）、用量记录层（`NOVEL/backend/app/services/ai_usage_recording.py:256`）、协作授权门禁（`NOVEL/backend/app/services/collaboration_ai_guard.py:205`）。
- 用量计量单位：token 数与 `image_count`（`NOVEL/backend/app/ai/providers/usage.py:27`、`openai_compat.py:209-211`）。没有视频秒数、分辨率之类的计量维度。
- 调用方目前只有三处，且都只返回预览、不直接落库：封面（`cover_service.py:151, 176`）、作品设定缩略图（`NOVEL/backend/app/services/project_thumbnail_ai.py:209`）、系列缩略图（`NOVEL/backend/app/services/series_thumbnail_ai.py:273`）。三处都在 HTTP 请求内同步完成，**没有任何一处走后台任务引擎**。
- 模型配置的类型只有 `text` / `image` / `embedding`（`NOVEL/backend/app/schemas/ai_config.py:8`）。

### B.9 任务引擎与「提交后需轮询数分钟的外部任务」

**引擎形态**

- `tasks` 表即队列，进程内调度器按 id 先进先出领取；类型清单固定为 22 种，均为文本生成、分析或向量索引类，无媒体类（`NOVEL/backend/app/models/entities.py:61-84`；表定义 `:861-928`，`kind` 为 `String(32)`）。状态七种：queued / running / paused / succeeded / failed / cancelled / interrupted（`entities.py:85`）。
- 处理器是协议：`validate_and_claim`（建任务时同步鉴权与抢占）→ `build_job` → `run`（异步生成器，产出 delta/stage/paused/done/error 事件）→ `on_abort`（幂等清理）（`NOVEL/backend/app/services/task_engine/registry.py:52-87`）。
- 进度是推送：`GET /tasks/{id}/stream` 走 SSE，输出与进度每 2 秒节流刷库，订阅空闲时每 15 秒心跳（`NOVEL/backend/app/api/v1/tasks.py:59-76`；`NOVEL/backend/app/services/task_engine/engine.py:264-267`）。
- 并发名额：全局默认 4、每用户默认 2，其余排队；运行中的任务在整个执行期间占用一个名额（`NOVEL/backend/app/core/config.py:57-58`；`engine.py:1034`）。
- 多实例：执行者持有租约，心跳 5 秒、租约 30 秒（`engine.py:270-271`）；双实例示例见 `NOVEL/docker-compose.multi.yml:33-43, 66-81`。

**有没有长时间等待外部结果的先例**

- **没有「提交 → 轮询外部任务」的处理器。** 在全部处理器（`task_engine/handlers/*.py`）中检索 `asyncio.sleep` / `poll` 无命中。
- 最接近的两个先例，都不是轮询：
  - `chapter.batch`：一个任务内逐章生成加分析，可运行很久；断点存 `step_cursor` 与 `checkpoint`（JSON），审核点可转 `paused`，之后由 `resume` 继续（`NOVEL/backend/app/services/task_engine/handlers/batch.py:1-5`；`entities.py:911-913`）。它等的是流式模型输出，每次请求仍受 300 秒上限约束。
  - `agent.turn` 的 MCP 工具调用：连接初始化 15 秒、**单次调用上限 30 秒**（`NOVEL/backend/app/services/mcp/client.py:35-38`）。属于短时同步调用。
- 单次 AI 请求超时默认 300 秒（`config.py:46`）。引擎层面未见针对整个任务的总时长上限（未找到相关配置项；属「未发现」而非「确认不存在」）。

**取消**

- `POST /tasks/{id}/cancel`（`tasks.py:79`）→ `cancel_task`（`NOVEL/backend/app/services/task_service.py:1041`）→ 引擎取消对应的 asyncio 任务并最多等 10 秒收尾（`engine.py:273-274, 3499-3507`）。取消请求会落库到 `cancel_requested_at`，可跨实例由另一执行者接管收尾（`entities.py:924`；`engine.py:664-723`）。
- 清理由处理器的 `on_abort` 负责。取消的是本地协程；现有代码里没有「通知外部服务取消其任务」的环节。

**进程重启后的恢复**

- 机制是「接管并标记中断」，**不是续跑**：存活执行者扫描租约过期的 `running` 任务，认领后调用 `on_abort` 并置为 `interrupted`，文案「服务重启，任务已中断」（`engine.py:92, 660-662, 725-768, 799-918`）。规范原文：「重启仍按原中断规则，不重放模型」（`NOVEL/.trellis/spec/backend/admin-task-monitoring.md:135`）。
- 之后靠用户手动重试：failed / cancelled / interrupted 可重试，重试是**新建一条任务**并记 `retry_of_id`（`task_service.py:51, 697-727`；`tasks.py:89`）。批量任务重试沿用原范围与断点，最多重做当前章的当前阶段（`NOVEL/.trellis/spec/backend/batch-generate.md:74`）。
- 外部调用的既有处理方式：Agent 的外部（MCP）调用若停在 `executing` 超过 2 分钟，被标记为「外部调用结果未知，请核对」，**不重新调用**，理由是重新调用可能重复已完成的操作（`NOVEL/backend/app/services/agent/service.py:74-75, 148-156`）。
- 小结（事实层面）：`checkpoint` 字段具备持久化任意 JSON 的能力，批量任务已在用；但现有处理器里没有任何一个会在重启后凭已保存的外部任务号继续等待同一个外部任务。

### B.10 大文件 / 二进制对象的存储抽象

- **除整库备份外没有。** 备份有一套本地目录与 S3 的存储实现（`LocalStore`、`S3Store`，`NOVEL/backend/app/services/backups/storage.py:60, 347`），但它是备份专用的：文件名由执行键派生（`storage.py:35`），根目录是单一的 `BACKUP_DIR`（`config.py:67`），与实例身份和备份记录绑定，不是通用对象存储接口。S3 依赖 `boto3` 已在项目依赖中（`NOVEL/backend/pyproject.toml:27`）。
- 其余二进制一律放数据库列（`LargeBinary`）：项目封面（`entities.py:304`）、设定图片（`setting_images.py:35`）、小说模板内嵌图片（`NOVEL/backend/app/models/novel_templates.py:42-57`）、向量（`entities.py:1267`）、导入预览密文（`NOVEL/backend/app/models/project_import_preview.py:49`）。
- 部署层面没有媒体数据卷：双实例示例中两个应用实例只共享备份目录，另有数据库卷（`docker-compose.multi.yml:9-10, 59-60, 85-86`）。
- 现有最大的上传入口是知识库文档与拆书导入，20MB（`NOVEL/backend/app/services/kb_parser.py:35`；`main.py:196-245`）。上传上限靠逐路由登记的中间件规则，未登记的路由不受该中间件约束。
- 镜像里没有 FFmpeg：运行镜像只装了 ca-certificates、curl、gnupg、tzdata 与 PostgreSQL 16 客户端（`NOVEL/Dockerfile:18-24`）；在 `backend/app`、`pyproject.toml`、`Dockerfile` 中检索 ffmpeg / ffprobe / moviepy / opencv / pydub 无命中。图像处理库只有 Pillow（`pyproject.toml:31`）。
- 前端没有音视频播放：在 `NOVEL/frontend/src` 检索 `<video` / `<audio` 无命中。

---

## C. 差距清单

范围：在 aiNovel 内实现参考项目的 生图 → 生视频 → 拼接导出 所需的基础设施。只列事实，不含方案。业务数据模型（剧、集、分镜、造型等表）由另一份调研负责，不在此列。

| 能力 | aiNovel 已具备 | 需要扩展 | 完全没有 |
|---|---|---|---|
| 图片模型接入 | OpenAI 兼容与 Gemini 的文生图（B.8） | 签名只有 `prompt, model`；尺寸与比例写死为竖版；固定一张；缺火山 Seedream 这类厂商协议 | 参考图输入（图生图 / edits / inline 图片） |
| 视频模型接入 | — | — | 任何视频厂商适配；提交/轮询/下载三段式协议；首帧、尾帧、多模态参考等输入形态 |
| 语音与音乐模型 | — | — | 全部（参考项目短剧线也未使用，仅其 UGC/MV 模块用） |
| 模型配置类别 | `text` / `image` / `embedding` 三类，按用户归属，含协作授权回退 | 类别枚举不含视频 | `video` / `speech` / `music` 类别；剧集级模型锁定 |
| 参考资源传递 | AI 端点的私网与重定向防护、下载大小限制（B.8） | — | 把库内图片压缩为 data URL 或 multipart 发给厂商的归一化；供厂商回源的公网可访问地址（参考项目的 `PUBLIC_BASE_URL`） |
| 用量与费用 | token 与 `image_count` 计量、用量记录层、协作授权门禁 | 计量维度不含视频时长、分辨率 | 视频/音频的计费单位 |
| 后台任务 | 持久化队列、SSE 推送、取消、手动重试、多步断点、双实例租约接管、全局与每用户并发上限（B.9） | 任务类型清单无媒体类；单次请求 300 秒上限与名额占用模型是按流式文本设计的；重启语义是「标记中断」 | 轮询外部任务的处理器；重启后凭外部任务号续等同一任务；向外部服务发起取消 |
| 图片存储 | 库内二进制列、魔数与解码校验、WebP 规范化、鉴权读取、版本化缓存（B.7） | 封面只存原图无缩略图；设定图只存 ≤512px 的标准图、不留原图；单张上限 10MB | 同一张图的「原图 + 列表缩略图」双版本 |
| 视频与大文件存储 | 仅备份专用的本地目录与 S3 实现；`boto3` 依赖已在 | 上传大小规则需逐路由登记，现有最大 20MB | 通用的文件/对象存储抽象；媒体数据卷；双实例间共享的媒体存储；视频文件落盘与流式下载 |
| 媒体对外提供 | 图片经鉴权接口一次性返回全部字节 | — | 视频的分段（Range）传输；任何用户内容的静态托管 |
| 访问控制 | 多用户、项目级 `content.read` / `content.edit` / `ai.use`、协作邀请核验（参考项目完全没有） | 现有权限点未覆盖媒体资源类型 | — |
| 视频处理 | — | — | FFmpeg 与 ffprobe 运行时（镜像未安装）；拼接转码；时长探测；海报帧抽取 |
| 文件生命周期 | 设定图资源随作品永久删除而级联清理；备份按保留份数清理 | — | 媒体文件的清理策略、磁盘占用统计、按用户的存储配额（参考项目也只有占用统计，无自动清理） |
| 备份 | 整库备份（本地与 S3）、单书备份内嵌图片（上限 100MB） | 单书备份的 100MB 上限与 base64 内嵌方式是按小图设计的 | 数据库之外的文件进入备份的途径 |
| 前端 | 图片上传、预览确认、版本化图片地址 | — | 音视频播放组件 |

### 「完全没有」一栏汇总

1. 视频生成的厂商适配与 提交 → 轮询 → 下载 流程。
2. 图片生成的参考图（图生图）输入。
3. 语音、音乐模型，以及模型配置里的视频/语音/音乐类别。
4. 把本地素材交给厂商的通道：data URL/multipart 归一化，以及厂商可回源的公网地址。
5. 能在进程重启后续等同一个外部任务的任务处理器，以及向外部服务发起取消。
6. 通用的大文件存储抽象、媒体数据卷、双实例共享的媒体存储。
7. 视频的落盘、分段传输与前端播放。
8. FFmpeg/ffprobe 运行时及拼接、转码、时长探测、海报帧。
9. 媒体文件的清理、占用统计与配额。
10. 数据库之外的文件进入备份的途径。

## Caveats / Not Found

- 视频生成的单次真实耗时在参考项目代码中没有实测数据，只有轮询上限（约 8.3 小时）与前端 15 分钟上限两个边界；「分钟级」为推断。
- 参考项目分镜首帧/尾帧生图只确认了后端能力与回写逻辑，前端短剧工作台未发现调用点；是否为已下线功能未能从代码判断。
- 「整库备份包含封面与缩略图」是由「图片存于库内」加「备份方式为 pg_dump / SQLite backup」推出，没有实际检查备份产物。
- aiNovel 任务引擎是否存在针对整个任务的总时长上限：未找到相关配置或代码，但 `engine.py` 有 4353 行，未逐行通读。
- aiNovel 小说模板图片的 50MB 常量（`NOVEL/backend/app/schemas/novel_template_snapshot.py:27`）只看到定义，未核对它是单张还是总量上限。
- 未读取任何 `.env`；未执行参考项目的 OpenWolf / `.wolf` 流程；未修改参考项目任何文件。
- 参考项目的产品流程与业务数据模型不在本文范围。

## Related Specs

- `NOVEL/.trellis/spec/backend/project-cover.md` — 封面存储、生成预览、备份与鉴权契约
- `NOVEL/.trellis/spec/backend/setting-thumbnails.md` — 设定缩略图的资源/绑定模型与 AI 生成确认
- `NOVEL/.trellis/spec/backend/admin-backups.md` — 整库备份的方式、本地与 S3 存储
- `NOVEL/.trellis/spec/backend/batch-generate.md` — 多步任务断点与中断续跑
- `NOVEL/.trellis/spec/backend/admin-task-monitoring.md` — 任务取消、中断与重启语义
- `NOVEL/.trellis/spec/backend/task-stream-peers.md` — 双实例任务流
