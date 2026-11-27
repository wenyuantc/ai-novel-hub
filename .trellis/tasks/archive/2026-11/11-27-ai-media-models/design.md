# 图片参考图与视频模型接入：技术设计

> 需求见同目录 `prd.md`（G1–G10）；决定编号见父任务 `prd.md`（D14、D16）。调研依据：
> `11-27-short-drama-plugin/research/reference-media-infra-and-gap.md` A.1–A.3、A.6、B.8、B.9。

## 1. 设计原则

1. **外部任务先记账再等待。** 向厂商提交成功后，立刻把厂商任务号写进数据库并提交，然后才开始轮询。
   之后无论进程重启、任务重试还是换实例，都凭这条记录续等同一个外部任务，不再提交第二次（G4）。
2. **沿用现有的三层调用链。** 图片与视频调用同样经过调度、用量记录、协作授权（`backend/app/services/ai_dispatch.py`、
   `ai_usage_recording.py`、`collaboration_ai_guard.py`），不另开旁路（G7）。
3. **核心提供能力，不提供业务任务。** 本子任务交付适配器、外部任务台账和一个可复用的驱动器；
   「为哪条分镜生成」这类任务类型由使用方（短剧插件）定义。唯一的例外是模型配置页的测试生成。
4. **不照搬参考项目的缺陷。** 参考项目重启即判失败、没有取消、没有并发控制（调研 A.2、A.3），这里都要补上。

## 2. 结构

```
backend/app/ai/providers/
  base.py            # ImageInput、ImageOptions 与参考图能力
  openai_compat.py   # 生图支持尺寸与参考图
  gemini.py          # 生图支持画幅与参考图
  video/
    __init__.py      # 视频适配器注册表
    base.py          # VideoProvider、VideoRequest、VideoPoll 及受控 HTTP
    volcengine.py    # 火山引擎 Seedance
    openai_video.py  # OpenAI 兼容视频接口
    minimax.py
    aliyun_wan.py
backend/app/ai/references.py     # 参考素材归一化（缩放、编码、数量上限）
backend/app/ai/external_jobs.py  # 台账读写 + 驱动器（提交、轮询、取回、取消）
backend/app/models/ai_external_jobs.py
backend/app/services/task_engine/handlers/video_probe.py   # ai.video_probe
backend/alembic/versions/0082_ai_external_jobs.py          # 编号以实施时的链尾为准
```

## 3. 图片：尺寸与参考图（G1）

```python
@dataclass(frozen=True)
class ImageInput:            # 一张参考图
    data: bytes
    mime_type: str

@dataclass(frozen=True)
class ImageOptions:
    aspect_ratio: str | None = None      # "16:9" / "9:16" / "1:1" / "3:4" / "4:3"
    references: tuple[ImageInput, ...] = ()

async def generate_image(self, prompt: str, model: str, *, options: ImageOptions | None = None) -> GeneratedImage
```

- `options=None` 时行为与现在完全一致（封面、设定缩略图三处调用方不改，`backend/app/ai/providers/base.py:68`）。
- OpenAI 兼容：无参考图走 `images.generate`，按画幅映射到该模型支持的尺寸；有参考图走 `images.edit` 的多图上传。
  仅调用方未显式指定画幅时保留旧的「尺寸被拒则去掉尺寸重试一次」兜底（`openai_compat.py:173`）。
- Gemini：Imagen 模型传 `aspect_ratio`，不支持参考图；原生图片模型把参考图作为 `inline_data` 放进 `contents`（`gemini.py:247`）。
- 模型不支持参考图却传入时，抛 `AIProviderError("该模型不支持参考图…")`，不静默丢弃（G1）。
- 外层三处封装（`ai_dispatch.py:715`、`ai_usage_recording.py:255`、`collaboration_ai_guard.py:202`）同步增加 `options` 透传。
- `references.py::normalize_images(inputs, *, max_count, max_edge)`：去重、缩到最长边不超过 1536、统一转 JPEG、限制张数。
  参考项目压到 768 像素（调研 A.1），这里取更高上限以保住人脸细节，具体值做成常量。

## 4. 视频模型（G2）

### 4.1 配置

- `ModelType` 增加 `video`（`backend/app/schemas/ai_config.py:8`）。`ai_configs.model_type` 是字符串列，不需要改表。
- `ProviderName` 增加 `volcengine`、`minimax`、`aliyun_wan`；OpenAI 兼容视频沿用 `openai_compat`。
  三个新厂商只允许 `model_type=video`，保存时校验（写法同 `check_embedding_provider`）。Anthropic、Gemini 不提供视频。
- 密钥归属不变：每个作者自己的 `ai_configs` 行（D16）。协作者使用作者模型的规则沿用现有授权。
- 前端「模型配置」页增加视频类别与厂商选项；不适用的字段（思考强度、上下文大小）在视频类别下隐藏。

