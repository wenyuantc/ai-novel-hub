# 图片参考图与视频生成

## 使用范围

作品资产生图使用 `app.services.generated_media.generate_stored_image`。模型配置页通过后台任务 `ai.video_probe` 生成测试视频。插件可复用 `app.ai.external_jobs.create_job` 和 `drive`，在自己的任务类型内关联素材、展示结果。

## 接口和归属

- 生图协议为 `generate_image(prompt, model, *, options=None)`。显式画幅或参考图封装在 `ImageOptions`。封面和缩略图省略选项时须维持原来的请求。核心存储入口按作品权限解析模型，记录发起人和付费人，结果归作品。
- 视频配置归用户，`model_type="video"`。厂商为 `volcengine`、`openai_compat`、`minimax` 或 `aliyun_wan`。OpenAI 兼容和 Wan 3.0 需显式填写接口地址。作品内调用须绑定当前协作授权和原配置实例。
- `POST /api/v1/ai-configs/{id}/video-probe/reference` 接受 multipart 的 `file`，返回 201 和 `object_id`。`POST /api/v1/ai-configs/{id}/video-probe` 接受 `{"image_ids":[...]}`，返回 202 和任务。`GET` 同路径读取本人最近一次测试。成功任务的 `video_object_id` 交给鉴权媒体读取接口。探针参考图和结果按 24 小时清理。
- 后台请求保存素材 UUID。提交前按发起人和作品重新校验归属，图片可内联，视频和音频从媒体服务取得限时外链。PCM WAV 可作参考音频。密钥和包含签名的 URL 均不写入外部任务台账。
- `0082` 新增 `ai_external_jobs`。作品备份排除该表，整库备份保留任务与其厂商编号。运行中任务恢复还需原媒体卷或存储桶。

## 状态和恢复

1. 受理任务时同时写入台账。真正发送前从 `created` 改为 `submitting` 并提交事务。
2. 收到厂商任务号立即写入 `external_id` 和 `submitted`。只有取得此收据才可自动轮询同一任务。进程丢失收据时提示用户核对厂商记录，自动重发会产生第二笔费用。
3. 处理器显式声明 `resume_after_interrupt = True`。接管者保留原任务轮次，重新领取执行身份并核对取消意图。等待上限由 `VIDEO_JOB_TIMEOUT_SECONDS` 控制，默认 1800 秒。
4. 成功收尾关联媒体对象、台账和用量。优先使用厂商返回的输出秒数，缺失时从 MP4 电影头读取实际时长，两者都未知时保留计量缺口。一条台账以自己的 UUID 对应一条调用。取消和失败结束本地等待，远端取消取决于厂商协议及排队状态。

## 输入和错误

| 输入或事件 | 行为 |
| --- | --- |
| 模型无法处理参考图或指定画幅 | 显示能力限制，禁止静默丢弃用户选项 |
| 私有素材属于另一用户，或作品素材属于另一作品 | 拒绝读取或发送 |
| 参考音视频需要外链，但本地没有配置公网地址 | 提示配置公网地址或 S3 |
| 账号或适用作品的预计费用超出预算 | 生成前拒绝；预算判断使用已结算的开销，不预占其他等待任务的费用 |
| 供应商限流、查询网络短暂中断 | 在截止时间内继续查询原任务 |
| 内容审核、鉴权或参数错误 | 显示可读原因，错误消息不包含密钥或原始厂商响应 |
| 第三方结果是内网地址或重定向 | 拒绝下载；只向配置的兼容接口内容路径发送该服务的密钥 |

## 示例与易错点

常规情况是受理时保存一行台账，厂商成功后保存一份视频，写入一次秒数。换实例后从 `submitted` 继续查询。若在发送后、持久化厂商任务号前丢失进程，继续生成可能重复扣费，须显示提交结果未知。若下载链接指向第三方 CDN，只附带该 URL 自身的签名，不复制模型请求的 Authorization 头。

## 验证

- `test_image_options.py` 覆盖图像选项、编辑输入与媒体写入；封面、缩略图回归覆盖旧调用。
- `test_video_adapters_contract.py` 检查四种请求结构、状态映射与边界；样例不替代有额度的真实厂商账号验收。
- `test_external_jobs.py`、`test_ai_video_probe.py` 检查提交、费用幂等、取消、超时和私有结果。硬杀接管由 `test_task_engine_auto_resume.py` 验证。
- `test_ai_external_jobs_migration.py` 检查升级、降级保护、旧数据与备份。原音频/视频文件仍依赖媒体存储备份。
