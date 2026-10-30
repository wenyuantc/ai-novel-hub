import { ArrowRightOutlined, DownloadOutlined, FileTextOutlined, KeyOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { OPEN_API_DOCS_URL, OPEN_API_SCHEMA_URL } from '../../api/apiKeys'
import CodeBlock from './CodeBlock'
import { VALIDATION_EXAMPLE } from './content'

/** 状态码、实际校验错误结构与按密钥的请求额度。 */
export function ErrorGuide() {
  return (
    <section id="errors" className="developer-section" tabIndex={-1} aria-labelledby="developer-errors-title">
      <p className="developer-section-number">06 / 异常处理</p>
      <h2 id="developer-errors-title">错误与限流</h2>
      <p>通过 HTTP 状态码判断结果，错误响应包含中文 <code>detail</code>。参数校验失败时还包含 <code>errors</code>，不会回显请求原文或密钥。</p>
      <div className="developer-table-scroll" tabIndex={0} role="region" aria-label="开放接口错误码">
        <table>
          <thead><tr><th scope="col">状态</th><th scope="col">含义</th><th scope="col">处理建议</th></tr></thead>
          <tbody>
            <tr><td><code>400</code></td><td>请求不满足业务约束，例如没有可导出的正文</td><td>阅读 detail，调整请求</td></tr>
            <tr><td><code>401</code></td><td>Key 缺失、无效、到期、撤销，或账号不可用</td><td>检查请求头和密钥状态</td></tr>
            <tr><td><code>404</code></td><td>资源不存在，或不属于当前 Key 的授权小说</td><td>核对授权范围、小说 ID 和子资源 ID</td></tr>
            <tr><td><code>422</code></td><td>参数类型、格式或范围不正确</td><td>按 errors 中的 loc、msg 修正</td></tr>
            <tr><td><code>429</code></td><td>当前 Key 的分钟额度已用完</td><td>等待 Retry-After 指定的秒数再请求</td></tr>
          </tbody>
        </table>
      </div>
      <CodeBlock label="JSON · 422 错误示例" code={VALIDATION_EXAMPLE} />
      <h3>默认每分钟 120 次</h3>
      <p>每把 Key 按 UTC 分钟固定窗口计数，部署方可通过 <code>OPEN_API_RATE_LIMIT_PER_MINUTE</code> 调整。收到 429 后读取响应头 <code>Retry-After</code>，至少等待对应秒数，避免立即循环重试。</p>
      <p className="developer-note">身份认证成功后的 404 / 422 请求也计入额度；文档阅读不计入。分钟窗口切换时可能出现突发，请按实际配额安排分页与下载频率。</p>
    </section>
  )
}

/** 封面与全文下载的鉴权、格式和空章处理。 */
export function DownloadGuide() {
  return (
    <section id="downloads" className="developer-section" tabIndex={-1} aria-labelledby="developer-downloads-title">
      <p className="developer-section-number">07 / 文件</p>
      <h2 id="developer-downloads-title">封面与全文导出</h2>
      <h3>封面也需要请求头</h3>
      <p>小说响应中的 <code>cover_url</code> 是受保护的相对地址。使用带 <code>X-API-Key</code> 的请求读取图片字节，再按 <code>Content-Type</code> 处理 PNG、JPEG 或 WebP；没有封面时返回 404。</p>
      <p>不要把 Key 拼进 URL 或 <code>{'<img src="…">'}</code>。普通图片标签不能代替携带鉴权请求头的读取。</p>
      <CodeBlock label="Shell · 下载封面" code={`curl --fail-with-body \\
  -H "X-API-Key: $API_KEY" \\
  "$API_BASE/novels/$NOVEL_ID/cover" \\
  --output cover-image`} />
      <h3>下载 TXT、Markdown 或 EPUB</h3>
      <p><code>format</code> 支持 <code>txt</code>、<code>md</code>、<code>epub</code>，默认 txt。可选 <code>chapter_ids</code> 限定章节，支持逗号分隔或重复参数；<code>include_synopsis</code> 控制 EPUB 是否包含简介页，默认 true。</p>
      <CodeBlock label="Shell · 下载 EPUB" code={`curl --fail-with-body \\
  -H "X-API-Key: $API_KEY" \\
  -D export-headers.txt \\
  "$API_BASE/novels/$NOVEL_ID/export?format=epub" \\
  --output novel.epub`} />
      <ul className="developer-bullets">
        <li>下载是文件字节，不是 JSON；文件名可从 <code>Content-Disposition</code> 获取，中文文件名使用 <code>filename*</code>。</li>
        <li>生成中和空白正文章节会被跳过；没有可导出正文时返回 400。</li>
        <li>EPUB 跳过空章等提示位于 <code>X-Export-Warnings</code>，使用 UTF-8 百分号编码，可经 URL 百分号解码后显示。</li>
      </ul>
    </section>
  )
}

/** 指南与现有密钥管理、交互参考和描述文件的连接。 */
export function DeveloperResources() {
  return (
    <section id="resources" className="developer-section developer-resources" tabIndex={-1} aria-labelledby="developer-resources-title">
      <p className="developer-section-number">08 / 继续接入</p>
      <h2 id="developer-resources-title">相关入口</h2>
      <div className="developer-resource-links">
        <Link to="/settings/api-keys"><KeyOutlined aria-hidden /><span><strong>管理 API 密钥</strong><small>登录后配置小说授权、有效期与撤销</small></span><ArrowRightOutlined aria-hidden /></Link>
        <a href={OPEN_API_DOCS_URL} target="_blank" rel="noopener noreferrer"><FileTextOutlined aria-hidden /><span><strong>Swagger 接口参考</strong><small>完整字段、参数枚举与在线调试</small></span><ArrowRightOutlined aria-hidden /></a>
        <a href={OPEN_API_SCHEMA_URL} download="ainovel-openapi.json"><DownloadOutlined aria-hidden /><span><strong>下载 OpenAPI JSON</strong><small>导入调用工具，生成自己的客户端</small></span><ArrowRightOutlined aria-hidden /></a>
      </div>
      <p className="developer-note">本页只提供说明和公开示例，不读取你的密钥或作品。在线调用请由你在 Swagger 中主动填写密钥；实际权限始终由服务端校验。</p>
    </section>
  )
}