### 4.2 适配器协议

```python
@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    model: str
    duration_seconds: int
    aspect_ratio: str            # 16:9 / 9:16 / 1:1
    resolution: str              # 480p / 720p / 1080p
    images: tuple[ImageInput, ...] = ()
    generate_audio: bool = True

@dataclass(frozen=True)
class VideoCapabilities:
    max_images: int
    durations: tuple[int, int]        # 最小、最大秒数
    resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    can_cancel: bool

class VideoProvider(Protocol):
    def capabilities(self, model: str) -> VideoCapabilities
    async def submit(self, request: VideoRequest) -> str                 # 返回厂商任务号
    async def poll(self, external_id: str) -> VideoPoll                  # state: running / succeeded / failed；可带进度、结果地址、失败原因
    async def download(self, poll: VideoPoll) -> AsyncIterator[bytes]    # 流式取回结果
    async def cancel(self, external_id: str) -> None                     # 不支持时为空操作
```

四家的公开接口已按厂商文档核实。详细型号和限制见[厂商协议记录](research/providers.md)。

| 厂商 | 提交 | 查询 | 参考图上限 | 时长 |
| --- | --- | --- | --- | --- |
| 火山引擎 Seedance | `POST /api/v3/contents/generations/tasks` | `GET .../tasks/{id}` | 2.0 为 9；2.5 按型号限制，核心单次最多 16 张 | 2.0 为 4–15 秒，2.5 最长 30 秒 |
| OpenAI 兼容 | multipart `POST /v1/videos` | `GET /v1/videos/{id}`，结果经 `/content` 带鉴权下载 | 1（首帧） | 由模型决定 |
| MiniMax | `POST /v2/video_generation` | `GET /v2/query/video_generation/{id}` | 以厂商为准 | 4–15 秒 |
| 阿里云百炼 Wan | `POST .../video-synthesis`（异步头） | `GET /api/v1/tasks/{id}` | 10 | 2–30 秒 |

