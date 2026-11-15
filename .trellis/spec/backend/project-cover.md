# 项目封面：上传、生成预览与导出

## 1. 范围与触发

封面涉及 `ProjectCover`、图片 Provider、项目 API、概览/书架 UI、备份与 EPUB。
修改任一入口时同步检查这些消费方；图片校验集中在 `app/ai/images.py`，不在各接口重复实现。

## 2. 签名

- 迁移 `0021_project_covers`：`project_covers(project_id PK/FK projects.id ON DELETE CASCADE, mime_type, data, created_at, updated_at)`。
- `ProjectOut.cover_updated_at: datetime | None`；列表只查询更新时间，不能顺带加载二进制图片。
- `LLMProvider.generate_image(prompt: str, model: str) -> GeneratedImage(data: bytes, mime_type: str)`。
- `build_epub(..., cover: tuple[bytes, str] | None = None) -> bytes`。

| 方法 | `/api/v1/projects/{project_id}` 下路径 | 请求与响应 |
|---|---|---|
| GET | `/cover` | 图片原始字节；可选 `v` 查询参数 |
| PUT | `/cover` | multipart `file`；返回 `ProjectOut` |
| DELETE | `/cover` | 返回 `ProjectOut`；重复删除幂等 |
| GET | `/cover/prompt` | `{prompt: string, warning: string \| null}` |
| POST | `/cover/generate` | `{prompt: string, config_id?: int}` → `{image_base64: string, mime_type: string, model_source?: "actor" \| "owner"}`；`model_source` 为协作者路径的实际来源 |

## 3. 契约

- 人工读取封面使用 `content.read`，上传和删除使用 `content.edit`；提示词与生图要求 `ai.use`。作者、已接受的协作者和既有管理员人工能力按各自动作鉴权；仅有管理员人工权限不能代作者生图。未获项目访问权或项目不存在返回 404，GET 无封面也为 404。
- JPG/PNG/WebP，最大 `10 * 1024 * 1024` 字节；以魔数识别，上传不信文件扩展名或客户端 MIME。
  生成结果及备份另需校验声明的 MIME 与魔数一致。
- `UploadLimitMiddleware` 拦截 `PUT /api/v1/projects/\d+/cover`，上限为图片上限加 multipart 开销；
  路由再读 `MAX_COVER_BYTES + 1` 字节兜底。
- 上传和删除通过锁定父项目串行化；替换时显式更新微秒时间戳，保证短时间连续替换也改变版本。
  前端 `coverUrl` 直接编码完整服务端时间戳，不转为会丢失微秒的 JavaScript `Date`。
- `GET /cover?v=...` 为 `private, max-age=31536000, immutable`；无 `v` 时为 `private, no-cache`。
- 生成只返回预览，**不写封面表**。前端确认后将 base64 转 Blob，经同一个 `PUT /cover` 写入；取消时丢弃预览并中止请求。
- `prompt` 去首尾空白后非空，长度 1–2000；`config_id` 为正整数。作者配置沿用 `resolve_config(model_type="image")`。协作者显式配置只能属于本人；未显式指定时优先本人图片配置，只有本人没有图片配置且作者开启模型授权时才回退作者配置。本人的配置失效不能回退。协作者响应只标识 `model_source`，不得返回作者配置 ID、端点、模型名、凭据或第三方原始错误。
- 初始提示词包含书名、题材、标签、设定/简介与对当前 actor 可见的项目默认风格，保留“画面中不要出现任何文字”要求。私人风格不可见时省略其内容并返回 `warning`；不能因为管理员兼协作者身份把作者私人风格传给模型。
- 协作者在受理时固定原登录身份、邀请轮次、模型配置和 AI 可见性版本；每次真实图片发送及独立 URL 下载均执行发送门禁，结果返回前重验原授权与模型。提示词和协作者图片响应在 ASGI 发送时持有短时授权锁，超时上限为 5 秒，慢客户端不能无限阻塞撤权。已发出的字节不承诺撤回。
- OpenAI 兼容接口使用 `images.generate`，不传 `response_format`；先请求 `1024x1536`，仅明确的 400 size 错误才去掉尺寸重试一次。
  优先取 base64；URL 图片流式下载限 10MB，按现有 `ALLOW_PRIVATE_AI_ENDPOINTS` 策略做端点与重定向防护。
- Gemini 原生图片用 `generate_content` 的 IMAGE modality；Imagen 使用 `generate_images`。
  当前 SDK 的 API-key Developer API 对 Imagen 有限制，需要明确中文错误，不能假定模型名称包含 `imagen` 就可用。
  Anthropic 沿用默认“不支持图片生成”错误。
- 总超时沿用 `AI_REQUEST_TIMEOUT`；不引入独立图片存储目录或数据卷。
- 封面作为可选顶层 `cover: {mime_type, data: base64} | null`；当前备份格式为 `ainovel.project` v9，并兼容不含封面的 v1。
  `project_covers` 登记于 `NOT_EXPORTED`，二进制不走通用行导出。缺失/null 都表示无封面；导入在同一事务写入新项目封面。
