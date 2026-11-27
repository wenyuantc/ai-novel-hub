# 图片参考图与视频模型接入：实施计划

> 需求 `prd.md`（G1–G10），设计 `design.md`。依赖 `11-27-media-storage` 已完成。
> 按顺序执行；每一步先写测试再写实现。中途只跑受影响测试加哨兵，全量门禁留到最后一步。

## 开始前复核

- [ ] 媒体存储的实际接口：`app/media/service.py` 的 `store`、`signing.external_url` 的签名是否与本设计第 4、5 节的用法一致，有出入先改设计。
- [ ] 迁移链尾与本子任务的迁移编号。
- [ ] 读 `.trellis/spec/backend/ai-usage.md`、`batch-generate.md`、`admin-task-monitoring.md`，确认用量记录的幂等键、预算预检口径、中断与重试语义。
- [ ] 读 `backend/app/services/ai_dispatch.py` 中图片调用的完整路径，确认 `options` 需要透传的全部层。
- [ ] 四家厂商的当前接口文档（提交、查询、取消、参考图传法、时长与分辨率取值），与设计第 4.2 节的表逐项核对并更新。

## 步骤

### 1. 图片选项与参考图

- [ ] `base.py` 增加 `ImageInput`、`ImageOptions`；`generate_image` 增加仅关键字参数 `options`。
- [ ] OpenAI 兼容与 Gemini 的实现；不支持参考图时的明确报错。
- [ ] 三层封装透传；`app/ai/references.py::normalize_images`。
- [ ] 测试 `tests/test_image_options.py`：`options=None` 时发出的请求与改动前逐字段相同（封面、缩略图不受影响）；
      各画幅的尺寸映射；有参考图时走编辑接口；Imagen 模型带参考图报错；归一化的缩放、去重与张数上限；用量仍记一张。

验证：`cd backend && uv run pytest tests/test_image_options.py tests/test_project_cover.py -q`

### 2. 视频类别与配置

- [ ] `ModelType`、`ProviderName` 扩展与保存校验；前端类型、模型配置页的类别与厂商选项。
- [ ] 测试：新厂商配成文本类别被拒；视频配置的增改查；协作者可见性规则与现有一致；前端表单在视频类别下隐藏不适用字段。

### 3. 台账表与迁移

- [ ] `app/models/ai_external_jobs.py`；迁移（新表 + `ai_usage_calls.video_seconds` + 单价表 `video_price_nano`）；含降级。
- [ ] 作品备份注册表登记 `ai_external_jobs` 为不导出；整库备份表目录更新；迁移往返测试。

### 4. 适配器协议与一个假厂商

- [ ] `VideoProvider`、`VideoRequest`、`VideoCapabilities`、`VideoPoll`；适配器注册表。
- [ ] 测试用假厂商（进程内，状态可编排：若干次运行中后成功、失败、超时、查询偶发错误）。

### 5. 驱动器

- [ ] `app/ai/external_jobs.py::drive` 与取消逻辑；下载地址校验与体积上限；结果存入媒体存储；成功时记用量。
- [ ] 测试 `tests/test_external_jobs.py`（用假厂商）：正常完成；提交后即持久化任务号；从 `submitted` 重新进入只查询不提交；
      查询偶发失败不致任务失败；超过截止时间置 `expired`；厂商失败的原因翻译；取消后不下载；成功只计一次用量（重入不重复计）；
      下载地址指向内网被拒；`parallel` 限制同时等待数。

### 6. 四家厂商适配器

每家一个文件，各自一组用录制样例驱动的测试（请求体字段、任务号解析、三种终态解析、能力上限校验）：

- [ ] 火山引擎 Seedance
- [ ] OpenAI 兼容视频接口
- [ ] MiniMax
- [ ] 阿里云百炼 Wan

验证：`uv run pytest tests/test_video_adapters_*.py -q`

### 7. 测试生成任务

- [ ] `ai.video_probe` 处理器与受理接口；模型配置页的「测试生成」与结果播放；探针结果 24 小时回收。
- [ ] 测试：受理、进度、成功后可读取结果、取消、预算不足被拒、他人不可读取结果。

### 8. 重启续等（风险最高，先读后写）

- [ ] **先读代码**：`backend/app/services/task_engine/engine.py` 中接管租约过期任务并标记中断的段落，以及批量任务的重试实现。
      评估 `resume_after_interrupt` 的可行性，把结论补进 `design.md` 5.3。
- [ ] 保底：`validate_retry` 沿用原台账行；测试「中断后重试，厂商侧只有一个任务」。
- [ ] 目标（评估可行才做）：引擎对声明了该标记的处理器重新排队；测试「杀掉执行进程后任务自动继续并成功」及与取消、插件停用的交互。
- [ ] 若目标不可行：停下来向用户说明，并征得同意后把 `prd.md` 的对应验收改为「一键继续且不重复提交」。

回滚点：引擎改动单独成一个提交，可独立回退而保留保底方案。

### 9. 用量、预算与前端展示

- [ ] 受理时的视频预算预检；用量页与模型单价页的视频维度。
- [ ] 测试：超预算被拒；未设单价只记数量；协作者按授权；用量页出现视频调用。

### 10. 真实密钥验收、文档与收口

- [ ] **需要用户提供四家厂商的测试密钥与少量额度。** 逐家用「测试生成」跑通文本加参考图到视频，记录到 `research/verification.md`
      （不记录密钥；注明模型、耗时、是否支持取消）。拿不到某家密钥时，该家标注为「仅样例测试通过，未做真实验收」并告知用户。
- [ ] 规范与 README（见 `design.md` 第 10 节）。
- [ ] 全量门禁：
  - `cd backend && uv run ruff check . && uv run pytest -n 4 --dist loadfile --durations=20`
  - `cd frontend && npm run lint && npm run test && npm run build`

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 厂商接口与参考项目代码不一致或已变更 | 开始前对照当前文档核实；适配器测试用录制样例，真实验收放在最后 |
| 引擎恢复路径改动破坏既有任务语义 | 第 8 步先读后写；保底方案不依赖引擎改动；引擎改动单独提交 |
| 提交成功但写库前崩溃导致重复提交 | 提交与写库之间不做其他操作；规范写明这一残余窗口 |
| 重复计费 | 台账的 `usage_call_id` 作为幂等标记；测试覆盖重入 |
| 结果下载地址被用来探测内网 | 下载前单独校验公网地址、禁重定向、限体积 |
| 拿不到真实密钥 | 第 10 步明确降级方式并如实标注，不把样例测试说成真实验收 |

## 开始实现前的检查

- [ ] 用户已批准本子任务的规划；`11-27-media-storage` 已完成并归档。
- [ ] `implement.jsonl` 与 `check.jsonl` 各有至少一条真实的规范条目。
- [ ] 工作区干净，或已与用户确认现有改动的归属。
