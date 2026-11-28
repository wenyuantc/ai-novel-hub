# 短剧插件（媒体制作）：技术设计

> 需求见同目录 `prd.md`（P1–P10）；决定编号见父任务 `prd.md`（D9、D12、D18）。
> 建立在三项前序设计之上：`11-27-media-storage/design.md`（存储）、`11-27-ai-media-models/design.md`（图片选项、视频协议、外部任务台账）、
> `11-27-short-drama-plugin/design.md`（数据模型与任务约定）。调研依据：`11-27-short-drama-plugin/research/` 两份文档。

## 1. 设计原则

1. **业务表就是进度。** 每张图、每个视频版本都是一行带状态的记录；任务只是推动这些行前进。页面刷新、任务重试、进程重启后，
   进度都从这些行恢复，而不是从任务的内存状态恢复。
2. **参考图在服务端计算。** 参考项目在前端临时拼参考图（调研 5.4）；这里放到服务端，保证预览看到的和实际发给模型的是同一份。
3. **版本是一等数据。** 参考项目用任务记录推导视频版本（调研 5.3），删任务等于删版本；这里用独立的版本表。
4. **成片在服务器本地盘上合成。** FFmpeg 需要本地文件；输入先取到临时目录，输出再存回媒体存储，不要求媒体存储本身是本地盘。

## 2. 结构

在 `backend/app/plugins/drama/` 内新增：

```
models.py            # 追加 4 张表与若干列
services/images.py   # 资产图：生成、上传、选定、从小说设定带入
services/references.py   # 分镜参考图计算与提示词里的 @ 替换
services/videos.py   # 分镜视频版本
services/exports.py  # 成片
handlers.py          # 追加 drama.image / drama.video / drama.merge
api/media.py         # 相关接口
backend/app/media/ffmpeg.py        # FFmpeg 封装（放在核心媒体包里，供其他功能复用）
backend/alembic/versions/0084_drama_media.py     # 编号以实施时的链尾为准
Dockerfile           # 运行镜像安装 ffmpeg
```

前端在 `frontend/src/plugins/drama/components/` 增加资产图、视频制作、导出三组组件。

## 3. 数据模型

### 3.1 资产图 `drama_asset_images`

| 列 | 说明 |
| --- | --- |
| `id` | |
| `owner_type`、`owner_id` | `look` / `scene` / `prop` 及其 id；删除资产时由服务层一并标记删除 |
| `media_object_id` | 媒体对象 id；生成中为空 |
| `source` | `generated` / `uploaded` / `novel`（从关联的小说设定缩略图带入） |
| `status` | `pending` / `ready` / `failed` |
| `prompt` | 生成时实际使用的提示词快照 |
| `reference_image_ids` | JSON，生成时使用的参考图（其他资产图的 id） |
| `error_message` | |
| `is_selected` | 每个资产至多一张被选定，用作后续参考与展示 |
| 时间戳 | |

每个资产最多保留 10 张；新图生成成功且该资产尚无选定图时自动选定。

### 3.2 分镜视频版本 `drama_shot_videos`

| 列 | 说明 |
| --- | --- |
| `id`、`shot_id`（CASCADE） | |
| `job_id` | 外键 → `ai_external_jobs`，SET NULL |
| `status` | `pending`（已受理未提交）/ `generating` / `ready` / `failed` / `cancelled` |
| `media_object_id`、`poster_object_id` | 视频与海报帧 |
| `duration_ms` | 实际时长（探测得到） |
| `params` | JSON 快照：提示词（替换 `@` 之后）、时长、画幅、分辨率、模型、参考图的资产图 id 列表 |
| `error_message` | |
| `is_selected` | 每条分镜至多一个被选定，成片使用它（P5） |
| 时间戳 | |

每条分镜最多 10 个版本（见 `prd.md` 细节取值）；首个成功的版本自动选定。

### 3.3 成片 `drama_exports`

`id`、`episode_id`（CASCADE）、`status`（`pending` / `processing` / `ready` / `failed`）、`shot_video_ids`（JSON，有序）、
`media_object_id`、`duration_ms`、`error_message`、时间戳。一集可有多条成片记录。

### 3.4 已有表追加的列

- 工作台顶栏统一选择文本、图片、视频三类模型。`0085` 新增 `drama_episode_model_preferences`，按集和当前账号保存三个可空配置引用，防止协作者相互覆盖或看到别人的私有配置。删除配置时引用置空。
- 媒体对象的归属：`scope=project`、`project_id` 为所属作品、`plugin_id='drama'`（停用插件后不可访问，P10）。

## 4. 资产图（P1–P3）

本轮先交付剧级素材库和集工作台的角色造型。单目标 `drama.image` 使用图片模型，即使没有文本模型也可发起。优先使用造型已保存的图片提示词；留空时，从允许发送给模型的角色外貌、造型和视觉风格直接组装输入。服务端记录实际使用的提示词及参考图。迁移 `0084_drama_asset_images` 只建立此次需要的图片历史表。后续视频与导出表使用新的迁移编号。


