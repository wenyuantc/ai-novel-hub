import { OPEN_API_DOCS_URL } from '../../api/apiKeys'
import { ENDPOINT_GROUPS } from './content'

/** 用真实路由分组展示全部只读端点，完整字段由 Swagger 提供。 */
export default function ApiReference() {
  return (
    <section id="endpoints" className="developer-section" tabIndex={-1} aria-labelledby="developer-endpoints-title">
      <p className="developer-section-number">03 / 接口</p>
      <h2 id="developer-endpoints-title">接口目录</h2>
      <p>以下 20 个接口全部使用 <code>GET</code>，路径前缀为 <code>/api/open/v1</code>。列表支持公共分页参数；“额外参数”只适用于所在行。字段类型、枚举与完整参数见 <a href={OPEN_API_DOCS_URL} target="_blank" rel="noopener noreferrer">Swagger 接口参考</a>。</p>
      <p className="developer-note">人物、大纲、组织和关系返回当前保存的设定，可能包含后续剧情。关系记录保留双方称呼；组织成员数包含离开、被逐、死亡等全部成员状态。</p>
      {ENDPOINT_GROUPS.map((group) => (
        <div className="developer-endpoint-group" key={group.title}>
          <h3>{group.title}</h3>
          <div className="developer-table-scroll" tabIndex={0} role="region" aria-label={`${group.title}接口，可横向滚动`}>
            <table className="developer-endpoint-table">
              <thead><tr><th scope="col">GET 路径</th><th scope="col">返回内容</th><th scope="col">额外参数</th></tr></thead>
              <tbody>{group.endpoints.map((endpoint) => (
                <tr key={endpoint.path}><td><code>{endpoint.path}</code></td><td>{endpoint.description}</td><td>{endpoint.extra ?? '—'}</td></tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      ))}
    </section>
  )
}
