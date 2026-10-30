import { Link } from 'react-router-dom'
import CodeBlock from './CodeBlock'

/** 从密钥创建、接入检查到正文读取的完整最小路径。 */
export default function QuickStart({ baseUrl }: { baseUrl: string }) {
  return (
    <section id="quick-start" className="developer-section" tabIndex={-1} aria-labelledby="developer-quick-title">
      <p className="developer-section-number">01 / 起步</p>
      <h2 id="developer-quick-title">快速开始</h2>
      <p>准备一把平台密钥，先找到小说，再从目录取得章节 ID。下面的命令按顺序在同一个终端执行，示例中的密钥与 ID 都需要自行替换。</p>
      <ol className="developer-steps">
        <li>
          <h3>创建一把平台 API 密钥</h3>
          <p><Link to="/settings/api-keys">登录并进入 API 密钥管理</Link>，填写名称，选择允许读取的小说和有效期。默认指定小说、90 天有效；创建后点击眼睛图标显示，或直接复制完整密钥。</p>
        </li>
        <li>
          <h3>检查密钥与授权</h3>
          <p><code>/me</code> 返回密钥名称、只读权限、授权小说范围和到期时间，不返回完整密钥。</p>
          <CodeBlock label="Shell · 检查接入" code={`API_BASE='${baseUrl}'
API_KEY='YOUR_API_KEY'

curl --fail-with-body \\
  -H "X-API-Key: $API_KEY" \\
  "$API_BASE/me"`} />
        </li>
        <li>
          <h3>找到小说和章节</h3>
          <p>小说列表只包含这把密钥已获授权的作品。选择列表中的 <code>items[].id</code> 作为 <code>novel_id</code>，再请求该小说的章节目录。</p>
          <CodeBlock label="Shell · 获取小说与目录" code={`curl --fail-with-body \\
  -H "X-API-Key: $API_KEY" \\
  "$API_BASE/novels?page=1&page_size=20"

# 替换为上一步 items[].id，不是小说标题
NOVEL_ID='NOVEL_ID_FROM_LIST'

curl --fail-with-body \\
  -H "X-API-Key: $API_KEY" \\
  "$API_BASE/novels/$NOVEL_ID/chapters?page=1&page_size=20"`} />
        </li>
        <li>
          <h3>读取一章正文</h3>
          <p>把目录条目的 <code>id</code> 用作 <code>chapter_id</code>。它与 <code>chapter_no</code>（章号）不同，不能互换。</p>
          <CodeBlock label="Shell · 获取章节正文" code={`# 替换为章节目录 items[].id，不是 chapter_no
CHAPTER_ID='CHAPTER_ID_FROM_DIRECTORY'

curl --fail-with-body \\
  -H "X-API-Key: $API_KEY" \\
  "$API_BASE/novels/$NOVEL_ID/chapters/$CHAPTER_ID"`} />
        </li>
      </ol>
    </section>
  )
}