- 生成：任务 `drama.image`，输入为一组目标（若干造型 / 场景 / 道具）。逐个目标：
  缺图片提示词时先在任务内生成（复用文本段的提示词逻辑）→ 组装选项 → 调用图片模型 → 存入媒体存储 → 新增一行并置 `ready`。
  单个目标失败只影响该目标，任务继续处理其余目标并在结束时汇总。
- 画幅：造型与场景用横向画幅，道具用方形（参考项目的取值，调研 A.1）；风格预设的提示词前缀拼在最前。
- 造型一致性（P2）：非默认造型生成时，把该角色默认造型的选定图作为参考图，并追加「同一张脸」的提示。
  默认造型自身：若关联了小说角色且该角色有设定缩略图，可选择以它作为参考（P3）。
- 模型不支持参考图时：任务对该目标给出明确原因（来自核心的报错），不退化为无参考生成，避免悄悄得到不一致的脸。
- 上传：`POST` 多部分表单，JPG / PNG / WebP、不超过 10MB，按文件头校验；`source=uploaded`。
  上传体积限制需要在挂载时登记：给框架清单补 `upload_limits`（路径模式与上限），由应用装配时并入现有的请求体上限规则
  （`backend/app/main.py:225` 一带的列表）。这是本子任务对框架的补充。
- 从小说设定带入：把关联角色 / 世界观条目的设定缩略图复制为一张 `source=novel` 的资产图（读取数据库内图片后写入媒体存储）。
- 选定、删除：选定即改 `is_selected`；删除即标记媒体对象删除并删行；被选定的那张删除后自动选定最新的一张。

## 5. 分镜视频（P4–P6）

### 5.1 参考图计算

`services/references.py::resolve(shot) -> ShotReferences`（参考项目 `shot-references.ts` 的服务端等价实现，调研 5.4）：

- 顺序：场景的选定图 → 各引用造型的选定图（默认造型在前）→ 各道具的选定图；去重；超过模型上限的丢弃并在结果里列出。
- 分镜只引用了角色而未指定造型时用默认造型；造型没有选定图则跳过并记入「缺图」清单，不用别的造型顶替。
- 名称登记：`角色名·造型名`、裸 `角色名`（指向该角色本镜第一套有图的造型）、场景名、道具名。
- `@` 替换：把视频提示词里的 `@名称` 换成 `@图片N名称`，最长名优先，重复执行结果不变。
- 接口 `GET /shots/{id}/references` 返回将使用的参考图、缺图清单、替换后的提示词与当前模型的能力上限，供界面在生成前展示（P4）。

### 5.2 生成

任务 `drama.video`，输入为一组分镜 id（最多 30 条）。受理时在同一事务内：

1. 校验每条分镜有视频提示词、版本数未达上限。
2. 为每条分镜新增一行 `drama_shot_videos`（`pending`，写入 `params` 快照）与一行 `ai_external_jobs`（`created`）。
3. 预算按总秒数预检。

执行：调用核心的驱动器（`app.ai.external_jobs.drive`），`parallel=3`。驱动器推进台账行，处理器在每个台账行状态变化时同步对应的版本行，
并把逐条状态发到任务的实时输出。成功时：结果已由驱动器存入媒体存储 → 探测时长、抽海报帧 → 版本行置 `ready`。

- 重启与重试：版本行与台账行都在库里，任务重试时沿用同一批行，已提交的不再提交（核心设计 5.3）。
- 单条失败不影响其余；失败的可单独重试（新建一个只含该分镜的任务，产生新版本行）。
- 取消任务：未结束的台账行尽力通知厂商，版本行置 `cancelled`。
- 资源键 `drama.episode:{id}:video`：同一集同时只有一个视频任务；与文本段的集级互斥键分开，生成视频期间仍可编辑其他分镜的文字。
- 进度视图（P6）：`GET /episodes/{eid}/shots` 的每条分镜带上各版本及状态；页面在有进行中任务时订阅任务的实时输出，否则静态读取。

### 5.3 选定与删除

选定版本即改 `is_selected`（同分镜互斥）。删除版本：进行中的不可删（先取消任务）；删除即标记媒体对象（视频与海报）删除并删行；
被选定的删除后改选最新的 `ready` 版本。

## 6. 成片导出（P7、P9）

### 6.1 FFmpeg 封装 `app/media/ffmpeg.py`

- 查找顺序：环境变量 `FFMPEG_BIN` / `FFPROBE_BIN` → 系统 `PATH`；找不到时抛出带明确文案的错误（P9）。
- `probe_duration(path) -> int`、`extract_poster(path, out, at=0.5)`、`concat(inputs, out, *, width, height)`。
- 拼接参数沿用参考项目的做法（调研 A.5）：concat 读取文件清单、统一重编码为 H.264（libx264、preset medium、crf 23）+ AAC（48kHz、192k）、
  `+faststart`；另外统一缩放到本集分辨率并补黑边，使不同模型产出的片段可以拼接。
