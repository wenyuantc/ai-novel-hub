# 设计：项目封面（上传 / AI 生成 / 备份 / EPUB）

## 1. 数据模型

新表 `project_covers`（迁移 `0021_project_covers`）：

| 列 | 类型 | 说明 |
|---|---|---|
| project_id | int PK, FK projects.id ON DELETE CASCADE | 一个项目至多一张封面 |
| mime_type | String(32) | image/jpeg / image/png / image/webp |
| data | LargeBinary | 图片原始字节（≤ 10MB） |
| created_at / updated_at | TimestampMixin | updated_at 兼作封面版本号 |

选独立表而非 `projects` 加列：列表、概览等所有 `select(Project)` 不会顺带读出图片字节；删除项目由外键级联清理。图片存数据库而非磁盘：不用新增 Docker 数据卷，备份 / 迁移随数据库走。

`ProjectOut` 新增 `cover_updated_at: datetime | None`（`list_projects` / `project_out` 经 LEFT JOIN 只取 `project_covers.updated_at`）。前端用 `/api/v1/projects/{id}/cover?v=<cover_updated_at 时间戳>` 作为图片地址，换封面后 URL 变化，旧缓存自然失效。

实现细化：版本参数保留完整服务端时间戳字符串（含微秒），不转为 JavaScript `Date`，避免精度降到毫秒后连续替换仍复用 URL。

## 2. 后端接口（`backend/app/api/v1/project_cover.py`，前缀 `/projects/{project_id}/cover`）

均先 `get_owned_project`，他人 / 不存在 → 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/cover` | 返回图片字节，`Content-Type`=mime，`Cache-Control: private, max-age=31536000, immutable`（URL 带版本参数）；无封面 404 |
| PUT | `/cover` | multipart `file`；读取上限 +1 字节判超限；按魔数识别 JPG/PNG/WebP（不信扩展名与客户端 Content-Type）；upsert；返回 `ProjectOut` |
| DELETE | `/cover` | 删除封面；返回 `ProjectOut`（无封面时也成功，幂等） |
| GET | `/cover/prompt` | 返回 `{prompt}`：按项目信息拼好的默认提示词 |
| POST | `/cover/generate` | body `{prompt: 1..2000 字, config_id?: int}`；用图片模型生成，返回 `{image_base64, mime_type}`，**不落库** |

`UploadLimitMiddleware` 增加规则：`PUT /api/v1/projects/\d+/cover`，上限 10MB + `MULTIPART_OVERHEAD`。

确认流程：前端把预览的 base64 转为 Blob，走同一个 `PUT /cover` 上传。写入只有一条路径，校验只做一处。

## 3. 默认提示词（`backend/app/ai/prompts/cover.py`）

纯 Python 拼接（不进提示词模板注册表，因为用户每次都能在弹窗里直接修改）。素材：书名、题材、类型标签、一句话设定（为空时用 synopsis 前 300 字）、项目默认写作风格的 `name` + `description` + `tone`。固定要求：竖版 2:3 书籍封面、突出主题氛围、**画面中不要出现任何文字**（避免模型生成乱码汉字）。空字段跳过。

## 4. 图片生成能力（Provider 层）

`LLMProvider` 新增：

```python
@dataclass
class GeneratedImage:
    data: bytes
    mime_type: str

async def generate_image(self, prompt: str, model: str) -> GeneratedImage:
    raise AIProviderError("该接口类型不支持图片生成，请在「模型配置」中换用 OpenAI 兼容或 Gemini 的图片模型")
