import { ArrowLeftOutlined, ArrowRightOutlined, ArrowUpOutlined, DownOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { OPEN_API_DOCS_URL, OPEN_API_SCHEMA_URL } from '../api/apiKeys'
import { StudioBrand } from '../components/StudioUI'
import { AuthenticationGuide, PaginationGuide, ResponseGuide } from '../components/developerDocs/AccessAndResponses'
import ApiReference from '../components/developerDocs/ApiReference'
import { DOC_SECTIONS } from '../components/developerDocs/content'
import { DeveloperResources, DownloadGuide, ErrorGuide } from '../components/developerDocs/ErrorsAndDownloads'
import QuickStart from '../components/developerDocs/QuickStart'
import './DeveloperDocsPage.css'

function Contents() {
  return (
    <ol>
      {DOC_SECTIONS.map((section, index) => <li key={section.id}><a href={`#${section.id}`}><span>{String(index + 1).padStart(2, '0')}</span>{section.title}</a></li>)}
    </ol>
  )
}

/** 面向第三方客户的公开接入指南；不加载私有数据或收集真实密钥。 */
export default function DeveloperDocsPage() {
  const baseUrl = `${window.location.origin}/api/open/v1`
  return (
    <div className="developer-docs" id="developer-top">
      <a className="developer-skip" href="#developer-content">跳到文档正文</a>
      <header className="developer-header">
        <div className="developer-header-inner">
          <StudioBrand />
          <nav className="developer-header-nav" aria-label="开发文档导航">
            <Link to="/"><ArrowLeftOutlined aria-hidden />返回首页</Link>
            <Link to="/developers" aria-current="page">开发文档</Link>
          </nav>
          <Link className="developer-button" to="/settings/api-keys">管理密钥<ArrowRightOutlined aria-hidden /></Link>
        </div>
      </header>
      <main>
        <div className="developer-intro">
          <p className="developer-eyebrow">知页开放接口 · API v1</p>
          <h1>开发文档</h1>
          <p className="developer-lead">为你的阅读器和内容工具，接入已授权的故事。</p>
          <p className="developer-intro-description">从小说信息到章节正文，从人物档案到故事脉络。用一把只读密钥，让创作内容在你选择的工具里延伸。</p>
          <div className="developer-base-url"><span>接口地址</span><code>{baseUrl}</code><strong>只读 GET</strong></div>
          <div className="developer-intro-links"><a href={OPEN_API_DOCS_URL} target="_blank" rel="noopener noreferrer">Swagger 在线调试<ArrowRightOutlined aria-hidden /></a><a href={OPEN_API_SCHEMA_URL} download="ainovel-openapi.json">下载 OpenAPI JSON<ArrowRightOutlined aria-hidden /></a></div>
        </div>
        <div className="developer-layout">
          <aside className="developer-toc">
            <nav aria-label="文档章节目录"><p>本页目录</p><Contents /></nav>
          </aside>
          <div className="developer-article" id="developer-content" tabIndex={-1}>
            <details className="developer-mobile-toc">
              <summary>本页目录<DownOutlined aria-hidden /></summary>
              <nav aria-label="移动端文档章节目录"><Contents /></nav>
            </details>
            <QuickStart baseUrl={baseUrl} />
            <AuthenticationGuide />
            <ApiReference />
            <PaginationGuide />
            <ResponseGuide />
            <ErrorGuide />
            <DownloadGuide />
            <DeveloperResources />
          </div>
        </div>
      </main>
      <footer className="developer-footer"><StudioBrand /><p>知页小说 · 让故事拥有更多可能</p><a href="#developer-top">回到顶部<ArrowUpOutlined aria-hidden /></a></footer>
    </div>
  )
}
