# 视频协议核对记录（2026-11-27）

下表记录适配器实际发送的协议。测试使用官方文档样例的状态和字段构造 HTTP 响应，没有使用真实密钥。

| 接口 | 提交和轮询 | 本期可传的参考素材 | 取消 |
| --- | --- | --- | --- |
| 火山方舟 Seedance 2.x | `POST /api/v3/contents/generations/tasks` 返回 `id`，对同路径下的任务号执行 GET，成功时读取 `content.video_url` | 通过 `content` 指定有角色的图片、视频和音频。2.0 时长 4–15 秒，2.5 可到 30 秒 | 排队时可 DELETE，运行后由本地停止等待。[创建任务](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh) · [任务管理](https://docs.volcengine.com/docs/ark/video-generation-tutorial?lang=zh) |
| OpenAI 兼容 | 对第三方地址的 `/v1/videos` 提交 multipart，GET 查询任务号，成功后从 `videos/{id}/content` 带密钥下载 | 参考图按比例缩放并补边至输出尺寸，以 `input_reference` 上传；支持横屏和竖屏。时长 4、8、12、16、20 秒，具体以服务商实现为准 | 本地停止等待。OpenAI 官方已于 2026-09-24 关闭 Videos API，因此必须配置仍提供该协议的服务地址。[协议参数](https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide) · [停用记录](https://developers.openai.com/api/docs/deprecations) |
| MiniMax H3 | `POST /v2/video_generation` 返回 `task_id`，GET `/v2/query/video_generation/{task_id}`，读取 `task.content.url` | H3 采用 V2 `content` 数组，支持参考图、视频和音频。各 H3 版本支持的时长及 480P、768P、2K 档位不同 | 查询到排队状态时 DELETE `/v2/video_generation/{task_id}`。厂商在运行状态拒绝取消，在终态将该请求解释为删除任务记录。[创建](https://platform.minimax.io/docs/api-reference/video-generation-v2-create) · [查询](https://platform.minimax.io/docs/api-reference/video-generation-v2-query) · [取消](https://platform.minimax.io/docs/api-reference/video-generation-v2-delete) |
| 阿里云百炼 Wan 3.0 | `POST /api/v1/services/aigc/video-generation/video-synthesis` 携带 `X-DashScope-Async: enable`，从 `output.task_id` 取任务号。GET `/api/v1/tasks/{id}`，成功读取 `output.video_url` | `input.media` 支持参考图最多 10 张、视频和音频各 5 段。仅生成视频时为 2–30 秒。有输入视频时，厂商另限制输入和输出总时长。模型、密钥和工作空间端点须同地域，须填写含 WorkspaceId 的 API 根地址 | 对 `tasks/{id}/cancel` 执行 POST，仅排队任务可取消。[Wan 3.0 协议](https://help.aliyun.com/en/model-studio/wan3-video-generation-api-reference) · [异步任务取消](https://www.alibabacloud.com/help/en/model-studio/manage-asynchronous-tasks) |

各接口提交均无自动重试。厂商响应明确失败时按审核、鉴权、额度、参数等类别显示中文原因。仅限流、暂时的服务不可用和传输中断可按查询策略重试。

普通厂商的结果使用临时 CDN 链接下载，逐次检查公网地址，不携带模型密钥。兼容协议的鉴权下载路径由已配置的接口根地址和经过格式校验的任务号组成。所有请求禁用重定向。结果流的累计读取上限为 1 GiB，媒体存储可以施加更小的限制。

探针使用各模型最短时长。MiniMax 核对的是 V2 H3 系列，Wan 核对的是 3.0 系列，各自较早一代使用不同请求体。签名参考素材由媒体存储和任务驱动准备；适配器对图片内联，对视频和音频传 URL。用量采用成功结果的输出时长，Wan 的 `usage.output_video_duration` 可排除作为输入的参考视频时长。

归一化后的参考图片仍要满足厂商的尺寸要求。Seedance 每边为 300–6000 像素，MiniMax 为 256–5760 像素，两者长边最多是短边的 2.5 倍。Wan 3.0 每边为 240–8000 像素，长边最多是短边的 8 倍。超出这些范围时，适配器在提交前给出错误。Seedance 2.0 的参考音频须配合参考图或视频，2.5 可单独参考音频。[方舟创建任务](https://console.volcengine.com/ark/region%3Acn-beijing/docs/create-video-generation-task-api) · [MiniMax H3](https://platform.minimax.io/docs/api-reference/video-generation-v2-create) · [Wan 3.0](https://help.aliyun.com/en/model-studio/wan3-video-generation-api-reference)

Seedance、MiniMax 的取消与删除共用接口。客户端在发 DELETE 前先查询排队状态，若厂商在查询和删除之间完成任务，厂商可能删除刚完成的任务记录。