- EPUB 有封面时包含图片资源、`properties="cover-image"`、兼容元数据和 spine 首位的 `cover.xhtml`；无封面不增加这些资源。
- 前端修改封面成功时只向当前项目 store 合并 `cover_updated_at`；不能用完整返回项目重置概览页未保存的表单。
- 生成弹窗的提示词计数使用正常文档流独立占位，放在输入框下方并与 footer 保持间距。
  不能在末尾零下边距的 `Form.Item` 中直接使用悬浮的 `TextArea.showCount`，否则计数会覆盖“设为封面”按钮。
  `cover-prompt` 与计数 `cover-prompt-count` 通过 `aria-describedby` 关联，计数和 `maxLength` 保持一致的长度语义。

## 4. 校验与错误矩阵

| 条件 | 行为 |
|---|---|
| 非图片文件头 | 400，中文提示仅支持 JPG/PNG/WebP，原封面不变 |
| 图片超过 10MB | 400（中间件提前拦截或读取后校验），中文上限提示，原封面不变 |
| 他人项目 | 404，不透露项目/封面是否存在 |
| viewer 请求提示词或生图 | 403；不得调用图片 Provider |
| editor 显式指定作者配置 | 404；不得调用作者模型 |
| editor 无本人图片配置且作者未授权模型 | 拒绝生图；不得调用作者模型 |
| editor 本人配置失效、原邀请/授权/模型变化或账号令牌失效 | 当前操作失败关闭，不切换其他配置；不得返回失效后的预览 |
| 无图片模型 | 400“尚未配置图片模型”；UI 提供模型配置链接 |
| 图片 Provider 不支持/超时/返回非图片 | 400 中文错误，不覆盖旧封面 |
| 空白或过长提示词、非法配置 id | 422；无 Provider 调用 |
| 备份 cover 结构/base64/MIME/大小不合法 | 400，并带 `errors`，不部分导入 |

## 5. 正常、基础与错误场景

- 正常：生成 → 预览 → 确认 → PUT；概览与书架使用新的版本 URL，备份往返字节一致。
- 基础：项目没有封面，概览/书架展示原文字占位，旧备份照常恢复，EPUB 沿用原结构。
- 协作者：editor 有本人图片配置时使用本人配置；无本人配置且作者已授权时使用作者配置并提示费用来源。
- 错误：生成预览后取消，原封面及版本不变；伪装为 PNG 的文本被拒绝；撤权后的旧图片响应不再交付。

## 6. 必需测试

- HTTP：三种魔数、伪造文件名、超限、连续替换版本、删除幂等、全部封面入口跨用户 404、项目级联删除。
- 生成：预览不落库、默认/指定图片配置、无配置/类型错误/不支持/超时/返回非图片、默认提示词与风格。
- 协作：真实 HTTP 覆盖 owner、viewer、editor、管理员和重叠身份；验证本人配置优先、本人配置失效不回退、作者授权回退及 `model_source`，确认响应和错误不包含作者私有配置。
- PostgreSQL + 真实 HTTP：分别验证发送先于撤权和撤权先于发送，模型授权/配置变更、独立图片下载及返回前权限变化；慢客户端或取消后授权锁必须释放。
- Provider：OpenAI base64/URL、size 条件回退、下载超限与私网/重定向防护；Gemini 原生与 Imagen 分支。
- 备份与 EPUB：字节往返、旧备份、损坏 cover 拒绝、封面资源与 spine、无封面原结构；迁移升级与降级。
- 前端：版本 URL 保留微秒，multipart 及 Cookie/401/中文错误，预览 Blob 原字节；浏览器验证预览取消/确认、未保存表单、书架与375px布局。

## 7. 错误与正确写法

```python
# 错误：生成时覆盖，取消弹窗也无法保留原封面。
await put_cover(db, user_id, project_id, generated.data)

# 正确：生成只返回预览；确认由上传接口写入。
return CoverGenerateOut(image_base64=base64.b64encode(image.data).decode("ascii"), mime_type=mime_type)
```

```python
# 错误：协作者无本人配置时无条件使用 project.user_id 的模型。
cfg = await ai_config_service.resolve_config(db, project.user_id, None, model_type="image")
# 正确：由服务端绑定真实 actor、原授权和配置，发送及结果边界重新核验。
binding = await project_ai_config.resolve_project_model(db, user_id, project_id, "image", cipher=cipher)
guard = collaboration_ai_guard.for_http(db, user_id, binding, stamp, cipher=cipher)
```

```typescript
// 错误：Date 只保留毫秒，连续替换可能仍命中旧图。
const version = new Date(project.cover_updated_at).getTime()
// 正确：保留完整服务端时间戳。
return buildUrl(`/projects/${project.id}/cover`, { v: project.cover_updated_at })
```