- OpenAI 官方 Videos API 已于 2026-09-24 关闭。此协议用于仍提供相应路由的兼容服务，验收须配置对应服务的地址和密钥。[官方停用记录](https://developers.openai.com/api/docs/deprecations)
- 具体字段与上限在实现各适配器时对照厂商当前文档核实，不以参考项目代码为准；差异记入规范。
- 请求超出该模型能力（参考图过多、时长越界）时在提交前拒绝并说明，不交给厂商报错。
- 参考图通过归一化图片字节发送（内联或 multipart），超过媒体图片大小上限时拒绝。参考视频、音频使用 `signing.external_url` 签发的外链，无法签发时给出明确错误（G6）。
- 网络防护：适配器的 HTTP 客户端经现有的受控钩子创建（`backend/app/ai/providers/dispatch_hooks.py`），
  用户自填的接口地址沿用内网地址防护（G8）。结果下载地址由厂商返回、可能是第三方域名：下载前单独做一次公网地址校验，
  禁止重定向到内网，限制最大体积（默认 1 GiB）。

## 5. 外部任务台账（G3、G4、G5）

### 5.1 表 `ai_external_jobs`

| 列 | 说明 |
| --- | --- |
| `id` | `String(36)` UUID 主键 |
| `actor_id`、`payer_id`、`project_id` | 发起人、付费人、所属作品；语义与 `ai_usage_calls` 一致（`backend/app/models/ai_usage.py:157`） |
| `config_instance_key`、`provider`、`model` | 受理时锁定的模型配置 |
| `kind` | `video`（图片仍是同步调用，不进台账） |
| `request` | JSON：提示词、时长、画幅、分辨率、参考图的媒体对象 id 列表（不存图片字节） |
| `external_id` | 厂商任务号；为空表示尚未提交 |
| `state` | `created` → `submitting` → `submitted` → `succeeded` / `failed` / `cancelled` / `expired` |
| `progress` | 0–100，可空 |
| `result_object_id` | 结果的媒体对象 id |
| `error_code`、`error_message` | 失败原因，文案面向用户 |
| `usage_call_id` | 已记入用量的调用 id；非空即已计费，保证只计一次 |
| `submitted_at`、`finished_at`、`deadline_at`、`created_at` | |
| `owner_ref` | 使用方的引用标识（如 `drama.shot_video:123`），仅用于排查 |

### 5.2 驱动器

```python
async def drive(ctx: TaskContext, job_ids: Sequence[str], *, execution, emit, parallel: int = 3) -> list[str]
```

对每个台账行按状态推进，幂等：

1. `created`：构建请求 → 持久化 `submitting` → `submit` → **在独立短事务里写入 `external_id`、`state=submitted`、`deadline_at` 并提交** → 进入轮询。
   发请求前先持久化 `submitting`。如果进程在收到并保存厂商任务号之前退出，恢复时将此单标为提交结果未知，提示核查厂商记录。只有保存了任务号才自动续等，以免重复付费。
2. `submitted`：每 10 秒 `poll`（带抖动，连续失败退避到 30 秒）；单次查询失败不算任务失败；超过 `deadline_at` 置 `expired`。
3. 厂商成功：流式 `download` → 存入媒体存储（归属与作用域由受理时保存的作品和发起人确定）→ 同一事务内置 `succeeded`、写 `result_object_id`、记用量。
4. 厂商失败：置 `failed`，把厂商原因翻译为可读文案（内容审核、额度不足、参数不合法、其他）。
5. 取消：对 `submitted` 行尽力调用 `cancel`，置 `cancelled`，不下载结果。

`parallel` 限制同一任务内同时等待的外部任务数。`emit` 把阶段与进度发到任务的实时输出。

### 5.3 与任务引擎的衔接

一个后台任务可以驱动多个台账行（短剧的批量生视频即如此），只占一个并发名额，避免为每条视频各占一个名额导致排队
（现有名额默认全局 4、每用户 2，按流式文本设计，调研 B.9）。

重启恢复由处理器显式声明 `resume_after_interrupt = True`。正常关停时引擎停止本地等待并保留
运行行。进程崩溃时该行也保留。新执行者须先走现有租约围栏并接管任务，确认旧执行者已经停止
或失联。重新排队使用一次条件更新，核对任务 UUID、原入队时间、执行键、接管者和两类取消意图，
并在同一事务校验接管者租约、登记统计状态。保留任务输入、断点及外部台账引用，普通调度重新校验
权限、插件开关和并发名额，使用新执行键领取原任务。恢复请求与取消相遇时，已受理的取消优先走
原有清理流程。其余处理器继续使用各自的中断与收据恢复规则。

视频探针声明 `abort_status_aware = True`。引擎把当前执行体的停止原因传给 `on_abort`，
使探针仅在取消时通知厂商。排队、管理员和接管后的独立清理入口依据持久取消意图判断。
处理器的 `stage` 事件可携带 0 至 99 的整数 `progress`，100 由成功收尾统一写入。

## 6. 用量与费用（G7）

- `ai_usage_calls` 增加 `video_seconds`；模型单价表增加 `video_price_nano`（每秒），写法同现有的 `image_price_nano`（`backend/app/models/ai_usage.py:77`）。
- 视频在成功取回时记一次用量：数量为实际生成秒数；未设单价则只记数量、费用为空。失败与取消不计。
- 预算在受理时按「预计秒数 × 单价」做一次预检，与现有批量预估的口径一致；实际扣减以成功为准。
- 带参考图的图片调用仍按 `image_count` 计。
- 前端用量页与模型单价页增加视频维度的展示与输入。

## 7. 测试生成（G9）

任务类型 `ai.video_probe`（核心类型，登记在核心处理器目录）：模型配置页对某个视频配置点「测试生成」，
用固定的简短提示词生成一段最短时长的视频，结果以 `scope=user` 存入媒体存储并在页面内播放；探针结果保留 24 小时后由清理任务回收。
它同时是本子任务独立验收四家厂商的入口（此时短剧插件尚未实现）。

## 8. 兼容

- 现有文本、图片、向量调用路径与测试不变；`generate_image` 的新参数是仅关键字且可省略。
- 迁移 0082 增加视频任务表、视频用量及定价快照列，并扩展模型类别、提供方与媒体类别约束。降级前检查旧版本能否表示已有数据。
- `TASK_KINDS` 增加 `ai.video_probe`。

## 9. 权衡记录

| 问题 | 选择 | 放弃的方案与原因 |
| --- | --- | --- |
| 等待外部任务的方式 | 任务内轮询 + 台账续等 | 厂商回调：需要公网入口，自部署环境多数没有 |
| 并发模型 | 一个任务驱动多个外部任务 | 每个视频一个任务：直接占满按文本设计的并发名额；另开一套名额要改引擎调度 |
| 台账放哪 | 核心独立表 | 放在各使用方的业务表里：每个使用方都要重写幂等与计费逻辑 |
| 重启恢复 | 保底 + 目标两层 | 只做保底：达不到「自动继续」；只做目标：引擎改动若不可行则整项落空 |
| 视频适配器是否并入现有 Provider 类 | 独立协议与注册表 | 并入：文本 Provider 的构造、流式与重试语义与视频完全不同，硬塞会互相污染 |

## 10. 规范与文档

- 新增 `.trellis/spec/backend/ai-media-models.md`（图片选项、视频协议、台账状态机、驱动器、计费、错误矩阵、必需测试）。
- 更新 `ai-usage.md`（视频计量）、`quality-guidelines.md`（下载地址的防护场景）、前端 `component-guidelines.md`（模型配置页）。
- README：模型配置章节增加视频类别与四家厂商的注意事项（G10）。