```

- **OpenAICompatProvider**：`client.images.generate(model, prompt, n=1, size="1024x1536")`。若返回 400 且错误提到 size，去掉 size 重试一次（dall-e-3 / 各中转支持的尺寸不一）。不传 `response_format`（gpt-image 系列会拒绝）；结果优先取 `b64_json`，否则取 `url` 下载：禁止私网时（`follow_redirects=False`）先对 URL 做 `ensure_endpoint_allowed`，用 httpx 下载，限 10MB。
- **GeminiProvider**：模型名含 `imagen` → `client.aio.models.generate_images(..., config=GenerateImagesConfig(number_of_images=1, aspect_ratio="3:4"))`；否则 `generate_content(..., config=GenerateContentConfig(response_modalities=["IMAGE"]))`，取第一个 `inline_data` 图片。
  实施时核实 `google-genai 2.32.0`：当前 API-key Developer API 的 `generate_images` 仅支持 Vertex/Enterprise，保留兼容分支并转换为明确中文提示；不为此扩展 Vertex 鉴权。Gemini 原生 IMAGE 路径通过真实 SDK + 模拟 HTTP 验证。
- **AnthropicProvider**：沿用默认实现（报不支持）。
- 所有 SDK 异常经 `describe_error` 转为 `AIProviderError`。

服务层（`backend/app/services/cover_service.py`）：`resolve_config(model_type="image")` → `build_provider` → `generate_image`；结果再过一遍魔数校验与 10MB 上限（防止接口返回非图片）；`AIProviderError` → 400 中文提示。超时沿用 `settings.ai_request_timeout`。

共享图片校验实现于 `backend/app/ai/images.py`，供 Provider、服务与备份解析共同使用，避免 Provider 反向依赖服务层。

测试替身：`tests/helpers.py` 的 `FakeLLM` 增加 `images` 队列与 `image_calls` 记录。

## 5. 项目备份

- `project_covers` 写入 `NOT_EXPORTED`，理由：「二进制不走通用表导出，作为备份顶层 cover 字段（base64）单独导出导入」。满足 `test_registry_covers_all_project_tables`。
- 导出（`exporter.stream_export`）：head 中增加 `"cover": {"mime_type": ..., "data": <base64>}`，无封面时为 `null`。`estimate` 把封面字节数 ×4/3 计入。
- 导入（`importer.parse_backup`）：`cover` 可缺省 / null；存在时校验 mime 在白名单、base64 可解码、魔数与 mime 一致、≤ 10MB，不通过计入 errors。导入执行时在创建项目之后插入 `project_covers`。
- **备份 VERSION 不变（仍为 1）**：新增的是可选字段，旧备份没有 `cover` 照常导入；旧版本程序读到新备份会忽略未知顶层键（需在实现时确认 `parse_backup` 对未知顶层键不报错，现有代码只检查 `tables` 的未知键）。

## 6. EPUB

`build_epub(..., cover: tuple[bytes, str] | None = None)`：
- `OEBPS/cover.<jpg|png|webp>`，manifest 项 `properties="cover-image"`；opf `<meta name="cover" content="cover-image"/>` 兼容 EPUB 2。
- `OEBPS/cover.xhtml`（整页图片）放在 spine 首位、扉页之前。
- `export_service` 调用处读取封面后传入。无封面时输出与现在逐字节一致（除时间戳），已有 EPUB 测试不受影响。

## 7. 前端

- `api/types.ts`：`Project.cover_updated_at: string | null`；`api/projects.ts`：`coverUrl(p)`、`uploadCover(id, blob)`（FormData 单独 fetch，参照 `exportProject`）、`deleteCover`、`coverPrompt`、`generateCover`。
- `components/project/CoverPanel.tsx`（新）：2:3 封面（复用 `BookCover`，传 `coverUrl`），下方按钮「上传」「AI 生成」「删除」。上传用 antd `Upload` 的 `beforeUpload` 拦截并自行调用接口，前端先校验类型与 10MB。
- `components/project/CoverGenerateModal.tsx`（新）：打开时拉默认提示词填入 TextArea；图片模型选择（`AIConfigSelect` 增加 `modelType` 参数，默认 `'text'` 保持现有行为）；「生成」→ loading → 预览图；「重新生成」；「设为封面」→ base64 转 Blob → `uploadCover` → 更新 store；「取消」不改动。无图片模型时显示去配置的提示。
- `OverviewPanel.tsx`：「基本信息」卡片内左侧放 `CoverPanel`（固定宽约 180px），右侧为现有表单；窄屏改为上下排列（沿用 `splitRow` / `splitPane`）。
  封面更新只合并当前项目 store 的 `cover_updated_at`，保留基本信息表单的 `project.updated_at` key，避免丢失未保存编辑。
- `ProjectListPage.tsx`：`BookCover` 传入 `coverUrl={coverUrl(p)}`。

## 8. 兼容与回滚

- 迁移只新增表，downgrade 删表，现有数据不变；`test_migrations.py` 覆盖升降级。
- 备份格式向后兼容（可选字段）。
- 回滚：撤销代码并执行 downgrade 即可，封面数据随表删除。

## 9. 取舍

- 生成为同步 HTTP 请求（通常 10~60 秒），没有接入任务队列：交互上用户本来就在弹窗里等预览，队列化收益小、复杂度高。
- 大小上限定为 10MB（之前讨论时说的是 5MB）：AI 生成的竖版 PNG 常有 2~4MB，留出余量，避免确认封面时被自己的上限拒绝。