- 用 `asyncio.create_subprocess_exec` 执行，参数列表传递（不经 shell），设超时；标准错误截断后记日志，不直接回显给用户。
- Dockerfile 的运行阶段在现有 `apt-get install` 里加入 `ffmpeg`（`Dockerfile:18`）。

### 6.2 导出任务

任务 `drama.merge`，输入为有序的分镜 id 列表（界面勾选，P7）。受理时为每条分镜取其选定版本，缺选定版本的分镜拒绝受理并指出；
新增一行 `drama_exports`（`pending`）。执行：把各版本文件从媒体存储取到临时目录 → `concat` → 探测时长 → 存入媒体存储 → 置 `ready`；
临时目录在 `finally` 中清理。失败置 `failed` 并写可读原因。

- 串行键 `drama.merge`：全实例同一时间只跑一个拼接任务，避免占满处理器（参考项目没有任何限制，调研 A.5）。
- 标记完成：`PATCH /episodes/{eid}` 把状态置为 `completed`（已有接口）。
- 下载：经媒体读取接口带 `?download=1`。

## 7. 接口

前缀 `/api/v1/plugins/drama`。读需 `content.read`，写与上传需 `content.edit`，生成需 `ai.use`。

| 接口 | 说明 |
| --- | --- |
| `GET /assets/{type}/{id}/images` | 某资产的图片列表 |
| `POST /assets/{type}/{id}/images/upload` | 上传 |
| `POST /assets/{type}/{id}/images/from-novel` | 从小说设定带入 |
| `PATCH /asset-images/{id}`、`DELETE` | 选定、删除 |
| `GET /shots/{id}/references` | 生成前预览 |
| `PATCH /shot-videos/{id}`、`DELETE` | 选定、删除 |
| `GET /episodes/{eid}/exports`、`DELETE /exports/{id}` | 成片列表、删除 |
| `POST /episodes/{eid}/tasks` | 复用文本段的任务受理入口，`kind` 增加 `drama.image`、`drama.video`、`drama.merge` |
| `GET /episodes/{eid}/models`、`PATCH` | 当前账号在本集选用的文本、图片、视频模型 |

## 8. 前端

- 资产步骤：卡片显示选定图的缩略图；「生成 / 重新生成」「上传」「从小说设定带入」；图片历史抽屉（切换选定、删除）；批量生成按钮。
- 视频制作步骤（新增）：三栏布局，对齐参考项目（调研 2.4）——左栏分镜列表（状态筛选、多选），中栏检查器（沿用文本段的编辑控件，
  加上参考图预览与缺图提示），右栏播放器、版本列表、生成参数。批量生成前弹确认，显示条数、总时长与预估费用。
- 导出步骤（新增）：上半区成片列表（预览、下载、删除、标记完成），下半区分镜网格（勾选、版本角标切换）。
- 图片与视频一律用核心的 `MediaImage`、`MediaVideo` 组件；任务进度用现有的流式任务钩子。
- 查看者：可预览与播放，生成、上传、删除、导出的控件隐藏。

## 9. 备份与删除（D18、P10）

- `drama_asset_images`、`drama_shot_videos`、`drama_exports` 整表不导出（内容全是媒体引用）；
  `drama_episode_model_preferences` 是个人模型偏好，也整表不导出。导出对话框在作品含短剧媒体时显示「短剧的图片与视频不包含在备份中」。
- 删除资产、分镜、集、剧时，服务层先标记其下全部媒体对象删除，再删业务行；作品彻底删除由媒体存储的作品级标记处理。
- 插件停用：媒体对象带 `plugin_id='drama'`，读取接口拒绝；数据与文件保留。

## 10. 权衡记录

| 问题 | 选择 | 放弃的方案与原因 |
| --- | --- | --- |
| 视频版本的表示 | 独立版本表 | 由任务记录推导（参考项目）：任务记录会被清理，版本不能依赖它 |
| 参考图在哪算 | 服务端 | 前端计算（参考项目）：预览与实际请求可能不一致，也无法在任务重试时复现 |
| 批量视频的任务粒度 | 一个任务多条分镜 | 每条分镜一个任务：占满并发名额，见核心设计 5.3 |
| 不支持参考图的模型 | 明确失败 | 退化为无参考生成：结果人物不一致且用户不知情 |
| 拼接并发 | 全局串行 | 不限制（参考项目）：重编码会占满处理器，影响其他用户 |
| 首帧 / 尾帧图 | 不做 | 参考项目后端保留了能力但工作台不用（调研 A.1），按实际流程取舍 |

## 11. 规范与文档

- `.trellis/spec/backend/drama.md` 增补媒体制作部分；媒体存储规范增补 FFmpeg 封装；框架规范增补 `upload_limits`。
- README：部署章节说明镜像已内置 FFmpeg、本机开发需自行安装；短剧功能说明补全到成片。
