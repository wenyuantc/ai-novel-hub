import { Link } from 'react-router-dom'
import CodeBlock from './CodeBlock'
import { CHAPTER_DETAIL_EXAMPLE, CHAPTER_PAGE_EXAMPLE } from './content'

/** 客户凭据的获取、使用和生命周期说明。 */
export function AuthenticationGuide() {
  return (
    <section id="authentication" className="developer-section" tabIndex={-1} aria-labelledby="developer-auth-title">
      <p className="developer-section-number">02 / 权限</p>
      <h2 id="developer-auth-title">密钥与鉴权</h2>
      <p>每次业务请求都必须携带 <code>X-API-Key</code> 请求头。网页登录 Cookie、Bearer JWT 和模型供应商的 Key 都不能替代平台 API 密钥。</p>
      <div className="developer-inline-code"><code>X-API-Key: YOUR_API_KEY</code></div>
      <ul className="developer-bullets">
        <li><strong>按小说授权：</strong>选择指定小说，或本人的全部小说。全部授权也包含之后新建的作品；其他用户的小说始终不可访问。</li>
        <li><strong>保持只读：</strong>可读取作品及当前设定，不提供修改、删除或 AI 生成接口。</li>
        <li><strong>可再次查看：</strong>在<Link to="/settings/api-keys">密钥管理页</Link>点击图标显隐和复制。默认只显示掩码，完整值仅向密钥所有者开放。</li>
        <li><strong>独立有效期：</strong>默认 90 天，也可选 30 / 180 天、指定时间或不过期。网页登录登出、修改或重置密码不会自动撤销 Key。</li>
        <li><strong>永久撤销：</strong>到期、撤销或账号不可用后请求返回 401。撤销不可恢复，也不能再次显示；需要轮换时创建新 Key。</li>
      </ul>
      <aside className="developer-callout">
        <h3>由可信应用保管密钥</h3>
        <p>将长期 Key 保存在你控制的服务端或可信工具中，不要打包进公开网页、提交到代码仓库，或拼进 URL。本指南只展示占位符；需要在线调试时前往 Swagger 主动授权。</p>
      </aside>
    </section>
  )
}

/** 公共分页与各类资源独立筛选的边界。 */
export function PaginationGuide() {
  return (
    <section id="pagination" className="developer-section" tabIndex={-1} aria-labelledby="developer-pagination-title">
      <p className="developer-section-number">04 / 列表</p>
      <h2 id="developer-pagination-title">分页与筛选</h2>
      <div className="developer-table-scroll" tabIndex={0} role="region" aria-label="分页参数">
        <table>
          <thead><tr><th scope="col">参数</th><th scope="col">默认值</th><th scope="col">说明</th></tr></thead>
          <tbody>
            <tr><td><code>page</code></td><td>1</td><td>从 1 开始的页码</td></tr>
            <tr><td><code>page_size</code></td><td>20</td><td>每页 1–100 条，最大 100</td></tr>
          </tbody>
        </table>
      </div>
      <p>返回的 <code>total</code> 是筛选后的总条数，<code>items</code> 是当前页。可增加页码继续读取，直至收齐结果。详情、封面、导出和 <code>/me</code> 不分页。</p>
      <ul className="developer-bullets">
        <li>小说按元数据更新时间和 ID 降序；章节按 <code>chapter_no, id</code> 排序，目录不包含正文。</li>
        <li>大纲按 <code>volume_no, sort, chapter_no, id</code> 排序，细纲按 <code>seq, id</code> 排序。章号、纲内序号和资源 ID 分别表达不同含义。</li>
        <li><code>q</code> 仅在小说、人物与组织列表可用，最长 100 字符，按对应名称匹配。<code>%</code> 与 <code>_</code> 按普通字符处理。</li>
        <li>章节列表额外支持 <code>status</code> 和 <code>updated_after</code>；其他筛选仅用于<a href="#endpoints">接口目录</a>列出的对应端点。</li>
      </ul>
      <h3>按章节行更新时间筛选</h3>
      <p><code>updated_after</code> 必须带时区，例如 <code>2026-10-30T00:00:00Z</code>。只返回行更新时间<strong>严格晚于</strong>该时间的现存章节。</p>
      <CodeBlock label="Shell · 筛选更新章节" code={`curl --fail-with-body --get \\
  -H "X-API-Key: $API_KEY" \\
  --data-urlencode 'updated_after=2026-10-30T00:00:00Z' \\
  --data-urlencode 'page_size=100' \\
  "$API_BASE/novels/$NOVEL_ID/chapters"`} />
      <p className="developer-note">更新筛选不是完整增量同步：删除事件，以及关联大纲等派生字段的变化，可能不反映在章节行时间中。需要准确镜像时，定期完整读取目录并比对稳定 ID。</p>
    </section>
  )
}

/** 展示真实成功响应形态，区分列表包装和单对象。 */
export function ResponseGuide() {
  return (
    <section id="responses" className="developer-section" tabIndex={-1} aria-labelledby="developer-responses-title">
      <p className="developer-section-number">05 / 数据</p>
      <h2 id="developer-responses-title">响应与时间</h2>
      <p>列表统一返回 <code>{'{items, total, page, page_size}'}</code>；详情直接返回资源对象，封面与导出直接返回二进制文件。没有额外的 <code>data</code> 或 <code>code</code> 包装。以下内容是公开示例数据。</p>
      <h3>章节目录：分页对象</h3>
      <CodeBlock label="JSON · 章节列表示例" code={CHAPTER_PAGE_EXAMPLE} />
      <h3>单章正文：直接返回对象</h3>
      <CodeBlock label="JSON · 章节详情示例" code={CHAPTER_DETAIL_EXAMPLE} />
      <dl className="developer-definitions">
        <div><dt><code>id</code> 与 <code>novel_id</code></dt><dd>资源的稳定 ID 及所属小说 ID。示例中章号是 3，实际章节 ID 是 305。</dd></div>
        <div><dt><code>updated_at</code></dt><dd>资源行的更新时间。对小说而言，只表示小说元数据，不等同于全部内容更新时间。</dd></div>
        <div><dt><code>content_updated_at</code></dt><dd>小说现存章节行的最大更新时间；没有章节时为 null，不代表删除事件或全部设定变动。</dd></div>
        <div><dt>时间与空值</dt><dd>时间使用 UTC / 带时区的 ISO 8601 字符串。未分卷的目录字段、未建章细纲的章节引用等可返回 null。</dd></div>
      </dl>
      <p className="developer-note">目录包含 <code>draft</code>、<code>generating</code>、<code>done</code> 章节，也可能包含空章。正文是当前保存值，空章的 <code>content</code> 为 <code>""</code>；生成中不表示正文已经完成。</p>
    </section>
  )
}
